from __future__ import annotations
import json
import os
import select
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'app'))
import core
import gui


class RecorderPipelineIntegrationTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'FFmpeg/FFprobe are required')
    def test_stream_copy_recording_and_reconnectable_matroska_fifo(self):
        ffmpeg = shutil.which('ffmpeg')
        ffprobe = shutil.which('ffprobe')
        with tempfile.TemporaryDirectory(prefix='eyeyaye-v8-rec-') as temp:
            work = Path(temp)
            source = work / 'synthetic-input.mkv'
            output = work / 'recordings'
            output.mkdir()
            fifo = work / 'live.mkvpipe'
            os.mkfifo(fifo)
            create = [ffmpeg, '-hide_banner', '-loglevel', 'error', '-y',
                      '-f', 'lavfi', '-i', 'testsrc2=size=320x240:rate=10',
                      '-f', 'lavfi', '-i', 'sine=frequency=1000:sample_rate=48000',
                      '-t', '12', '-c:v', 'libx264', '-g', '10', '-keyint_min', '10',
                      '-preset', 'ultrafast', '-pix_fmt', 'yuv420p',
                      '-c:a', 'aac', '-b:a', '96k', str(source)]
            subprocess.run(create, check=True, capture_output=True, text=True, timeout=20)
            control = {'FFMPEG_BINARY': ffmpeg, 'FFMPEG_LOGLEVEL': 'warning',
                       'RECORDING_SEGMENT_SECONDS': 60, 'RECORDING_FORMAT': 'mkv',
                       'LIVE_PIPE_QUEUE_PACKETS': 128}
            command = core.build_recording_command('camtest', str(source), output, fifo,
                                                   control, realtime=True, stream_loop=True)
            self.assertEqual(command[command.index('-c:v') + 1], 'copy')
            self.assertEqual(command[command.index('-c:a') + 1], 'copy')
            self.assertEqual(command[command.index('-fps_mode:v') + 1], 'passthrough')
            self.assertIn('-copyinkf', command)
            # Shorten the target only in this synthetic test; V8 ships 600 seconds.
            command[-1] = command[-1].replace('segment_time=60:', 'segment_time=3:')
            proc = subprocess.Popen(command, stdin=subprocess.DEVNULL,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            try:
                # No viewer is attached during this interval. Recording must continue.
                time.sleep(5)
                segments = sorted(output.glob('*.mkv'))
                self.assertTrue(segments, 'No MKV segment was created with preview unopened.')
                self.assertTrue(any(item.stat().st_size > 1000 for item in segments),
                                'RAW segment did not receive packets while preview was unopened.')

                def fifo_header():
                    fd = os.open(fifo, os.O_RDONLY | os.O_NONBLOCK)
                    data = bytearray()
                    deadline = time.monotonic() + 2.5
                    try:
                        while time.monotonic() < deadline and len(data) < 65536:
                            ready, _, _ = select.select([fd], [], [], 0.15)
                            if ready:
                                try:
                                    block = os.read(fd, 65536)
                                except BlockingIOError:
                                    continue
                                if block:
                                    data.extend(block)
                    finally:
                        os.close(fd)
                    return bytes(data)

                first = fifo_header()
                self.assertTrue(first.startswith(bytes.fromhex('1a45dfa3')),
                                f'First viewer did not receive Matroska header: {first[:8].hex()}')
                time.sleep(1.1)
                second = fifo_header()
                self.assertTrue(second.startswith(bytes.fromhex('1a45dfa3')),
                                f'Reconnected viewer did not receive a new Matroska header: {second[:8].hex()}')
            finally:
                core.recording_procs['camtest'] = proc
                core.recording_logs['camtest'] = None
                with mock.patch.object(core, 'live_pipe_path', return_value=fifo):
                    core.stop_recorder('camtest', graceful=True)
                if proc.poll() is None:
                    proc.kill()
                    proc.wait(timeout=3)
                if proc.stderr:
                    proc.stderr.close()
            self.assertIsNotNone(proc.poll(), 'Recorder did not shut down cleanly.')
            # Probe only after the segment is closed, so ffprobe sees a finalized MKV.
            closed_segments = sorted(output.glob('*.mkv'))
            candidate = next((item for item in closed_segments if item.stat().st_size > 1000), None)
            self.assertIsNotNone(candidate, 'No finalized RAW MKV segment exists.')
            probe = subprocess.run([ffprobe, '-v', 'error', '-show_entries',
                                    'stream=codec_type,codec_name', '-of', 'json', str(candidate)],
                                   check=True, capture_output=True, text=True, timeout=8)
            streams = json.loads(probe.stdout).get('streams', [])
            codecs = {(item.get('codec_type'), item.get('codec_name')) for item in streams}
            self.assertIn(('video', 'h264'), codecs)
            self.assertIn(('audio', 'aac'), codecs)

    def test_mpv_live_pipe_forwards_one_way_bytes_and_exits_on_fifo_end(self):
        with tempfile.TemporaryDirectory(prefix='eyeyaye-v8-mpv-fake-') as temp:
            root = Path(temp)
            runtime = root / 'runtime'
            logs = root / 'logs'
            runtime.mkdir(); logs.mkdir()
            fifo = runtime / 'live.mkvpipe'
            os.mkfifo(fifo)
            fake = root / 'fake-mpv'
            capture = root / 'captured.bin'
            fake.write_text('''#!/usr/bin/env python3
import json, os, socket, sys, threading
sock = next(x.split('=',1)[1] for x in sys.argv if x.startswith('--input-ipc-server='))
server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
server.bind(sock); server.listen(4); server.settimeout(0.2)
def ipc():
    while True:
        try: conn,_ = server.accept()
        except socket.timeout: continue
        except OSError: return
        with conn:
            line = conn.recv(4096).splitlines()[0]
            try: msg=json.loads(line)
            except Exception: continue
            conn.sendall((json.dumps({'request_id':msg.get('request_id'),'error':'success','data':None})+'\\n').encode())
threading.Thread(target=ipc,daemon=True).start()
with open(os.environ['V8_FAKE_MPV_CAPTURE'],'wb') as out:
    while True:
        block=sys.stdin.buffer.read(4096)
        if not block: break
        out.write(block); out.flush()
server.close()
''', encoding='utf-8')
            fake.chmod(0o755)
            class Widget:
                def update_idletasks(self): pass
                def winfo_id(self): return 12345
            marker = bytes.fromhex('1a45dfa3') + b'V8-test-stream'
            with mock.patch.object(gui, 'ROOT', root), mock.patch.dict(os.environ, {'V8_FAKE_MPV_CAPTURE': str(capture)}):
                player = gui.MpvPlayer(Widget(), 'pipe-test', logs / 'mpv.log',
                                       runtime / 'input.conf', str(fake), {})
                proc = player.start_live_pipe(fifo)
                try:
                    deadline = time.monotonic() + 2
                    writer = None
                    while time.monotonic() < deadline and writer is None:
                        try:
                            writer = os.open(fifo, os.O_WRONLY | os.O_NONBLOCK)
                        except OSError:
                            time.sleep(0.02)
                    self.assertIsNotNone(writer, 'MPV forwarder did not attach to its FIFO.')
                    os.write(writer, marker)
                    os.close(writer)
                    deadline = time.monotonic() + 3
                    while not capture.exists() and time.monotonic() < deadline:
                        time.sleep(0.02)
                    proc.wait(timeout=3)
                    self.assertEqual(capture.read_bytes(), marker)
                finally:
                    player.stop()


if __name__ == '__main__':
    unittest.main()
