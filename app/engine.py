#!/usr/bin/env python3
"""CCTV media engine and local Unix-socket control service.

There is deliberately no HTTP server, REST endpoint, browser UI, or local web page.
The desktop client talks to this process over runtime/cctv.sock.
"""
from __future__ import annotations

import json
import base64
import re
import os
import signal
import socket
import socketserver
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

import core
from naming import parse_path as parse_recording_path

ROOT = core.ROOT
SOCKET_PATH = ROOT / 'runtime' / 'cctv.sock'
RPC_LIMIT = 2_000_000
shutdown_event = threading.Event()





def _public_camera(cam: dict) -> dict:
    item = dict(cam)
    item['has_password'] = bool(item.get('password'))
    item['has_web_admin_password'] = bool(item.get('web_admin_password'))
    item['has_audio_source_password'] = bool(item.get('audio_source_password'))
    if item.get('type') == 'cam3' and not item.get('password'):
        item['password'] = item.get('rtmp_password', '')
    # This response is available only over the owner-local Unix socket. Keep
    # credentials visible in the editor as explicitly requested by the owner.
    item['clock_warning'] = abs(int(item.get('clock_offset_seconds', 0) or 0)) >= int(
        core.load_control().get('CAMERA_CLOCK_WARNING_SECONDS', 5))
    item['live_url'] = f"rtsp://127.0.0.1:8554/{item.get('id', '')}"
    item['live_pipe'] = str(core.live_pipe_path(item.get('id', '')).relative_to(ROOT))
    return item


camera_status_cache = {}
camera_status_lock = threading.RLock()
export_job_lock = threading.RLock()
export_job = {'id': '', 'state': 'IDLE', 'message': 'No export has been started.',
              'completed_sections': 0, 'total_sections': 0, 'output': '',
              'duration_seconds': 0.0, 'time_lapse_sources_used': []}


def _run_export_job(request: dict, job_id: str) -> None:
    from export import run_export
    def update(values):
        with export_job_lock:
            export_job.update(values)
    try:
        result = run_export(request, status_callback=update)
        update({'id': job_id, 'state': 'COMPLETE', 'message': 'Export verified successfully.',
                'output': result.get('output', ''), 'duration_seconds': result.get('duration_seconds', 0),
                'time_lapse_sources_used': result.get('time_lapse_sources_used', [])})
        core.log('EXPORT COMPLETE ' + str(result.get('output', '')))
    except Exception as exc:
        update({'id': job_id, 'state': 'FAILED', 'message': f'{type(exc).__name__}: {exc}',
                'output': ''})
        core.log(f'EXPORT FAILED {type(exc).__name__}: {exc}')




def camera_probe_scheduler():
    """Continuously verify real per-camera video and audio tracks via ffprobe."""
    while not shutdown_event.is_set():
        for cam in core.cameras():
            cid = core.safe_id(cam.get('id', ''))
            if not cam.get('enabled', True):
                with camera_status_lock: camera_status_cache[cid] = {'status':'DISABLED','video':False,'audio':False,'checked':time.time()}
                continue
            # If the shared RTSP router is down, report that shared failure for
            # every camera instead of mixing misleading NO VIDEO / ENGINE OFFLINE states.
            try:
                with socket.create_connection(('127.0.0.1', 8554), timeout=0.4):
                    router_ready = True
            except OSError:
                router_ready = False
            if not router_ready:
                with camera_status_lock:
                    camera_status_cache[cid] = {'status':'ENGINE OFFLINE','video':False,'audio':False,
                        'details':core.last_error or 'MediaMTX RTSP listener 127.0.0.1:8554 is unavailable.',
                        'checked':time.time()}
                continue
            url = f'rtsp://127.0.0.1:8554/{cid}'
            ffprobe = str(core.load_control().get('FFPROBE_BINARY', str(core.BUNDLED_FFPROBE)))
            cmd = [ffprobe,'-v','error','-rtsp_transport','tcp','-show_streams','-show_entries','stream=codec_type,codec_name,width,height,r_frame_rate','-of','json',url]
            try:
                result = subprocess.run(cmd,capture_output=True,text=True,timeout=10)
                parsed = json.loads(result.stdout or '{}') if result.returncode == 0 else {}
                streams = parsed.get('streams',[])
                video = any(x.get('codec_type')=='video' for x in streams)
                audio = any(x.get('codec_type')=='audio' for x in streams)
                details = next((f"{x.get('codec_name','?')} {x.get('width','?')}x{x.get('height','?')} {x.get('r_frame_rate','?')}" for x in streams if x.get('codec_type')=='video'),'')
                if result.returncode != 0:
                    details = (result.stderr or result.stdout or 'ffprobe could not read the stream.')[-500:]
                row={'status':'ONLINE' if video else 'NO VIDEO','video':video,'audio':audio,'details':details,'checked':time.time()}
            except Exception as exc:
                row={'status':'NO VIDEO','video':False,'audio':False,'details':str(exc)[:500],'checked':time.time()}
            with camera_status_lock: camera_status_cache[cid]=row
            if shutdown_event.wait(2): return
        shutdown_event.wait(max(5,int(core.load_control().get('CAMERA_STATUS_PROBE_SECONDS',20))))


def _state() -> dict:
    ctl = core.load_control()
    cams = core.cameras()
    original_root = core.DATA / 'recordings' / 'original'
    archive_root = core.DATA / 'recordings' / 'archives'
    originals = [p for p in original_root.rglob('*') if p.is_file()] if original_root.exists() else []
    archives = [p for p in archive_root.rglob('*') if p.is_file() and p.suffix.lower() in ('.mp4', '.mkv', '.ts')] if archive_root.exists() else []
    stat = os.statvfs(core.DATA)
    free_gb = stat.f_bavail * stat.f_frsize / (1024 ** 3)
    process_active = core.media_proc is not None and core.media_proc.poll() is None
    active = False
    if process_active:
        try:
            with socket.create_connection(('127.0.0.1', 8554), timeout=0.4):
                active = True
        except OSError:
            active = False
    media_error = core.last_error
    if process_active and not active and not media_error:
        media_error = 'MediaMTX process exists, but RTSP listener 127.0.0.1:8554 is not accepting connections.'
    camera_rows = []
    for cam in cams:
        row = _public_camera(cam)
        cid = core.safe_id(cam.get('id', ''))
        row['bridge_running'] = cid in core.bridge_procs and core.bridge_procs[cid].poll() is None
        row['recorder'] = core.recording_state(cid)
        row['recording_segments'] = sum(1 for p in (original_root / cid).rglob('*') if p.is_file()) if (original_root / cid).exists() else 0
        with camera_status_lock:
            probe = dict(camera_status_cache.get(cid, {}))
        if not cam.get('enabled', True):
            row['status'] = 'DISABLED'
        elif not active:
            row['status'] = 'ENGINE OFFLINE'
        else:
            row['status'] = probe.get('status', 'WAITING')
        row['video_ready'] = bool(active and probe.get('video', False))
        row['audio_ready'] = bool(active and probe.get('audio', False))
        row['source_details'] = probe.get('details','') or (media_error if not active else '')
        camera_rows.append(row)
    return {
        'ok': True, 'media_server': active, 'last_error': media_error,
        'control_error': core.control_error, 'archive_job': dict(core.archive_job),
        'cameras': camera_rows, 'original_segments': len(originals), 'archive_files': len(archives),
        'disk_free_gb': round(free_gb, 2),
        'disk_warning': free_gb < float(ctl.get('RECORDING_MIN_FREE_GB_WARNING', 20)),
        'camera_limit_warning': len(cams) > int(ctl.get('MAX_CAMERAS_SOFT_LIMIT', 32)),
        'cam3_bundle_present': all((core.CAM3_BUNDLE / x).is_file() for x in ('rtmp_live_bridge.py', 'rtmp_cam.py', 'bin/mediamtx', 'bin/ffmpeg', 'bin/ffprobe', 'control/COMMANDS.json')),
        'cam3_client_present': (core.CAM3_BUNDLE / 'rtmp_cam.py').is_file(),
        'settings': {k: v for k, v in ctl.items() if k in (
            'PURGE_ORIGINAL_RETENTION_DAYS', 'RECORDING_SEGMENT_SECONDS', 'ARCHIVE_DAY_START',
            'ARCHIVE_AUTO_BUILD', 'ARCHIVE_AUTO_BUILD_DELAY_DAYS', 'ENABLE_DAILY_ARCHIVE',
            'ENABLE_REGULAR_TIMELAPSE', 'ENABLE_INTELLIGENT_TIMELAPSE',
            'ENABLE_DETECTION_EXCLUSION_ZONES', 'ENABLE_ZONE_BASED_ARCHIVE_COMPRESSION',
            'PLAYBACK_DEFAULT_SPEED', 'PURGE_ENABLED', 'PURGE_DRY_RUN',
            'PURGE_ORIGINAL_AFTER_VERIFIED_ARCHIVE', 'PURGE_CACHE_PLAYBACK_ENABLED',
            'PURGE_TEMP_JOB_FILES_ENABLED', 'PURGE_ROTATED_LOGS_ENABLED', 'PLAYBACK_MAX_SPEED',
            'LIVE_LAYOUTS', 'LIVE_LAYOUT_DEFAULT', 'LIVE_DOCK_DEFAULT_WIDTH_PERCENT',
            'DEFAULT_WORKSPACE', 'WORKSPACE_REMEMBER_LAYOUT', 'WORKSPACE_LIVE_DOCK_POSITION',
            'WORKSPACE_LIVE_DOCK_MIN_WIDTH_PERCENT', 'WORKSPACE_LIVE_DOCK_MAX_WIDTH_PERCENT',
            'LIVE_DOCK_RESIZABLE', 'LIVE_DOCK_COLLAPSIBLE', 'WORKSPACE_SHOW_STATUS_DETAILS',
            'STATUS_REFRESH_SECONDS', 'MAX_CAMERAS_SOFT_LIMIT', 'GUI_VIDEO_BACKEND',
            'GUI_DEFAULT_DOCK_POSITION', 'GUI_DEFAULT_DOCK_VISIBLE',
        )}
    }


def _recordings() -> list[dict]:
    """Index RAW/MER/TLR/TLI media using their timestamp-based filename scheme."""
    base = core.DATA / 'recordings'
    items: list[dict] = []
    manifest_index: dict[str, dict] = {}
    manifests = list((base / 'archives').rglob('*.manifest.json')) + list((base / 'timelapse').rglob('*.manifest.json'))
    for manifest in manifests:
        try:
            meta = json.loads(manifest.read_text(encoding='utf-8'))
            for part in meta.get('archive_parts', []):
                if part.get('file'):
                    manifest_index[part['file']] = {'start': part.get('start'), 'end': part.get('end'), 'duration': part.get('duration_seconds'), 'time_map': part.get('time_map', []), 'mode': meta.get('mode')}
            if meta.get('output'):
                timeline = meta.get('source_timeline_map') or []
                start_value = timeline[0].get('source_start') if timeline else meta.get('archive_day_start')
                manifest_index[meta['output']] = {'start': start_value, 'duration': meta.get('duration_seconds'),
                    'time_map': meta.get('time_map', []), 'mode': meta.get('mode')}
        except Exception:
            continue
    camera_by_code = {}
    for cam in core.cameras():
        cid = core.safe_id(cam.get('id', ''))
        camera_by_code[core.camera_filename_code(cid)] = cid
    # Current and legacy archive naming are parsed by app/naming.py.
    for group in ('original', 'archives', 'timelapse'):
        folder = base / group
        if not folder.exists():
            continue
        for path in sorted(folder.rglob('*')):
            if not path.is_file() or path.suffix.lower() not in ('.ts', '.mp4', '.mkv', '.mov'):
                continue
            stat = path.stat()
            rel = str(path.relative_to(ROOT))
            entry = manifest_index.get(rel, {})
            match = parse_recording_path(path)
            cid = None
            kind = None
            start_dt = None
            if match:
                cid = camera_by_code.get(match['camera'])
                kind = match['kind']
                start_dt = match['started']
            if cid is None:
                cid = path.parent.name if group in ('original', 'timelapse') else (path.parent.parent.name if path.parent.name.count('-') == 2 else path.parent.name)
            if kind is None:
                kind = ('TLR' if entry.get('mode') == 'regular' else 'TLI') if group == 'timelapse' else {'original':'RAW','archives':'MER'}.get(group,'RAW')
            if entry.get('start'):
                try: start_dt = datetime.fromisoformat(entry['start']).astimezone()
                except Exception: pass
            if start_dt is None:
                try: start_dt = datetime.strptime(path.stem, '%Y-%m-%d_%H-%M-%S-%f_%z').astimezone()
                except Exception:
                    try: start_dt = datetime.fromtimestamp(stat.st_mtime).astimezone()
                    except Exception: continue
            duration = float(entry.get('duration') or (600.0 if kind == 'RAW' else 0.0))
            if entry.get('end'):
                try: duration = max(0.0, (datetime.fromisoformat(entry['end']) - datetime.fromisoformat(entry['start'])).total_seconds())
                except Exception: pass
            if duration <= 0 and kind == 'RAW': duration = 600.0
            items.append({'path': rel, 'group': group, 'type_code': kind, 'camera_id': cid, 'name': path.name,
                'size': stat.st_size, 'start_ts': start_dt.timestamp(), 'duration_seconds': duration,
                'modified': datetime.fromtimestamp(stat.st_mtime).astimezone().isoformat(timespec='seconds'),
                'time_map': entry.get('time_map', []), 'timelapse_mode': entry.get('mode')})
    items.sort(key=lambda item: (item['start_ts'], item['camera_id'], item['name']))
    return items


def _validate_literal_source(source: str) -> None:
    import ast
    if len(source) > 100_000:
        raise ValueError('USER_CONTROL.py is too large.')
    tree = ast.parse(source, filename='USER_CONTROL.py')
    for node in tree.body:
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            continue
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            raise ValueError('USER_CONTROL.py accepts only its documentation string and literal assignments; executable code is not allowed.')
        ast.literal_eval(node.value)


def _save_camera(data: dict) -> dict:
    from urllib.parse import urlsplit, urlunsplit, quote as urlquote
    cid = core.safe_id(data.get('id', ''))
    typ = str(data.get('type', 'rtsp')).strip().lower()
    if typ not in ('rtsp', 'cam3'):
        raise ValueError('Camera type must be rtsp or cam3.')
    doc = core.read_json(core.CAMERAS_FILE, {'schema_version': 1, 'cameras': []})
    previous = json.loads(json.dumps(doc))
    existing = next((c for c in doc['cameras'] if c.get('id') == cid), {}) or {}
    previous_media_config = core.media_config_text()

    def text_value(key, default=''):
        # An explicitly submitted blank is a deliberate clear, not a request
        # to resurrect stale values from the previous camera record.
        value = data[key] if key in data else existing.get(key, default)
        return '' if value is None else str(value).strip()

    def int_value(key, default):
        raw = data[key] if key in data else existing.get(key, default)
        try:
            return int(str(raw).strip(), 10)
        except (TypeError, ValueError):
            raise ValueError(f'{key} must be a whole number; received {raw!r}.')

    name = str(data.get('name') or existing.get('name') or cid).strip()[:80]
    enabled_value = data.get('enabled', existing.get('enabled', False))
    enabled = enabled_value if isinstance(enabled_value, bool) else str(enabled_value).lower() in ('1', 'true', 'yes', 'on')
    cam = {
        'id': cid, 'name': name, 'type': typ, 'enabled': enabled,
        'web_admin_url': text_value('web_admin_url'),
        'web_admin_username': text_value('web_admin_username'),
        'web_admin_password': text_value('web_admin_password'),
        'audio_source_url': text_value('audio_source_url'),
        'audio_source_username': text_value('audio_source_username'),
        'audio_source_password': text_value('audio_source_password'),
        'clock_offset_seconds': int_value('clock_offset_seconds', 0),
        'exclude_zones': existing.get('exclude_zones', []),
    }
    if cam['audio_source_url'] and not cam['audio_source_url'].lower().startswith(('rtsp://', 'rtsps://')):
        raise ValueError('External audio URL must start with rtsp:// or rtsps://.')

    if typ == 'rtsp':
        rtsp_url = text_value('rtsp_url')
        username = text_value('username')
        password = text_value('password')
        if not rtsp_url.lower().startswith(('rtsp://', 'rtsps://')):
            raise ValueError('RTSP URL must start with rtsp:// or rtsps://.')
        parts = urlsplit(rtsp_url)
        if parts.username or parts.password:
            # Keep credentials already embedded in the URL exactly as entered.
            pass
        elif username:
            auth = urlquote(username, safe='') + ':' + urlquote(password, safe='') + '@'
            rtsp_url = urlunsplit((parts.scheme, auth + parts.netloc, parts.path, parts.query, parts.fragment))
        cam.update({'rtsp_url': rtsp_url, 'username': username, 'password': password})
        # Deliberately do not retain any CAM3-only UDX attributes on RTSP cams.
    else:
        host = text_value('rtmp_host', '192.168.10.104') or '192.168.10.104'
        port = int_value('rtmp_port', 80)
        if not 1 <= port <= 65535:
            raise ValueError('CAM3 UDX TCP port must be between 1 and 65535.')
        cam.update({
            'rtmp_host': host, 'rtmp_port': port,
            'rtmp_username': text_value('rtmp_username'),
            'rtmp_password': text_value('rtmp_password'),
            'rtmp_stream_name': text_value('rtmp_stream_name', '720p.264') or '720p.264',
        })
        # The CAM3 primary password field is a compatibility mirror only.
        cam['password'] = cam['rtmp_password']

    if existing:
        doc['cameras'] = [cam if c.get('id') == cid else c for c in doc['cameras']]
    else:
        doc['cameras'].append(cam)
    service_keys = ['type', 'enabled', 'audio_source_url', 'audio_source_username', 'audio_source_password']
    if typ == 'rtsp':
        service_keys += ['rtsp_url', 'username', 'password']
    else:
        service_keys += ['rtmp_host', 'rtmp_port', 'rtmp_username', 'rtmp_password', 'rtmp_stream_name']
    service_changed = any((existing.get(k, False) if k == 'enabled' else existing.get(k, '')) != cam.get(k, '') for k in service_keys)
    core.atomic_json(core.CAMERAS_FILE, doc)
    try:
        media_changed = core.media_config_text() != previous_media_config
        if media_changed:
            core.write_media_config()
    except Exception:
        core.atomic_json(core.CAMERAS_FILE, previous)
        raise
    result = _public_camera(cam)
    result['_media_config_changed'] = media_changed
    result['_service_restart'] = service_changed
    return result


def _delete_camera(data: dict) -> None:
    cid = core.safe_id(data.get('id', ''))
    doc = core.read_json(core.CAMERAS_FILE, {'schema_version': 1, 'cameras': []})
    previous = json.loads(json.dumps(doc))
    doc['cameras'] = [c for c in doc['cameras'] if c.get('id') != cid]
    core.atomic_json(core.CAMERAS_FILE, doc)
    try:
        core.write_media_config()
    except Exception:
        core.atomic_json(core.CAMERAS_FILE, previous)
        raise
    core.stop_recorder(cid, graceful=True)
    proc = core.bridge_procs.pop(cid, None)
    core.stop_process(proc, f'{cid} bridge')
    audio_proc = core.audio_mux_procs.pop(cid, None)
    core.stop_process(audio_proc, f'{cid} external audio mux')
    fh = core.bridge_logs.pop(cid, None)
    if fh:
        fh.close()


def _run_job(action: str, cid: str, day: str, mode: str = '') -> None:
    with core.archive_job_lock:
        if core.archive_job['running']:
            raise ValueError('An archive task is already running.')
        core.archive_job.update({'running': True, 'message': f'{action.title()} {cid} / {day}.'})

    def worker():
        try:
            cmd = ['/usr/bin/python3', str(core.APP / 'archive.py'), action, '--camera', cid, '--day', day]
            if mode:
                cmd += ['--mode', mode]
            core.log('EXEC ' + core.redact_command(cmd))
            result = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True, timeout=24 * 3600)
            combined = (result.stdout + '\n' + result.stderr)[-8000:]
            for line in combined.splitlines():
                core.log('ARCHIVE ' + line[:2000])
            with core.archive_job_lock:
                core.archive_job.update({'running': False, 'message': combined, 'returncode': result.returncode})
        except Exception as exc:
            with core.archive_job_lock:
                core.archive_job.update({'running': False, 'message': str(exc), 'returncode': -1})
    threading.Thread(target=worker, daemon=True, name='archive-job').start()


def dispatch(action: str, p: dict) -> dict:
    if action == 'ping':
        return {'ok': True, 'version': (ROOT / 'VERSION').read_text(encoding='utf-8').strip()}
    if action == 'state.get':
        return _state()
    if action == 'cameras.list':
        return {'cameras': [_public_camera(c) for c in core.cameras()]}
    if action == 'cameras.save':
        saved = _save_camera(p)
        media_changed = bool(saved.pop('_media_config_changed', True))
        service_changed = bool(saved.pop('_service_restart', media_changed))
        if service_changed:
            threading.Thread(target=core.restart_media_stack, daemon=True, name='camera-service-restart').start()
        return {'camera': saved, 'media_config_changed': media_changed, 'service_restart': service_changed}
    if action == 'cameras.delete':
        _delete_camera(p)
        threading.Thread(target=core.restart_media_stack, daemon=True, name='media-config-restart').start()
        return {'ok': True}
    if action == 'cameras.test':
        cid = core.safe_id(p.get('camera_id', ''))
        url = f'rtsp://127.0.0.1:8554/{cid}'
        cmd = core.configured_command('camera_test_probe', {
            'ffprobe': str(core.load_control().get('FFPROBE_BINARY', str(core.BUNDLED_FFPROBE))),
            'transport': 'tcp', 'input': url})
        core.log('EXEC ' + core.redact_command(cmd))
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        return {'ok': r.returncode == 0, 'details': r.stdout or r.stderr}
    if action == 'cameras.snapshot':
        cid = core.safe_id(p.get('camera_id', ''))
        url = f'rtsp://127.0.0.1:8554/{cid}'
        cmd = core.configured_command('camera_snapshot', {
            'ffmpeg': str(core.load_control().get('FFMPEG_BINARY', str(core.BUNDLED_FFMPEG))),
            'loglevel': 'error', 'transport': 'tcp', 'input': url, 'quality': '3'})
        core.log('EXEC ' + core.redact_command(cmd))
        r = subprocess.run(cmd, capture_output=True, timeout=12)
        if r.returncode or not r.stdout:
            raise RuntimeError('Camera snapshot unavailable: ' + r.stderr.decode(errors='replace')[-1200:])
        return {'png_base64': base64.b64encode(r.stdout).decode('ascii')}
    if action == 'bookmarks.list':
        return {'bookmarks': core.bookmarks()}
    if action == 'bookmarks.save':
        ctl = core.load_control()
        if not ctl.get('ENABLE_PLAYBACK_BOOKMARKS', True):
            raise ValueError('Bookmarks are disabled in USER_CONTROL.py.')
        protected = True  # Every cue protects its selected footage by default in v2.0.
        if protected and not ctl.get('ENABLE_PROTECTED_INTERVALS', True):
            raise ValueError('Protected intervals are disabled in USER_CONTROL.py.')
        entry = {
            'id': str(p.get('id') or f"mark-{int(time.time() * 1000)}"),
            'camera_id': core.safe_id(p.get('camera_id', '')),
            'camera_ids': list(dict.fromkeys(core.safe_id(x) for x in p.get('camera_ids', [p.get('camera_id', '')]))),
            'start': str(p.get('start', '')), 'end': str(p.get('end', '')),
            'label': str(p.get('label', 'Bookmark'))[:100],
            'notes': str(p.get('notes', ''))[:1000], 'protected': protected,
        }
        a = datetime.fromisoformat(entry['start'])
        b = datetime.fromisoformat(entry['end'])
        if b <= a:
            raise ValueError('Bookmark end must be after start.')
        doc = core.read_json(core.BOOKMARKS_FILE, {'schema_version': 1, 'bookmarks': []})
        doc['bookmarks'].append(entry)
        core.atomic_json(core.BOOKMARKS_FILE, doc)
        return {'bookmark': entry}
    if action == 'bookmarks.update':
        doc = core.read_json(core.BOOKMARKS_FILE, {'schema_version': 1, 'bookmarks': []})
        item = next((b for b in doc.get('bookmarks', []) if b.get('id') == p.get('id')), None)
        if not item:
            raise ValueError('Cue not found.')
        item['label'] = str(p.get('label', item.get('label', 'Cue')))[:100]
        item['notes'] = str(p.get('notes', item.get('notes', '')))[:1000]
        # Cue protection cannot be silently switched off by an edit.
        item['protected'] = True
        item['camera_ids'] = list(dict.fromkeys(core.safe_id(x) for x in item.get('camera_ids', [item.get('camera_id', '')])))
        core.atomic_json(core.BOOKMARKS_FILE, doc)
        return {'bookmark': item}
    if action == 'bookmarks.delete':
        doc = core.read_json(core.BOOKMARKS_FILE, {'schema_version': 1, 'bookmarks': []})
        doc['bookmarks'] = [b for b in doc['bookmarks'] if b.get('id') != p.get('id')]
        core.atomic_json(core.BOOKMARKS_FILE, doc)
        return {'ok': True}
    if action == 'exports.start':
        import uuid
        with export_job_lock:
            if export_job.get('state') == 'RUNNING':
                raise ValueError('An export is already running. Wait for it to finish.')
            # Extend the selected cue's protection to every camera used by this
            # synchronized section, and persist it before the export worker starts.
            cue_doc = core.read_json(core.BOOKMARKS_FILE, {'schema_version': 1, 'bookmarks': []})
            by_id = {x.get('id'): x for x in cue_doc.get('bookmarks', [])}
            for section in p.get('sections', []):
                cue = by_id.get(str(section.get('cue_id', '')))
                if not cue:
                    raise ValueError('One selected cue no longer exists. Refresh Export Studio.')
                cameras_for_cue = list(dict.fromkeys(core.safe_id(x) for x in section.get('camera_ids', [])))
                cue['camera_ids'] = list(dict.fromkeys([core.safe_id(cue.get('camera_id', ''))] + list(cue.get('camera_ids', [])) + cameras_for_cue))
                cue['protected'] = True
            core.atomic_json(core.BOOKMARKS_FILE, cue_doc)
            job_id = uuid.uuid4().hex[:12]
            export_job.update({'id': job_id, 'state': 'RUNNING', 'message': 'Validating export request…',
                               'completed_sections': 0, 'total_sections': len(p.get('sections', [])),
                               'output': '', 'duration_seconds': 0.0, 'time_lapse_sources_used': []})
        threading.Thread(target=_run_export_job, args=(dict(p), job_id), daemon=True, name='cue-export').start()
        return {'id': job_id, 'state': 'RUNNING'}
    if action == 'exports.status':
        with export_job_lock:
            return dict(export_job)
    if action == 'zones.save':
        if not core.load_control().get('ENABLE_DETECTION_EXCLUSION_ZONES', True):
            raise ValueError('Detection exclusion zones are disabled in USER_CONTROL.py.')
        cid = core.safe_id(p.get('camera_id', ''))
        polygons = p.get('polygons', [])
        if not isinstance(polygons, list) or len(polygons) > 64:
            raise ValueError('Provide at most 64 polygons.')
        for poly in polygons:
            if not isinstance(poly, list) or len(poly) < 3 or len(poly) > 100:
                raise ValueError('Each polygon needs 3-100 points.')
            for point in poly:
                if len(point) != 2 or not all(isinstance(v, (int, float)) and 0 <= v <= 1 for v in point):
                    raise ValueError('Polygon coordinates must be normalized between 0 and 1.')
        doc = core.read_json(core.CAMERAS_FILE, {'schema_version': 1, 'cameras': []})
        cam = next((c for c in doc['cameras'] if c['id'] == cid), None)
        if not cam:
            raise ValueError('Camera not found.')
        cam['exclude_zones'] = polygons
        core.atomic_json(core.CAMERAS_FILE, doc)
        return {'polygons': polygons}
    if action == 'appearance.get':
        saved = core.read_json(core.APPEARANCE_FILE, core.APPEARANCE_DEFAULTS)
        return core.validate_appearance(saved)
    if action == 'appearance.save':
        clean = core.validate_appearance(p)
        tmp = core.APPEARANCE_FILE.with_suffix('.json.tmp')
        tmp.write_text(json.dumps(clean, indent=2) + '\n', encoding='utf-8')
        os.chmod(tmp, 0o600)
        os.replace(tmp, core.APPEARANCE_FILE)
        return {'appearance': clean}
    if action == 'user-control.get':
        return {'source': core.USER_CONTROL.read_text(encoding='utf-8')}
    if action == 'user-control.save':
        source = str(p.get('source', ''))
        _validate_literal_source(source)
        old = core.USER_CONTROL.read_text(encoding='utf-8')
        tmp = core.USER_CONTROL.with_suffix('.py.tmp')
        tmp.write_text(source, encoding='utf-8')
        os.chmod(tmp, 0o600)
        os.replace(tmp, core.USER_CONTROL)
        try:
            core.load_control()
            core.write_media_config()
        except Exception:
            core.USER_CONTROL.write_text(old, encoding='utf-8')
            os.chmod(core.USER_CONTROL, 0o600)
            core.load_control()
            raise
        return {'ok': True}
    if action == 'commands.get':
        return core.read_json(core.COMMANDS_FILE, {})
    if action == 'commands.save':
        obj = p.get('commands')
        if not isinstance(obj, dict) or obj.get('schema_version') != 1:
            raise ValueError('Invalid command file schema.')
        required = {
            'timelapse_regular': '{ffmpeg}', 'archive_clip': '{ffmpeg}',
            'archive_join': '{ffmpeg}',
            'timelapse_intelligent': '{ffmpeg}', 'archive_probe_duration': '{ffprobe}',
            'archive_verify_probe': '{ffprobe}', 'archive_verify_decode': '{ffmpeg}',
            'timelapse_motion_scan': '{ffmpeg}', 'timelapse_source_probe': '{ffprobe}',
            'playback_codec_probe': '{ffprobe}', 'playback_prepare_copy': '{ffmpeg}',
            'playback_prepare_transcode': '{ffmpeg}', 'camera_snapshot': '{ffmpeg}',
            'camera_test_probe': '{ffprobe}', 'cam3_publish': '{ffmpeg}', 'ffmpeg_version': '{ffmpeg}',
            'audio_mux_publish': '{ffmpeg}',
        }
        for key, executable in required.items():
            args = obj.get(key)
            if not isinstance(args, list) or len(args) < 2 or not all(isinstance(x, str) and len(x) < 1000 for x in args):
                raise ValueError(f'Command {key} must be a non-empty argument array.')
            if args[0] != executable:
                raise ValueError(f'Command {key} must begin with {executable}.')
        tmp = core.COMMANDS_FILE.with_suffix('.json.tmp')
        tmp.write_text(json.dumps(obj, indent=2) + '\n', encoding='utf-8')
        os.chmod(tmp, 0o600)
        os.replace(tmp, core.COMMANDS_FILE)
        return {'ok': True}
    if action == 'recordings.list':
        return {'files': _recordings()}
    if action == 'archive.run':
        cid = core.safe_id(p.get('camera_id', ''))
        day = str(p.get('day', ''))
        datetime.strptime(day, '%Y-%m-%d')
        task = str(p.get('task', 'build'))
        mode = str(p.get('mode', ''))
        if task not in ('build', 'estimate', 'timelapse'):
            raise ValueError('Unknown archive task.')
        if task == 'build' and not core.load_control().get('ENABLE_DAILY_ARCHIVE', True):
            raise ValueError('Daily archive generation is disabled in USER_CONTROL.py.')
        if task == 'timelapse' and mode not in ('regular', 'intelligent'):
            raise ValueError('Invalid time-lapse mode.')
        _run_job(task, cid, day, mode)
        return {'message': f'{task.title()} job queued for {cid} / {day}.'}
    if action == 'purge.preview':
        cmd = [sys.executable, str(core.APP / 'archive.py'), 'purge', '--preview']
        r = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True, timeout=120)
        return {'ok': r.returncode == 0, 'report': r.stdout + r.stderr}
    if action == 'ffmpeg.version':
        cmd = core.configured_command('ffmpeg_version', {'ffmpeg': str(core.load_control().get('FFMPEG_BINARY', str(core.BUNDLED_FFMPEG)))})
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=8)
        return {'ok': r.returncode == 0, 'details': r.stdout or r.stderr}
    if action == 'logs.tail':
        count = max(1, min(int(p.get('lines', 100)), 1000))
        output = []
        for path in (core.LOGS / 'application.log', core.LOGS / 'mediamtx.log'):
            if path.exists():
                try:
                    lines = path.read_text(errors='replace').splitlines()[-count:]
                    output.append(f'===== {path.name} =====')
                    output.extend(lines)
                except OSError as exc:
                    output.append(f'{path.name}: {exc}')
        return {'text': '\n'.join(output)}
    if action == 'service.stop':
        shutdown_event.set()
        return {'ok': True}
    raise ValueError(f'Unknown operation: {action}')


class RPCHandler(socketserver.StreamRequestHandler):
    def handle(self):
        try:
            line = self.rfile.readline(RPC_LIMIT + 1)
            if not line or len(line) > RPC_LIMIT:
                raise ValueError('Invalid or oversized request.')
            request = json.loads(line.decode('utf-8'))
            action = str(request.get('action', ''))
            params = request.get('params') or {}
            if not isinstance(params, dict):
                raise ValueError('Request parameters must be an object.')
            result = dispatch(action, params)
            response = {'ok': True, 'result': result}
        except Exception as exc:
            response = {'ok': False, 'error': f'{type(exc).__name__}: {exc}'}
        raw = json.dumps(response, ensure_ascii=False).encode('utf-8') + b'\n'
        self.wfile.write(raw)


class UnixRPCServer(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True
    allow_reuse_address = True


def shutdown_engine(signum=None, frame=None):
    shutdown_event.set()


def main():
    core.ensure_layout()
    SOCKET_PATH.parent.mkdir(parents=True, exist_ok=True)
    try:
        SOCKET_PATH.unlink()
    except FileNotFoundError:
        pass
    threading.Thread(target=core.supervisor, daemon=True, name='media-supervisor').start()
    threading.Thread(target=core.archive_scheduler, daemon=True, name='archive-scheduler').start()
    threading.Thread(target=camera_probe_scheduler, daemon=True, name='camera-probe').start()
    threading.Thread(target=core.control_file_watcher, daemon=True, name='settings-watcher').start()
    server = UnixRPCServer(str(SOCKET_PATH), RPCHandler)
    os.chmod(SOCKET_PATH, 0o660)
    signal.signal(signal.SIGTERM, shutdown_engine)
    signal.signal(signal.SIGINT, shutdown_engine)
    core.log(f'Native CCTV engine ready. Local control socket: {SOCKET_PATH}')
    try:
        while not shutdown_event.is_set():
            server.timeout = 0.5
            server.handle_request()
    finally:
        server.server_close()
        try:
            SOCKET_PATH.unlink(missing_ok=True)
        except OSError:
            pass
        core.stop_recorders(graceful=True)
        core.stop_audio_muxers()
        for proc in list(core.bridge_procs.values()):
            core.stop_process(proc, 'CAM3 bridge')
        for fh in list(core.bridge_logs.values()):
            try:
                fh.close()
            except Exception:
                pass
        core.stop_process(core.media_proc, 'MediaMTX')


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        core.log(f'FATAL ENGINE ERROR: {type(exc).__name__}: {exc}')
        raise
