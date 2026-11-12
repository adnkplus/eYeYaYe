from __future__ import annotations
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'app'))
import master
import gui


class LifecycleTests(unittest.TestCase):
    def test_launcher_opens_gui_by_default_and_supports_terminal_only(self):
        self.assertEqual(master.parse_launch_mode([]), 'gui')
        self.assertEqual(master.parse_launch_mode(['-G']), 'gui')
        self.assertEqual(master.parse_launch_mode(['--gui']), 'gui')
        self.assertEqual(master.parse_launch_mode(['-T']), 'terminal')
        self.assertEqual(master.parse_launch_mode(['--terminal']), 'terminal')
        with self.assertRaises(ValueError):
            master.parse_launch_mode(['-G', '-T'])

    def test_legacy_docker_descriptors_are_removed_without_deleting_user_data(self):
        with tempfile.TemporaryDirectory(prefix='eyeyaye-legacy-cleanup-') as temp:
            root = Path(temp)
            for name in ('Dockerfile', 'docker-compose.yml', '.dockerignore'):
                (root / name).write_text('obsolete', encoding='utf-8')
            data = root / 'docker' / 'data' / 'keep.bin'
            data.parent.mkdir(parents=True)
            data.write_bytes(b'user data remains untouched')
            old_root = master.ROOT
            try:
                master.ROOT = root
                master.remove_legacy_docker_files()
            finally:
                master.ROOT = old_root
            for name in ('Dockerfile', 'docker-compose.yml', '.dockerignore'):
                self.assertFalse((root / name).exists())
            self.assertEqual(data.read_bytes(), b'user data remains untouched')

    def test_startup_uses_the_project_engine_and_waits_for_unix_socket(self):
        source = (ROOT / 'master.py').read_text(encoding='utf-8')
        self.assertIn("str(ROOT / 'app' / 'engine.py')", source)
        self.assertIn('def wait_for_engine(timeout=35):', source)
        self.assertIn('wait_for_engine()', source)
        self.assertIn("env['CCTV_PROJECT_ROOT'] = str(ROOT)", source)
        self.assertNotIn("['docker'", source)
        self.assertNotIn("['podman'", source)
        self.assertNotIn('sudo docker', source)

    def test_gui_close_only_stops_local_players_and_window(self):
        class FakeRoot:
            destroyed = False
            def destroy(self): self.destroyed = True
        desktop = gui.CCTVDesktop.__new__(gui.CCTVDesktop)
        desktop.root = FakeRoot()
        with mock.patch.object(desktop, 'persist_workspace'), mock.patch.object(desktop, 'stop_media_tiles') as stop:
            gui.CCTVDesktop.close(desktop)
        stop.assert_called_once_with()
        self.assertTrue(desktop.root.destroyed)

    def test_master_shutdown_closes_owned_gui_groups_and_engine(self):
        class FakeProcess:
            pid = 18765
            def poll(self): return None
            def wait(self, timeout=None): return 0
        old_gui = master.GUI_PROCESSES
        old_guard = master._SHUTDOWN_STARTED
        try:
            master.GUI_PROCESSES = [FakeProcess()]
            master._SHUTDOWN_STARTED = False
            with mock.patch.object(master, '_terminate_process_group') as terminate, \
                 mock.patch.object(master, 'stop_engine') as stop, \
                 mock.patch.object(master, 'say'):
                master.shutdown_project()
            terminate.assert_called_once_with(18765, graceful_seconds=8)
            stop.assert_called_once_with()
            self.assertEqual(master.GUI_PROCESSES, [])
        finally:
            master.GUI_PROCESSES = old_gui
            master._SHUTDOWN_STARTED = old_guard

    def test_master_terminal_owns_signal_and_process_group_lifecycle(self):
        source = (ROOT / 'master.py').read_text(encoding='utf-8')
        self.assertIn('def _terminate_process_group', source)
        self.assertIn('_terminate_process_group(proc.pid', source)
        self.assertIn('def reap_gui_processes', source)
        self.assertIn("signal.signal(signal.SIGHUP, _exit_signal)", source)
        self.assertIn('finally:\n        shutdown_project()', source)
        self.assertIn('Closing the GUI keeps streams and recording running.', source)
        self.assertIn('Closing this terminal or typing quit stops this project and its players.', source)
        mpv_class = (ROOT / 'app' / 'gui.py').read_text(encoding='utf-8').split('class MpvPlayer:', 1)[1].split('class VideoTile:', 1)[0]
        self.assertNotIn('start_new_session=True', mpv_class, 'mpv must inherit the isolated GUI process group')


if __name__ == '__main__':
    unittest.main()
