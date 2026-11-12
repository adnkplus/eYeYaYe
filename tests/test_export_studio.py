from __future__ import annotations
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'app'))
import core
import export
import runtime_paths


@unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'FFmpeg/FFprobe are required for media integration test')
class ExportStudioIntegrationTests(unittest.TestCase):
    def test_ordered_three_and_two_camera_sections_create_verified_h264_aac_mp4(self):
        temp = Path(tempfile.mkdtemp(prefix='cctv-export-test-'))
        core_names = ('ROOT', 'APP', 'CONFIG', 'DATA', 'LOGS', 'USER_CONTROL', 'CAMERAS_FILE', 'BOOKMARKS_FILE', 'COMMANDS_FILE', 'CAM3', 'CAM3_BUNDLE', 'BIN_DIR', 'MEDIAMTX_BIN', 'BUNDLED_FFMPEG', 'BUNDLED_FFPROBE')
        export_names = ('ROOT', 'OUTPUT', 'WORK')
        old_core = {name: getattr(core, name) for name in core_names}
        old_export = {name: getattr(export, name) for name in export_names}
        runtime_names = ('ROOT', 'CAM3_BUNDLE', 'BIN_DIR')
        old_runtime = {name: getattr(runtime_paths, name) for name in runtime_names}
        try:
            mapping = {
                'ROOT': temp, 'APP': temp / 'app', 'CONFIG': temp / 'config',
                'DATA': temp / 'data', 'LOGS': temp / 'logs',
                'USER_CONTROL': ROOT / 'USER_CONTROL.py',
                'CAMERAS_FILE': temp / 'config' / 'cameras.json',
                'BOOKMARKS_FILE': temp / 'config' / 'bookmarks.json',
                'COMMANDS_FILE': ROOT / 'control' / 'COMMANDS.json',
            }
            for key, value in mapping.items():
                setattr(core, key, value)
            bundle = temp / 'cam3' / 'CAM3_LIVE_STANDALONE'
            bin_dir = bundle / 'bin'
            bin_dir.mkdir(parents=True, exist_ok=True)
            (bin_dir / 'ffmpeg').symlink_to(shutil.which('ffmpeg'))
            (bin_dir / 'ffprobe').symlink_to(shutil.which('ffprobe'))
            (bin_dir / 'mediamtx').write_text('test-only placeholder\n', encoding='utf-8')
            core.CAM3 = temp / 'cam3'
            core.CAM3_BUNDLE = bundle
            core.BIN_DIR = bin_dir
            core.MEDIAMTX_BIN = bin_dir / 'mediamtx'
            core.BUNDLED_FFMPEG = bin_dir / 'ffmpeg'
            core.BUNDLED_FFPROBE = bin_dir / 'ffprobe'
            runtime_paths.ROOT = temp
            runtime_paths.CAM3_BUNDLE = bundle
            runtime_paths.BIN_DIR = bin_dir
            export.ROOT = temp
            export.OUTPUT = temp / 'data' / 'exports'
            export.WORK = temp / 'cache' / 'export-work'
            (temp / 'config').mkdir(parents=True)
            (temp / 'logs').mkdir()
            cameras = [{'id': f'cam{i}'} for i in (1, 2, 3)]
            (temp / 'config' / 'cameras.json').write_text(json.dumps({'cameras': cameras}), encoding='utf-8')
            now = datetime.now().astimezone().replace(microsecond=0)
            stamp = now.strftime('%H%M%S_%d%m%y')
            for index in (1, 2, 3):
                folder = temp / 'data' / 'recordings' / 'original' / f'cam{index}'
                folder.mkdir(parents=True, exist_ok=True)
                output = folder / f'C{index}_RAW_{stamp}.ts'
                command = [
                    shutil.which('ffmpeg'), '-y', '-hide_banner', '-loglevel', 'error',
                    '-f', 'lavfi', '-i', f'color=c={"red" if index == 1 else "green" if index == 2 else "blue"}:s=320x180:r=8',
                    '-f', 'lavfi', '-i', f'sine=frequency={400 + index * 100}:sample_rate=48000',
                    '-t', '7', '-c:v', 'libx264', '-preset', 'ultrafast', '-g', '8',
                    '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-f', 'mpegts', str(output),
                ]
                subprocess.run(command, check=True, capture_output=True, timeout=45)
            cue1_start = (now + timedelta(seconds=0.5)).isoformat(timespec='seconds')
            cue1_end = (now + timedelta(seconds=2.5)).isoformat(timespec='seconds')
            cue2_start = (now + timedelta(seconds=3.0)).isoformat(timespec='seconds')
            cue2_end = (now + timedelta(seconds=5.0)).isoformat(timespec='seconds')
            (temp / 'config' / 'bookmarks.json').write_text(json.dumps({'bookmarks': [
                {'id': 'cue-three', 'camera_id': 'cam1', 'start': cue1_start, 'end': cue1_end, 'label': 'Three camera section', 'protected': True},
                {'id': 'cue-two', 'camera_id': 'cam1', 'start': cue2_start, 'end': cue2_end, 'label': 'Two camera section', 'protected': True},
            ]}), encoding='utf-8')
            result = export.run_export({
                'sections': [
                    {'cue_id': 'cue-three', 'camera_ids': ['cam1', 'cam2', 'cam3']},
                    {'cue_id': 'cue-two', 'camera_ids': ['cam1', 'cam2']},
                ],
                'settings': {'resolution': '1280x720', 'fps': 25, 'crf': 30, 'preset': 'ultrafast',
                             'audio_mode': 'mix', 'allow_timelapse': False},
            })
            final_path = temp / result['output']
            self.assertTrue(final_path.is_file())
            self.assertGreater(final_path.stat().st_size, 2048)
            probe = subprocess.run([shutil.which('ffprobe'), '-v', 'error', '-show_entries',
                                    'format=duration:stream=codec_name,codec_type,width,height,pix_fmt',
                                    '-of', 'json', str(final_path)], check=True, capture_output=True, text=True)
            info = json.loads(probe.stdout)
            streams = info['streams']
            video = next(x for x in streams if x['codec_type'] == 'video')
            self.assertEqual((video['width'], video['height']), (1280, 720))
            self.assertEqual(video['codec_name'], 'h264')
            self.assertEqual(video['pix_fmt'], 'yuv420p')
            self.assertTrue(any(x['codec_type'] == 'audio' and x['codec_name'] == 'aac' for x in streams))
            manifest = json.loads(final_path.with_suffix('.manifest.json').read_text(encoding='utf-8'))
            self.assertEqual([x['cue_id'] for x in manifest['sections']], ['cue-three', 'cue-two'])
            self.assertEqual(manifest['sections'][0]['camera_ids'], ['cam1', 'cam2', 'cam3'])
            self.assertEqual(manifest['sections'][1]['camera_ids'], ['cam1', 'cam2'])
            self.assertFalse(list(export.WORK.glob('*')), 'successful export should clean temporary working files')
        finally:
            for key, value in old_core.items():
                setattr(core, key, value)
            for key, value in old_export.items():
                setattr(export, key, value)
            for key, value in old_runtime.items():
                setattr(runtime_paths, key, value)
            shutil.rmtree(temp, ignore_errors=True)


if __name__ == '__main__':
    unittest.main()
