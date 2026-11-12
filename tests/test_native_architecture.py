from __future__ import annotations
import ast
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'app'))
import core
import engine
import archive
import gui


class NativeArchitectureTests(unittest.TestCase):
    def test_engine_rpc_reports_release_version(self):
        self.assertEqual(engine.dispatch('ping', {})['version'], (ROOT / 'VERSION').read_text().strip())

    def test_native_desktop_has_no_web_frontend_or_http_api(self):
        for rel in ('app/server.py', 'app/index.html', 'app/static'):
            self.assertFalse((ROOT / rel).exists(), rel)
        master = (ROOT / 'master.py').read_text(encoding='utf-8')
        self.assertNotIn('docker compose', master.lower())
        gui = (ROOT / 'app/gui.py').read_text(encoding='utf-8')
        self.assertIn('tkinter', gui)
        self.assertIn('socket.AF_UNIX', gui)
        self.assertNotIn('http://127.0.0.1', gui)
        self.assertEqual(gui.count('webbrowser.open(url)'), 1)

    def test_mpv_is_the_continuous_video_backend_not_frame_extraction(self):
        gui = (ROOT / 'app/gui.py').read_text(encoding='utf-8')
        mpv_class = gui[gui.index('class MpvPlayer:'):gui.index('class VideoTile:')]
        tile_class = gui[gui.index('class VideoTile:'):gui.index('class CCTVDesktop:')]
        self.assertIn('--wid=', mpv_class)
        self.assertIn('--input-ipc-server=', mpv_class)
        self.assertIn("'loadfile'", mpv_class)
        self.assertIn('MpvPlayer(', tile_class)
        self.assertNotIn('image2pipe', mpv_class + tile_class)
        self.assertNotIn('PhotoImage', mpv_class + tile_class)
        self.assertNotIn('playback_frame', gui)
        self.assertNotIn('live_stream', gui)
        self.assertIn("WHEEL_UP seek 5 exact", gui)
        self.assertIn("WHEEL_DOWN seek -5 exact", gui)
        self.assertIn("bind('<MouseWheel>', self._speed_wheel)", tile_class)
        self.assertIn("bind('<MouseWheel>', self._volume_wheel)", tile_class)
        self.assertIn('def toggle_tile_fullscreen', gui)
        self.assertIn('--audio=yes', gui)
        self.assertIn('--aid=auto', gui)
        self.assertNotIn('--audio-display=no', gui)
        self.assertIn('def get_many(self, property_names', gui)
        self.assertIn('STREAM STALLED', gui)
        self.assertIn("'playback-time', 'time-pos'", gui)
        self.assertIn("--vf=@eyeyaye-fps:lavfi=[fps={int(self.fps)}]", gui)
        self.assertIn("GUI_PLAYBACK_FPS_DEFAULT", gui)
        self.assertIn("--vf=@eyeyaye-fps:lavfi=[fps={int(self.fps)}]", gui)
        self.assertIn("GUI_PLAYBACK_FPS_DEFAULT", gui)
        self.assertIn("--vf=@eyeyaye-fps:lavfi=[fps={int(self.fps)}]", gui)
        self.assertIn("GUI_PLAYBACK_FPS_DEFAULT", gui)

    def test_independent_tiles_layouts_audio_speed_and_fps(self):
        gui = (ROOT / 'app/gui.py').read_text(encoding='utf-8')
        self.assertIn("self.picker = ttk.Combobox", gui)
        self.assertIn("self.player.set('volume',self.volume)", gui)
        self.assertIn("self.player.set('speed',self.speed)", gui)
        self.assertIn("self.player.command(['cycle','aid'])", gui)
        self.assertIn('def apply_master_volume', gui)
        self.assertIn('def apply_master_speed', gui)
        self.assertIn('def apply_master_fps', gui)
        self.assertIn('self.live_cols_var', gui)
        self.assertIn('self.live_rows_var', gui)
        self.assertIn('self.play_cols_var', gui)
        self.assertIn('self.play_rows_var', gui)
        self.assertIn("def _apply_grid_layout(self, kind):", gui)
        self.assertIn("'live_grid'", gui)
        self.assertIn("'playback_grid'", gui)
        self.assertNotIn('LIVE_LAYOUTS', gui)
        self.assertIn("['vf', 'add'", gui)
        self.assertIn("['vf', 'remove'", gui)
        self.assertIn('def toggle_stats', gui)
        self.assertIn('video-bitrate', gui)
        self.assertIn('audio-codec-name', gui)
        self.assertIn('Show Playback', gui)
        self.assertIn('Show Live Dock', gui)
        self.assertIn('target = cams[:min(capacity, len(cams))]', gui)
        self.assertIn("parent.grid_rowconfigure(row, weight=1", gui)
        self.assertIn("'--vo=gpu,x11'", gui)
        playback_start = gui[gui.index('    def start(self, source='):gui.index('    def start_live_pipe(')]
        self.assertIn("'--demuxer=lavf'", playback_start)
        self.assertIn("'--demuxer=lavf'", gui)
        self.assertIn("'--demuxer-lavf-format=matroska'", gui)
        self.assertIn('self.player.start_live_pipe(pipe_path', gui)
        self.assertNotIn('height=190', gui)
        self.assertNotIn('self.video.pack_propagate(False)', gui)
        self.assertIn('def _player_log_excerpt(self):', gui)

    def test_mediamtx_routes_only_and_disables_its_builtin_recorder(self):
        cameras = [
            {'id':'cam1','type':'rtsp','rtsp_url':'rtsp://192.0.2.11:554','username':'admin','password':'pass'},
            {'id':'cam2','type':'rtsp','rtsp_url':'rtsp://192.0.2.12:554','username':'admin','password':'pass'},
            {'id':'cam3','type':'cam3','rtmp_host':'192.0.2.13'},
        ]
        controls = {'RECORDING_SEGMENT_SECONDS':600,'ENABLE_ORIGINAL_RECORDING':True,
                    'RECORDING_FORMAT':'mkv','CAMERA_DEFAULT_RTSP_TRANSPORT':'tcp'}
        with mock.patch.object(core, 'cameras', return_value=cameras), mock.patch.object(core, 'load_control', return_value=controls):
            text = core.media_config_text()
        self.assertIn('record: false', text)
        self.assertNotIn('recordPath:', text)
        self.assertNotIn('recordFormat:', text)
        self.assertNotIn('recordSegmentDuration:', text)
        for cid in ('cam1','cam2','cam3'):
            self.assertIn(f'  {cid}:', text)
        self.assertIn('source: publisher', text)
        self.assertIn('rtsp://admin:pass@192.0.2.11:554', text)
        self.assertIn('rtsp://admin:pass@192.0.2.12:554', text)

    def test_ffmpeg_recorder_uses_one_camera_input_two_copy_outputs(self):
        from pathlib import Path
        cmd = core.build_recording_command(
            'cam1', input_url='rtsp://127.0.0.1:8554/cam1',
            output_dir=Path('/tmp/original/cam1'), pipe_path=Path('/tmp/live/cam1.mkvpipe'),
            control={'FFMPEG_BINARY':'/usr/bin/ffmpeg','RECORDING_SEGMENT_SECONDS':600,
                     'RECORDING_FORMAT':'mkv','LIVE_PIPE_QUEUE_PACKETS':512,
                     'CAMERA_DEFAULT_RTSP_TRANSPORT':'tcp'})
        self.assertEqual(cmd[cmd.index('-c:v')+1], 'copy')
        self.assertEqual(cmd[cmd.index('-c:a')+1], 'copy')
        self.assertEqual(cmd[cmd.index('-fps_mode:v')+1], 'passthrough')
        self.assertIn('-copyinkf', cmd)
        self.assertNotIn('-use_wallclock_as_timestamps', cmd)
        self.assertEqual(cmd[cmd.index('-map')+1], '0:v:0')
        self.assertIn('0:a?', cmd)
        self.assertEqual(cmd[cmd.index('-f')+1], 'tee')
        tee = cmd[-1]
        self.assertIn('segment_format=matroska', tee)
        self.assertIn('segment_time=600', tee)
        self.assertIn('%H%M%S_%d%m%Y_RAW_C1.mkv', tee)
        self.assertIn('fifo_format=matroska', tee)
        self.assertIn('drop_pkts_on_overflow=1', tee)
        self.assertIn('onfail=ignore', tee)
        self.assertNotIn('libx264', cmd)
        self.assertNotIn('aac', cmd)

    def test_camera_credentials_and_cam3_protocol_client(self):
        cams = json.loads((ROOT / 'config' / 'cameras.json').read_text(encoding='utf-8'))['cameras']
        self.assertEqual([c['id'] for c in cams], ['cam1', 'cam2', 'cam3'])
        self.assertEqual(cams[0]['rtsp_url'], 'rtsp://admin:sansplomb95@192.168.10.102:554')
        self.assertEqual(cams[1]['rtsp_url'], 'rtsp://admin:sansplomb95@192.168.10.101:554')
        for cam in cams[:2]:
            self.assertEqual(cam['username'], 'admin')
            self.assertEqual(cam['password'], 'sansplomb95')
        client_path = ROOT / 'cam3' / 'rtmp_cam.py'
        source = client_path.read_text(encoding='utf-8')
        ast.parse(source)
        namespace = {}
        cam3_env = {'CAM3_HOST':'192.168.10.104','CAM3_PORT':'80','CAM3_USERNAME':'admin','CAM3_PASSWORD':'sansplomb95','CAM3_STREAM_NAME':'720p.264'}
        with mock.patch.dict(os.environ, cam3_env):
            exec(compile(source, str(client_path), 'exec'), namespace)
        for name in ('open_rtmp','setup','initial_connect','recv_for','extract_challenge','send_login','a_str','a_num','a_null','send_msg'):
            self.assertTrue(callable(namespace.get(name)), name)
        self.assertEqual(namespace['HOST'], '192.168.10.104')
        self.assertEqual(namespace['PORT'], 80)
        self.assertEqual(namespace['USERNAME'], 'admin')
        self.assertEqual(namespace['PASSWORD'], 'sansplomb95')
        self.assertEqual(cams[2]['rtmp_host'], '192.168.10.104')
        self.assertEqual(cams[2]['rtmp_username'], 'admin')
        self.assertEqual(cams[2]['rtmp_password'], 'sansplomb95')
        self.assertEqual(namespace['STREAM_NAME'], '720p.264')

    def test_camera_admin_pages_are_the_only_http_browser_exception(self):
        gui = (ROOT / 'app/gui.py').read_text(encoding='utf-8')
        self.assertEqual(gui.count('webbrowser.open(url)'), 1)
        cams = json.loads((ROOT / 'config' / 'cameras.json').read_text(encoding='utf-8'))['cameras']
        for cam in cams:
            self.assertTrue(cam['web_admin_url'].startswith('http://192.168.10.'))

    def test_local_camera_editor_receives_existing_credentials(self):
        result = engine._public_camera({'id':'cam1','password':'visible','web_admin_password':'web-secret','audio_source_password':'audio-secret'})
        self.assertEqual(result['password'], 'visible')
        self.assertEqual(result['web_admin_password'], 'web-secret')
        self.assertEqual(result['audio_source_password'], 'audio-secret')
        self.assertEqual(result['live_url'], 'rtsp://127.0.0.1:8554/cam1')

    def test_legacy_split_rtsp_credentials_migrate_into_url(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            target=Path(tmp)/'cameras.json'
            target.write_text(json.dumps({'schema_version':1,'cameras':[{
                'id':'cam1','type':'rtsp','rtsp_url':'rtsp://192.0.2.10:554',
                'username':'admin','password':'pw'
            }]}))
            with mock.patch.object(core,'CAMERAS_FILE',target):
                core.migrate_camera_credentials_into_urls()
            migrated=json.loads(target.read_text())['cameras'][0]
            self.assertEqual(migrated['rtsp_url'],'rtsp://admin:pw@192.0.2.10:554')
            self.assertEqual(core.camera_source(migrated),'rtsp://admin:pw@192.0.2.10:554')

    def test_camera_editor_shows_udx_fields_only_for_cam3(self):
        gui = (ROOT / 'app/gui.py').read_text(encoding='utf-8')
        editor = gui[gui.index('    def edit_camera(self, camera=None):'):gui.index('    def _delete_selected_camera(self):')]
        cam3_branch = editor.index("if typ == 'cam3':")
        rtsp_branch = editor.index('            else:', cam3_branch)
        udx_host = editor.index("('rtmp_host', 'CAM3 UDX host')", cam3_branch)
        udx_port = editor.index("('rtmp_port', 'CAM3 UDX TCP port')", cam3_branch)
        rtsp_url = editor.index("('rtsp_url', 'RTSP URL", rtsp_branch)
        self.assertLess(cam3_branch, udx_host)
        self.assertLess(udx_host, rtsp_branch)
        self.assertLess(udx_port, rtsp_branch)
        self.assertLess(rtsp_branch, rtsp_url)

    def test_camera_editor_blank_optional_port_preserves_default_and_full_url_auth(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            target=Path(tmp)/'cameras.json'
            target.write_text(json.dumps({'schema_version':1,'cameras':[{
                'id':'cam1','name':'CAM1','type':'rtsp','enabled':True,
                'rtsp_url':'rtsp://admin:sansplomb95@192.168.10.102:554',
                'username':'admin','password':'sansplomb95','rtmp_port':80,
                'rtmp_password':'','clock_offset_seconds':0,'exclude_zones':[]
            }]}))
            with mock.patch.object(core,'CAMERAS_FILE',target), mock.patch.object(core,'write_media_config'):
                saved=engine._save_camera({
                    'id':'cam1','name':'CAM1','type':'rtsp','enabled':True,
                    'rtsp_url':'rtsp://admin:sansplomb95@192.168.10.102:554',
                    'username':'admin','password':'sansplomb95','rtmp_port':'',
                    'rtmp_username':'','rtmp_password':'','rtmp_stream_name':'720p.264',
                    'web_admin_url':'','web_admin_username':'','web_admin_password':'',
                    'audio_source_url':'','audio_source_username':'','audio_source_password':''
                })
                saved_doc=json.loads(target.read_text())
                cam=saved_doc['cameras'][0]
                self.assertNotIn('rtmp_port', cam)
                self.assertNotIn('rtmp_host', cam)
                self.assertNotIn('rtmp_password', cam)
                self.assertEqual(cam['rtsp_url'],'rtsp://admin:sansplomb95@192.168.10.102:554')
                self.assertEqual(cam['password'],'sansplomb95')
                self.assertEqual(saved['password'],'sansplomb95')
                self.assertFalse(saved['_media_config_changed'])
                self.assertFalse(saved['_service_restart'])

    def test_playback_grid_uses_real_variable_names_and_persists(self):
        gui=(ROOT/'app'/'gui.py').read_text(encoding='utf-8')
        self.assertIn("prefix = 'play' if kind == 'playback' else 'live'",gui)
        self.assertIn("prefix = 'play' if kind == 'playback' else 'live'",gui)
        self.assertIn("getattr(self, f'{prefix}_cols_var')",gui)
        self.assertIn("getattr(self, f'{prefix}_rows_var')",gui)

    def test_mediamtx_is_shared_rtsp_router_only_and_recording_is_ffmpeg_owned(self):
        text = core.media_config_text()
        self.assertIn('rtsp: true', text)
        self.assertIn('rtspAddress: :8554', text)
        for disabled in ('rtmp: false','hls: false','webrtc: false','srt: false','api: false','metrics: false','pprof: false'):
            self.assertIn(disabled, text)
        self.assertIn('record: false', text)
        self.assertNotIn('recordPath:', text)
        self.assertNotIn('recordSegmentDuration:', text)
        self.assertIn('source: publisher', text)
        self.assertIn('rtsp://admin:sansplomb95@192.168.10.102:554', text)
        self.assertIn('rtsp://admin:sansplomb95@192.168.10.101:554', text)

    def test_external_audio_is_optional_separate_input_and_video_stream_copy(self):
        cam = {'audio_source_url':'rtsp://10.0.0.5:554/audio','audio_source_username':'listener','audio_source_password':'pw'}
        self.assertEqual(core.external_audio_source(cam), 'rtsp://listener:pw@10.0.0.5:554/audio')
        commands = json.loads((ROOT / 'control' / 'COMMANDS.json').read_text(encoding='utf-8'))
        cmd = commands['audio_mux_publish']
        self.assertIn('0:v:0', cmd)
        self.assertIn('1:a:0', cmd)
        self.assertIn('copy', cmd)
        self.assertIn('{audio_codec}', cmd)
        cam3 = commands['cam3_publish']
        self.assertIn('0:a?', cam3)  # Copy audio when the CAM3 input supplies it.
        self.assertIn('copy', cam3)

    def test_command_catalog_contains_no_legacy_live_or_playback_frame_pipeline(self):
        commands = json.loads((ROOT / 'control' / 'COMMANDS.json').read_text(encoding='utf-8'))
        self.assertNotIn('live_stream', commands)
        self.assertNotIn('playback_frame', commands)
        self.assertIn('camera_test_probe', commands)
        self.assertIn('archive_verify_decode', commands)
        self.assertIn('image2pipe', commands['camera_snapshot'])  # Isolated one-shot zone calibration only.

    def test_recording_filename_codes_day_boundary_and_retention_defaults(self):
        control = {}
        tree = ast.parse((ROOT / 'USER_CONTROL.py').read_text(encoding='utf-8'))
        for node in tree.body:
            if isinstance(node, ast.Assign):
                try: value = ast.literal_eval(node.value)
                except Exception: continue
                for target in node.targets:
                    if isinstance(target, ast.Name): control[target.id] = value
        self.assertEqual(control['ARCHIVE_DAY_START'], '05:00')
        self.assertEqual(control['ARCHIVE_AUTO_BUILD_DELAY_DAYS'], 2)
        self.assertEqual(control['PURGE_ORIGINAL_RETENTION_DAYS'], 2)
        self.assertNotIn('RECORDING_ORIGINAL_RETENTION_DAYS', control)
        self.assertFalse(control['PURGE_ENABLED'])
        self.assertTrue(control['PURGE_DRY_RUN'])
        self.assertEqual(control['RECORDING_FORMAT'], 'mkv')
        self.assertEqual(control['LIVE_PIPE_QUEUE_PACKETS'], 512)
        self.assertEqual(core.camera_filename_code('cam1'), 'C1')
        self.assertEqual(core.camera_filename_code('cam2'), 'C2')
        self.assertEqual(core.camera_filename_code('cam3'), 'C3')
        from zoneinfo import ZoneInfo
        paris = ZoneInfo('Europe/Paris')
        self.assertEqual(archive.media_start(Path('2026/+0100/143200_02112026_RAW_C1.ts')).astimezone(paris).strftime('%H%M%S_%d%m%Y'), '143200_02112026')
        self.assertEqual(archive.media_start(Path('2026/+0100/050000_01112026_MER_C2_P01.mp4')).astimezone(paris).strftime('%H%M%S_%d%m%Y'), '050000_01112026')
        self.assertEqual(archive.media_start(Path('2026/+0100/050000_01112026_TLR_C3.mp4')).astimezone(paris).strftime('%H%M%S_%d%m%Y'), '050000_01112026')
        self.assertEqual(archive.media_start(Path('2026/+0100/050000_01112026_TLI_C3.mp4')).astimezone(paris).strftime('%H%M%S_%d%m%Y'), '050000_01112026')

    def test_recording_index_finds_timelapse_camera_and_type(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp); data=root/'data'
            for group, rel in [('original','cam1/+0100/143200_02112026_RAW_C1.ts'),('original','cam1/143200_02112026_RAW_C1.mkv'),('archives','cam2/+0100/050000_01112026_MER_C2_P01.mp4'),('timelapse','cam3/+0100/050000_01112026_TLR_C3.mp4'),('timelapse','cam3/+0100/050000_01112026_TLI_C3.mp4')]:
                p=data/'recordings'/group/rel;p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(b'test')
            with mock.patch.object(engine, 'ROOT', root), mock.patch.object(engine.core, 'DATA', data), mock.patch.object(engine.core, 'ROOT', root), mock.patch.object(engine.core, 'cameras', return_value=[{'id':'cam1'},{'id':'cam2'},{'id':'cam3'}]):
                found=engine._recordings()
            by_name={Path(x['path']).name:(x['camera_id'],x['type_code']) for x in found}
            self.assertEqual(by_name['143200_02112026_RAW_C1.ts'],('cam1','RAW'))
            self.assertEqual(by_name['143200_02112026_RAW_C1.mkv'],('cam1','RAW'))
            self.assertEqual(by_name['050000_01112026_MER_C2_P01.mp4'],('cam2','MER'))
            self.assertEqual(by_name['050000_01112026_TLR_C3.mp4'],('cam3','TLR'))
            self.assertEqual(by_name['050000_01112026_TLI_C3.mp4'],('cam3','TLI'))

    def test_current_archive_names_use_requested_time_first_order_and_keep_legacy_parsers(self):
        from datetime import datetime
        from zoneinfo import ZoneInfo
        from naming import filename, parse, parse_path
        when = datetime(2026, 11, 2, 14, 32, 0, tzinfo=ZoneInfo('Europe/Paris'))
        self.assertEqual(filename('C1', when, 'RAW', 'ts'), '143200_02112026_RAW_C1.ts')
        self.assertEqual(filename('C2', when, 'MER', 'mp4', part=1), '143200_02112026_MER_C2_P01.mp4')
        self.assertEqual(filename('C3', when, 'TLR', 'mp4'), '143200_02112026_TLR_C3.mp4')
        self.assertEqual(filename('C3', when, 'TLI', 'mp4'), '143200_02112026_TLI_C3.mp4')
        parsed = parse('143200_02112026_RAW_C1')
        self.assertEqual((parsed['camera'], parsed['kind'], parsed['year']), ('C1', 'RAW', '2026'))
        parsed_part = parse('143200_02112026_MER_C2_P01')
        self.assertEqual((parsed_part['camera'], parsed_part['part']), ('C2', '01'))
        # Old RAW names still read the year/offset from their parent directories.
        early = parse_path(Path('2026/+0200/C1_023000_2510_RAW.ts'))
        late = parse_path(Path('2026/+0100/C1_023000_2510_RAW.ts'))
        self.assertNotEqual(early['started'].timestamp(), late['started'].timestamp())
        # Previous V6 and V4 formats remain supported.
        parsed_v6 = parse('C1_143200_0211_RAW_26')
        self.assertEqual((parsed_v6['camera'], parsed_v6['kind'], parsed_v6['year']), ('C1', 'RAW', '26'))
        parsed_old = parse('C1_143200_0211_RAW_2026_+0100')
        self.assertEqual(parsed_old['format'], 'full-year')
        self.assertEqual(parse('C1_143200_021126_RAW')['format'], 'intermediate')
        self.assertEqual(parse('C1_RAW_143200_021126')['format'], 'legacy')

    def test_recording_is_not_configured_in_mediamtx(self):
        cameras = [
            {'id':'cam1','type':'rtsp','rtsp_url':'rtsp://192.0.2.11:554'},
            {'id':'cam2','type':'rtsp','rtsp_url':'rtsp://192.0.2.12:554'},
            {'id':'cam3','type':'cam3','rtmp_host':'192.0.2.13'},
        ]
        controls = {'RECORDING_SEGMENT_SECONDS':600,'ENABLE_ORIGINAL_RECORDING':True,
                    'RECORDING_FORMAT':'mkv','CAMERA_DEFAULT_RTSP_TRANSPORT':'tcp'}
        with mock.patch.object(core, 'cameras', return_value=cameras), mock.patch.object(core, 'load_control', return_value=controls):
            text = core.media_config_text()
        self.assertIn('paths:', text)
        self.assertEqual(text.count('record: false'), 4)  # pathDefaults plus three camera paths
        self.assertNotIn('recordPath:', text)

    def test_current_media_config_has_valid_routing_only_structure(self):
        text = core.media_config_text()
        self.assertTrue(text.startswith('logLevel: info\nlogDestinations: [stdout]\n'))
        self.assertIn('rtsp: true\nrtspAddress: :8554\n', text)
        self.assertIn('pathDefaults:\n  sourceOnDemand: false\n  record: false\n', text)
        self.assertIn('paths:', text)
        self.assertNotIn('recordPath:', text)

    def test_live_media_page_is_persistent_across_navigation_and_only_hide_stops_live(self):
        gui = (ROOT / 'app' / 'gui.py').read_text(encoding='utf-8')
        show_page = gui[gui.index('    def show_page(self, name):'):gui.index('    def refresh_all(self):')]
        set_panels = gui[gui.index('    def _set_panels(self):'):gui.index('    def _refresh_camera_tile_status(self):')]
        self.assertIn('self.media_page.pack_forget()', show_page)
        self.assertIn('if not self._media_page_built:', show_page)
        self.assertNotIn('stop_media_tiles', show_page)
        self.assertIn('tile.stop_player()', set_panels)
        self.assertIn('if self.show_live_var.get():', set_panels)
        self.assertIn('elif not var.get() and exists:self.workspace_split.forget(panel)', set_panels)

    def test_mpv_starts_idle_then_loads_source_only_after_ipc_socket_is_ready(self):
        import os
        import time
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            fake = root / 'fake-mpv'
            fake.write_text("""#!/usr/bin/env python3
import json, socket, sys, time
socket_path = next(x.split('=', 1)[1] for x in sys.argv if x.startswith('--input-ipc-server='))
s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
s.bind(socket_path)
s.listen(4)
stop = False
while not stop:
    conn, _ = s.accept()
    with conn:
        data = conn.recv(65536)
        for line in data.splitlines():
            try: msg = json.loads(line)
            except Exception: continue
            cmd = msg.get('command', [])
            if cmd and cmd[0] == 'loadfile':
                open(socket_path + '.loaded', 'w').write(cmd[1])
            conn.sendall((json.dumps({'request_id': msg.get('request_id'), 'error':'success', 'data':None})+chr(10)).encode())
            if cmd and cmd[0] == 'quit': stop = True
s.close()
""", encoding='utf-8')
            fake.chmod(0o755)
            class Widget:
                def update_idletasks(self): pass
                def winfo_id(self): return 12345
            with mock.patch.object(gui, 'ROOT', root):
                player = gui.MpvPlayer(Widget(), 'test', root/'logs'/'mpv.log',
                    root/'runtime'/'input.conf', str(fake), {'GUI_MPV_CACHE_SECONDS':2.5})
                try:
                    proc = player.start('/not-yet-loaded.ts', False)
                    deadline = time.monotonic() + 3
                    while not player.connected() and proc.poll() is None and time.monotonic() < deadline:
                        time.sleep(0.02)
                    self.assertTrue(player.connected(), 'fake MPV did not create its IPC socket')
                    self.assertFalse((root/'runtime'/'mpv_test.sock.loaded').exists(),
                        'start() must not try to load the source before IPC is ready')
                    player.load('/tmp/recording.ts', False)
                    marker = root/'runtime'/'mpv_test.sock.loaded'
                    deadline = time.monotonic() + 2
                    while not marker.exists() and time.monotonic() < deadline:
                        time.sleep(0.02)
                    self.assertTrue(marker.is_file())
                    self.assertEqual(marker.read_text(), '/tmp/recording.ts')
                finally:
                    player.stop()

    def test_mpv_status_polls_batch_properties_in_one_ipc_round_trip(self):
        import socket, tempfile, threading, json, itertools
        from pathlib import Path
        from gui import MpvPlayer
        with tempfile.TemporaryDirectory() as tmp:
            sock_path = Path(tmp) / 'mpv.sock'
            server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            server.bind(str(sock_path)); server.listen(1)
            accepted = []
            def serve():
                conn, _ = server.accept()
                accepted.append(1)
                with conn:
                    buf = b''
                    requests = []
                    while len(requests) < 3:
                        data = conn.recv(4096)
                        if not data: break
                        buf += data
                        while b'\n' in buf:
                            line, _, buf = buf.partition(b'\n')
                            if line:
                                requests.append(json.loads(line))
                    for req in requests:
                        name = req['command'][1]
                        conn.sendall(json.dumps({'request_id':req['request_id'],'error':'success','data':name}).encode()+b'\n')
            thread = threading.Thread(target=serve, daemon=True); thread.start()
            class Process:
                def poll(self): return None
            player = MpvPlayer.__new__(MpvPlayer)
            player.process = Process(); player.socket_path = sock_path; player._request_ids = itertools.count(1)
            values = player.get_many(['video-params','time-pos','track-list'])
            thread.join(2); server.close()
            self.assertEqual(values, {'video-params':'video-params','time-pos':'time-pos','track-list':'track-list'})
            self.assertEqual(len(accepted), 1)

    def test_timelapse_audio_tempo_filter_is_valid_chained_atempo(self):
        self.assertEqual(archive.atempo_filter(2.0), 'atempo=2.000000')
        self.assertEqual(archive.atempo_filter(120), 'atempo=2.0,atempo=2.0,atempo=2.0,atempo=2.0,atempo=2.0,atempo=2.0,atempo=1.875000')

    def test_camera_files_are_owner_only(self):
        for relative in ('config/cameras.json','cam3/rtmp_cam.py','USER_CONTROL.py','control/COMMANDS.json','config/bookmarks.json'):
            self.assertEqual((ROOT/relative).stat().st_mode & 0o777,0o600,relative)

    def test_cues_are_saved_as_protected_and_have_comments(self):
        source=(ROOT/'app'/'engine.py').read_text(encoding='utf-8')
        self.assertIn("protected = True",source)
        self.assertIn("'notes': str(p.get('notes'",source)
        self.assertIn("item['protected'] = True",source)
        self.assertIn('bookmarks.delete',source)

    def test_camera_status_checks_actual_media_streams(self):
        source=(ROOT/'app'/'engine.py').read_text(encoding='utf-8')
        self.assertIn("'-show_streams'",source)
        self.assertIn("codec_type",source)
        self.assertIn("'NO VIDEO'",source)
        self.assertIn("'audio_ready'",source)

    def test_detection_zones_rpc_saves_normalized_polygons(self):
        gui=(ROOT/'app'/'gui.py').read_text(encoding='utf-8')
        source=(ROOT/'app'/'engine.py').read_text(encoding='utf-8')
        self.assertIn('create_polygon',gui)
        self.assertIn("rpc('zones.save'",gui)
        self.assertIn("action == 'zones.save'",source)
        self.assertIn("cam['exclude_zones'] = polygons",source)


    def test_media_tool_defaults_resolve_to_project_bundle(self):
        sys.path.insert(0, str(ROOT / 'app'))
        import runtime_paths
        with tempfile.TemporaryDirectory(prefix='eyeyaye-media-paths-') as temp:
            bindir = Path(temp) / 'bin'
            bindir.mkdir()
            for name in ('ffmpeg', 'ffprobe'):
                binary = bindir / name
                binary.write_bytes(b'fake executable')
                binary.chmod(0o755)
            with mock.patch.object(runtime_paths, 'BIN_DIR', bindir):
                values = runtime_paths.resolve_media_tools({
                    'FFMPEG_BINARY': '/usr/bin/ffmpeg',
                    'FFPROBE_BINARY': '/usr/bin/ffprobe',
                })
            self.assertEqual(values['FFMPEG_BINARY'], str(bindir / 'ffmpeg'))
            self.assertEqual(values['FFPROBE_BINARY'], str(bindir / 'ffprobe'))

    def test_project_local_runtime_builder_and_media_paths_are_explicit(self):
        builder = (ROOT / 'BUILD_RUNTIME.py').read_text(encoding='utf-8')
        core_source = (ROOT / 'app' / 'core.py').read_text(encoding='utf-8')
        self.assertIn("TARGET = CAM3 / 'CAM3_LIVE_STANDALONE'", builder)
        self.assertIn("MEDIAMTX_BIN = BIN_DIR / 'mediamtx'", core_source)
        self.assertNotIn("Path('/usr/local/bin/mediamtx')", core_source)
        self.assertIn("CAM3_RTSP_HOST': '127.0.0.1'", core_source)
        self.assertIn("'CAM3_RTSP_PORT': '8554'", core_source)

    def test_runtime_builder_checksum_verification_accepts_matching_hash_and_rejects_bad_hash(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location('eyeyaye_build_runtime', ROOT / 'BUILD_RUNTIME.py')
        builder = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(builder)
        with tempfile.TemporaryDirectory(prefix='eyeyaye-builder-checksum-') as temp:
            folder = Path(temp)
            archive = folder / 'ffmpeg-linux-amd64.tar.gz'
            archive.write_bytes(b'known archive bytes for checksum verification')
            checksum = builder.sha256(archive)
            manifest = folder / 'SHA256SUMS.txt'
            manifest.write_text(f'{checksum}  {archive.name}\n', encoding='utf-8')
            with mock.patch.object(builder, 'download', return_value=manifest):
                self.assertEqual(builder.verify_manifest_checksum('https://invalid.invalid/checksums', archive, manifest), checksum)
                manifest.write_text(('0' * 64) + f'  {archive.name}\n', encoding='utf-8')
                with self.assertRaisesRegex(RuntimeError, 'SHA-256 mismatch'):
                    builder.verify_manifest_checksum('https://invalid.invalid/checksums', archive, manifest)

if __name__ == '__main__':
    unittest.main()
