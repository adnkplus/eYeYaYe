#!/usr/bin/env python3
"""Cue-based synchronized multi-camera MP4 export.

All inputs are existing project recordings. Original and merged sources are
preferred; time-lapse sources are considered only after explicit acceptance.
The module writes temporary work below cache/export-work and final MP4 files
below data/exports. It never modifies or removes source recordings.
"""
from __future__ import annotations

import json
import math
import os
import re
import subprocess
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path

import core
from naming import parse as parse_recording_name

ROOT = core.ROOT
OUTPUT = ROOT / 'data' / 'exports'
WORK = ROOT / 'cache' / 'export-work'


def _command(name: str, values: dict) -> list[str]:
    return core.configured_command(name, values)


def _run(args: list[str], timeout: int = 3600) -> subprocess.CompletedProcess:
    core.log('EXPORT EXEC ' + core.redact_command(args))
    result = subprocess.run(args, cwd=ROOT, capture_output=True, text=True, timeout=timeout)
    if result.returncode:
        raise RuntimeError((result.stderr or result.stdout or f'{args[0]} exited with {result.returncode}')[-5000:])
    return result


def _probe(path: Path) -> dict:
    ctl = core.load_control()
    args = _command('export_probe', {
        'ffprobe': str(ctl.get('FFPROBE_BINARY', str(core.BUNDLED_FFPROBE))),
        'input': str(path),
    })
    result = _run(args, timeout=45)
    try:
        info = json.loads(result.stdout or '{}')
    except json.JSONDecodeError as exc:
        raise RuntimeError(f'Invalid ffprobe response for {path.name}.') from exc
    duration = float((info.get('format') or {}).get('duration') or 0)
    streams = info.get('streams') or []
    return {'duration': duration, 'video': any(s.get('codec_type') == 'video' for s in streams),
            'audio': any(s.get('codec_type') == 'audio' for s in streams)}


def _manifest_index() -> dict[str, dict]:
    base = ROOT / 'data' / 'recordings'
    indexed: dict[str, dict] = {}
    for folder in (base / 'archives', base / 'timelapse'):
        if not folder.exists():
            continue
        for manifest in folder.rglob('*.manifest.json'):
            try:
                meta = json.loads(manifest.read_text(encoding='utf-8'))
                for part in meta.get('archive_parts', []):
                    if part.get('file'):
                        indexed[str(part['file'])] = {
                            'start': part.get('start'), 'end': part.get('end'),
                            'duration': part.get('duration_seconds'),
                            'time_map': part.get('time_map') or [], 'mode': meta.get('mode')}
                if meta.get('output'):
                    indexed[str(meta['output'])] = {
                        'start': (meta.get('source_timeline_map') or [{}])[0].get('source_start') or meta.get('archive_day_start'),
                        'duration': meta.get('duration_seconds'), 'time_map': meta.get('time_map') or [],
                        'mode': meta.get('mode')}
            except Exception as exc:
                core.log(f'EXPORT ignored unreadable manifest {manifest.name}: {exc}')
    return indexed


def _media_index() -> list[dict]:
    base = ROOT / 'data' / 'recordings'
    manifest = _manifest_index()
    code_to_camera = {core.camera_filename_code(c.get('id', '')): core.safe_id(c.get('id', ''))
                      for c in core.cameras()}
    entries = []
    for group in ('original', 'archives', 'timelapse'):
        folder = base / group
        if not folder.exists():
            continue
        for path in folder.rglob('*'):
            if not path.is_file() or path.suffix.lower() not in ('.ts', '.mp4', '.mkv', '.mov'):
                continue
            rel = str(path.relative_to(ROOT))
            metadata = manifest.get(rel, {})
            match = parse_recording_name(path.stem)
            camera_id = code_to_camera.get(match['camera']) if match else None
            kind = match['kind'] if match else None
            if not camera_id:
                camera_id = path.parent.name
            if kind is None:
                kind = 'RAW' if group == 'original' else 'MER' if group == 'archives' else ('TLR' if metadata.get('mode') == 'regular' else 'TLI')
            start = None
            if metadata.get('start'):
                try:
                    start = datetime.fromisoformat(str(metadata['start'])).timestamp()
                except Exception:
                    start = None
            if start is None and match:
                start = match['started'].timestamp()
            if start is None:
                try:
                    start = path.stat().st_mtime
                except OSError:
                    continue
            entries.append({'path': path, 'relative': rel, 'camera_id': camera_id, 'kind': kind,
                            'start_ts': float(start), 'duration': float(metadata.get('duration') or 0),
                            'time_map': metadata.get('time_map') or []})
    return entries


def _uncovered(spans: list[tuple[float, float]], a: float, b: float) -> tuple[list[tuple[float, float]], list[tuple[float, float]]]:
    """Return overlap pieces and the remaining portions of a list of intervals."""
    hits = []
    remain = []
    for x, y in spans:
        lo, hi = max(x, a), min(y, b)
        if hi > lo:
            hits.append((lo, hi))
            if x < lo: remain.append((x, lo))
            if hi < y: remain.append((hi, y))
        else:
            remain.append((x, y))
    return hits, remain


def _timeline_parts(camera_id: str, start: float, end: float, allow_timelapse: bool,
                    entries: list[dict]) -> tuple[list[dict], bool]:
    candidates = [dict(item) for item in entries if item['camera_id'] == camera_id]
    priority = {'RAW': 0, 'MER': 1, 'TLR': 2, 'TLI': 2}
    candidates.sort(key=lambda x: (priority.get(x['kind'], 9), x['start_ts']))
    missing = [(start, end)]
    selected = []
    timelapse_used = False
    for item in candidates:
        kind = item['kind']
        if kind not in ('RAW', 'MER', 'TLR', 'TLI'):
            continue
        is_tl = kind in ('TLR', 'TLI')
        if is_tl and not allow_timelapse:
            continue
        if is_tl:
            # A time-lapse file maps short output time back to its source wall-clock
            # timeline. Intersect each map entry with the requested cue interval.
            for mapping in item.get('time_map') or []:
                try:
                    wall_a = datetime.fromisoformat(mapping['source_start']).timestamp()
                    wall_b = datetime.fromisoformat(mapping['source_end']).timestamp()
                    speed = max(1.0, float(mapping.get('speed', 1.0)))
                    out_a = float(mapping.get('output_start_seconds', 0.0))
                    out_b = float(mapping.get('output_end_seconds', out_a))
                except Exception:
                    continue
                media_start = item['start_ts'] + out_a
                media_end = item['start_ts'] + out_b
                hits, remain = _uncovered(missing, wall_a, wall_b)
                for lo, hi in hits:
                    selected.append({'path': item['path'], 'kind': kind, 'wall_start': lo, 'wall_end': hi,
                                     'source_offset': out_a + (lo - wall_a) / speed,
                                     'media_duration': (hi - lo) / speed, 'wall_duration': hi - lo,
                                     'speed': speed, 'camera_id': camera_id})
                    timelapse_used = True
                missing = remain
            continue
        if item['duration'] <= 0:
            probe = _probe(item['path'])
            if not probe['video']:
                continue
            item['duration'] = probe['duration']
        clip_start = item['start_ts']; clip_end = clip_start + item['duration']
        hits, remain = _uncovered(missing, clip_start, clip_end)
        for lo, hi in hits:
            selected.append({'path': item['path'], 'kind': kind, 'wall_start': lo, 'wall_end': hi,
                             'source_offset': lo - clip_start, 'media_duration': hi - lo,
                             'wall_duration': hi - lo, 'speed': 1.0, 'camera_id': camera_id})
        missing = remain
        if not missing:
            break
    if missing:
        ranges = ', '.join(f'{datetime.fromtimestamp(a).astimezone():%Y-%m-%d %H:%M:%S}–{datetime.fromtimestamp(b).astimezone():%H:%M:%S}' for a, b in missing)
        raise RuntimeError(f'{camera_id}: footage does not fully cover this cue ({ranges}). Check other cameras or available recordings.')
    selected.sort(key=lambda x: x['wall_start'])
    return selected, timelapse_used


def _atempo(speed: float) -> str:
    value = max(0.5, float(speed)); chain = []
    while value > 2.0:
        chain.append('atempo=2.0'); value /= 2.0
    while value < 0.5:
        chain.append('atempo=0.5'); value /= 0.5
    chain.append(f'atempo={value:.6f}')
    return ','.join(chain)


def _encode_camera(camera_id: str, parts: list[dict], destination: Path, work: Path,
                   pane: tuple[int, int], fps: int, preset: str, crf: int, bitrate: str,
                   ffmpeg: str) -> None:
    width, height = pane
    work.mkdir(parents=True, exist_ok=True)
    chunks = []
    for index, part in enumerate(parts, 1):
        info = _probe(part['path'])
        if not info['video']:
            raise RuntimeError(f'No video stream in {part["path"].name}.')
        chunk = work / f'{camera_id}_{index:03d}.mp4'
        speed = max(1.0, float(part.get('speed', 1.0)))
        vfilter = (f'setpts=PTS*{speed:.8f},fps={fps},'
                   f'scale={width}:{height}:force_original_aspect_ratio=decrease,'
                   f'pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1')
        if info['audio']:
            afilter = f'asetpts=PTS-STARTPTS,{_atempo(1.0 / speed)},apad,atrim=duration={part["wall_duration"]:.6f}'
        else:
            afilter = f'anullsrc=channel_layout=stereo:sample_rate=48000,atrim=duration={part["wall_duration"]:.6f}'
        args = _command('export_segment', {
            'ffmpeg': ffmpeg, 'loglevel': 'warning',
            'source_offset': f'{max(0.0, float(part["source_offset"])):.6f}',
            'source': str(part['path']), 'media_duration': f'{float(part["media_duration"]):.6f}',
            'filter_complex': f'[0:v:0]{vfilter}[vout];[0:a:0]{afilter}[aout]' if info['audio'] else f'[0:v:0]{vfilter}[vout];{afilter}[aout]',
            'wall_duration': f'{float(part["wall_duration"]):.6f}', 'fps': str(fps),
            'preset': preset, 'crf': str(crf), 'audio_bitrate': bitrate, 'output': str(chunk),
        })
        _run(args, timeout=max(300, int(part['wall_duration'] * 4 + 120)))
        chunks.append(chunk)
    if not chunks:
        raise RuntimeError(f'No usable source clips for {camera_id}.')
    if len(chunks) == 1:
        os.replace(chunks[0], destination)
        return
    concat = work / f'{camera_id}.ffconcat'
    concat.write_text(''.join("file '" + str(p.resolve()).replace("'", "'\\''") + "'\n" for p in chunks), encoding='utf-8')
    _run(_command('export_concat', {'ffmpeg': ffmpeg, 'loglevel': 'warning',
                                    'concat_file': str(concat), 'output': str(destination)}), timeout=3600)


def _layout_filter(count: int, width: int, height: int, fps: int, audio_mode: str, duration: float) -> str:
    if count == 1:
        video = '[0:v:0]setsar=1[vout]'
    elif count == 2:
        video = '[0:v:0][1:v:0]hstack=inputs=2[vout]'
    else:
        half_w, half_h = width // 2, height // 2
        # Three-camera composition: two half-size panes across the top,
        # and one full-width pane across the bottom.
        video = (f'color=c=black:s={width}x{height}:r={fps}[base];'
                 f'[base][0:v:0]overlay=0:0[tmp1];'
                 f'[tmp1][1:v:0]overlay={half_w}:0[tmp2];'
                 f'[tmp2][2:v:0]overlay=0:{half_h}[vout]')
    if audio_mode == 'mute':
        audio = f'anullsrc=channel_layout=stereo:sample_rate=48000,atrim=duration={duration:.6f}[aout]'
    elif audio_mode == 'first' or count == 1:
        audio = '[0:a:0]anull[aout]'
    else:
        labels = ''.join(f'[{i}:a:0]' for i in range(count))
        audio = f'{labels}amix=inputs={count}:duration=longest:dropout_transition=2,alimiter=limit=0.95[aout]'
    return video + ';' + audio


def _export_section(section: dict, cue: dict, camera_ids: list[str], media: list[dict], work: Path,
                    settings: dict, ffmpeg: str, section_no: int) -> tuple[Path, bool]:
    start = datetime.fromisoformat(cue['start']).timestamp()
    end = datetime.fromisoformat(cue['end']).timestamp()
    if end <= start:
        raise ValueError(f'Cue {cue.get("label", cue.get("id"))} has an invalid time range.')
    duration = end - start
    count = len(camera_ids)
    width, height = settings['width'], settings['height']
    camera_files = []
    any_tl = False
    for camera_index, camera_id in enumerate(camera_ids):
        if count == 1:
            pane = (width, height)
        elif count == 2:
            pane = (width // 2, height)
        else:
            # The third camera spans the bottom row; the first two share the top.
            pane = (width, height // 2) if camera_index == 2 else (width // 2, height // 2)
        parts, used_tl = _timeline_parts(camera_id, start, end, settings['allow_timelapse'], media)
        any_tl = any_tl or used_tl
        camera_file = work / f'section_{section_no:03d}_{camera_id}.mp4'
        _encode_camera(camera_id, parts, camera_file, work / f'section_{section_no:03d}_{camera_id}_work',
                       pane, settings['fps'], settings['preset'], settings['crf'], settings['audio_bitrate'], ffmpeg)
        camera_files.append(camera_file)
    filter_graph = _layout_filter(count, width, height, settings['fps'], settings['audio_mode'], duration)
    args = [ffmpeg, '-y', '-hide_banner', '-loglevel', 'warning']
    for path in camera_files:
        args += ['-i', str(path)]
    args += _command('export_layout', {
        'filter_complex': filter_graph, 'fps': str(settings['fps']), 'preset': settings['preset'],
        'crf': str(settings['crf']), 'audio_bitrate': settings['audio_bitrate'],
        'duration': f'{duration:.6f}', 'output': str(work / f'section_{section_no:03d}_final.mp4'),
    })
    # export_layout is an options fragment: input arguments above are generated
    # for the exact camera count, while all codec/mapping switches are editable.
    output = work / f'section_{section_no:03d}_final.mp4'
    _run(args, timeout=max(600, int(duration * 8 + 240)))
    return output, any_tl


def _validate_request(request: dict) -> tuple[list[dict], dict, list[dict]]:
    cues = core.bookmarks()
    by_id = {item.get('id'): item for item in cues}
    sections = request.get('sections')
    if not isinstance(sections, list) or not sections or len(sections) > 100:
        raise ValueError('Add between 1 and 100 saved cues to the export queue.')
    camera_ids = {core.safe_id(c.get('id', '')) for c in core.cameras()}
    clean = []
    for entry in sections:
        cue = by_id.get(str(entry.get('cue_id', '')))
        if not cue:
            raise ValueError('One of the selected cues no longer exists. Refresh the Export Studio.')
        cams = entry.get('camera_ids')
        if not isinstance(cams, list) or len(cams) not in (1, 2, 3):
            raise ValueError(f'{cue.get("label", "Cue")}: select one, two or three cameras.')
        cams = list(dict.fromkeys(core.safe_id(x) for x in cams))
        if len(cams) != len(entry.get('camera_ids')) or any(x not in camera_ids for x in cams):
            raise ValueError('An export camera is missing or duplicated.')
        clean.append({'cue': cue, 'camera_ids': cams})
    output = request.get('settings') or {}
    resolution = str(output.get('resolution', '1920x1080'))
    match = re.fullmatch(r'(1280|1920|2560)x(720|1080|1440)', resolution)
    if not match:
        raise ValueError('Unsupported export resolution.')
    width, height = map(int, match.groups())
    if width % 2 or height % 2:
        raise ValueError('Export resolution dimensions must be even.')
    fps = int(output.get('fps', 25))
    if fps not in (25, 30, 50, 60):
        raise ValueError('Export frame rate must be 25, 30, 50 or 60 FPS.')
    crf = max(16, min(30, int(output.get('crf', 20))))
    preset = str(output.get('preset', 'medium'))
    if preset not in ('ultrafast', 'veryfast', 'faster', 'fast', 'medium', 'slow'):
        raise ValueError('Unsupported H.264 encoding preset.')
    audio_mode = str(output.get('audio_mode', 'mix'))
    if audio_mode not in ('mix', 'first', 'mute'):
        raise ValueError('Audio mode must be mix, first or mute.')
    allow_timelapse = bool(output.get('allow_timelapse', False))
    settings = {'width': width, 'height': height, 'fps': fps, 'crf': crf,
                'preset': preset, 'audio_mode': audio_mode, 'allow_timelapse': allow_timelapse,
                'audio_bitrate': '192k'}
    return clean, settings, cues


def run_export(request: dict, status_callback=None) -> dict:
    """Build the MP4 export in the requested cue order and return its verified path."""
    clean, settings, _ = _validate_request(request)
    ctl = core.load_control()
    ffmpeg = str(ctl.get('FFMPEG_BINARY', str(core.BUNDLED_FFMPEG)))
    OUTPUT.mkdir(parents=True, exist_ok=True)
    WORK.mkdir(parents=True, exist_ok=True)
    job_id = uuid.uuid4().hex[:12]
    work = WORK / job_id
    work.mkdir(parents=True, exist_ok=False)
    created = datetime.now().astimezone()
    final_name = f'CCTV_EXPORT_{created.strftime("%Y%m%d_%H%M%S")}.mp4'
    final_path = OUTPUT / final_name
    if final_path.exists():
        final_name = f'CCTV_EXPORT_{created.strftime("%Y%m%d_%H%M%S")}_{job_id[:4]}.mp4'
        final_path = OUTPUT / final_name
    media = _media_index()
    sections_out = []
    time_lapse_sections = []
    try:
        for index, section in enumerate(clean, 1):
            cue = section['cue']
            if status_callback:
                status_callback({'state': 'RUNNING', 'message': f'Preparing section {index}/{len(clean)}: {cue.get("label", "Cue")}',
                                 'completed_sections': index - 1, 'total_sections': len(clean), 'output': ''})
            output, used_tl = _export_section(section, cue, section['camera_ids'], media, work, settings, ffmpeg, index)
            sections_out.append(output)
            if used_tl:
                time_lapse_sections.append({'cue': cue.get('label'), 'camera_ids': section['camera_ids']})
        if not sections_out:
            raise RuntimeError('No export sections were generated.')
        concat = work / 'final.ffconcat'
        concat.write_text(''.join("file '" + str(p.resolve()).replace("'", "'\\''") + "'\n" for p in sections_out), encoding='utf-8')
        _run(_command('export_concat', {'ffmpeg': ffmpeg, 'loglevel': 'warning',
                                        'concat_file': str(concat), 'output': str(final_path)}), timeout=24 * 3600)
        info = _probe(final_path)
        if not info['video'] or info['duration'] <= 0 or final_path.stat().st_size < 2048:
            raise RuntimeError('Final export failed verification: no decodable video or invalid duration.')
        manifest = {
            'schema_version': 1, 'job_id': job_id, 'created': created.isoformat(timespec='seconds'),
            'output': str(final_path.relative_to(ROOT)), 'duration_seconds': round(info['duration'], 3),
            'resolution': f'{settings["width"]}x{settings["height"]}', 'fps': settings['fps'],
            'video_codec': 'H.264', 'audio_codec': 'AAC', 'audio_mode': settings['audio_mode'],
            'pixel_format': 'yuv420p', 'faststart': True,
            'sections': [{'cue_id': x['cue']['id'], 'label': x['cue'].get('label'),
                          'start': x['cue']['start'], 'end': x['cue']['end'], 'camera_ids': x['camera_ids']}
                         for x in clean],
            'time_lapse_sources_used': time_lapse_sections,
        }
        final_path.with_suffix('.manifest.json').write_text(json.dumps(manifest, indent=2) + '\n', encoding='utf-8')
        if status_callback:
            status_callback({'state': 'COMPLETE', 'message': 'Export verified successfully.',
                             'completed_sections': len(clean), 'total_sections': len(clean),
                             'output': str(final_path.relative_to(ROOT)), 'duration_seconds': info['duration'],
                             'time_lapse_sources_used': time_lapse_sections})
        return manifest
    except Exception:
        final_path.unlink(missing_ok=True)
        final_path.with_suffix('.manifest.json').unlink(missing_ok=True)
        raise
    finally:
        # Successful section files are temporary; final export and manifest live
        # under data/exports. Retain failed work for diagnosis, clean successful.
        if final_path.exists():
            import shutil
            shutil.rmtree(work, ignore_errors=True)
