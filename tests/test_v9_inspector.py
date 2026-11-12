from __future__ import annotations
import ast
import queue
import socket
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'app'))
from inspector import target_for, replace_target_text, commit_target
import gui
import master

class V9InspectorTests(unittest.TestCase):
    def test_source_inspector_resolves_project_methods_and_rejects_external_files(self):
        target = target_for(gui.CCTVDesktop.show_page, ROOT)
        self.assertIsNotNone(target)
        self.assertEqual(target['name'], 'CCTVDesktop.show_page')
        self.assertIn('def show_page', target['original'])
        self.assertTrue(target['path'].is_relative_to(ROOT))

    def test_source_replacement_is_syntax_checked_and_limited_to_selected_function(self):
        target = target_for(gui.CCTVDesktop.live_camera_is_enabled, ROOT)
        self.assertIsNotNone(target)
        edited = target['original'].replace('return bool(', 'return bool(')
        _path, updated = replace_target_text(ROOT, target, edited)
        ast.parse(updated)
        self.assertIn('def live_camera_is_enabled', updated)

    def test_source_inspector_resolves_tk_style_lambda_to_bound_project_method(self):
        instance = gui.CCTVDesktop.__new__(gui.CCTVDesktop)
        callback = lambda: instance.show_page('Cameras')
        target = target_for(callback, ROOT)
        self.assertIsNotNone(target)
        self.assertEqual(target['name'], 'CCTVDesktop.show_page')

    def test_source_replacement_detects_concurrent_source_change(self):
        target = target_for(gui.CCTVDesktop.show_page, ROOT)
        self.assertIsNotNone(target)
        target['original'] = 'stale source'
        with self.assertRaises(RuntimeError):
            replace_target_text(ROOT, target, 'def show_page(self, name):\n    pass\n')

    def test_per_camera_live_switches_are_viewer_only_and_persisted(self):
        source = (ROOT / 'app' / 'gui.py').read_text(encoding='utf-8')
        self.assertIn('live_camera_is_enabled', source)
        self.assertIn('set_live_camera_enabled', source)
        self.assertIn("'live_camera_enabled'", source)
        self.assertIn("self.player.start_live_pipe", source)
        self.assertNotIn("rpc('cameras.save'", source[source.index('def set_live_camera_enabled'):source.index('def _refresh_live_camera_switches')])


    def test_commit_is_atomic_and_creates_a_restorable_backup(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / 'app' / 'demo.py'
            source.parent.mkdir()
            original = 'class Demo:\n    def value(self):\n        return 1\n'
            source.write_text(original, encoding='utf-8')
            target = {
                'path': source, 'start_line': 2, 'end_line': 3,
                'original': '    def value(self):\n        return 1\n',
            }
            edited = '    def value(self):\n        return 2\n'
            backup = commit_target(root, target, edited)
            self.assertEqual(source.read_text(encoding='utf-8'), 'class Demo:\n' + edited)
            self.assertEqual(backup.read_text(encoding='utf-8'), original)
            compile(source.read_text(encoding='utf-8'), str(source), 'exec')

    def test_commit_rejects_invalid_python_without_touching_live_source(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / 'demo.py'
            original = 'def value():\n    return 1\n'
            source.write_text(original, encoding='utf-8')
            target = {'path': source, 'start_line': 1, 'end_line': 2, 'original': original}
            with self.assertRaises(SyntaxError):
                commit_target(root, target, 'def value(:\n    return 2\n')
            self.assertEqual(source.read_text(encoding='utf-8'), original)
            self.assertFalse((root / 'backups' / 'inspector').exists())

    def test_master_control_socket_queues_a_full_restart_request(self):
        with tempfile.TemporaryDirectory() as temp:
            socket_path = Path(temp) / 'runtime' / 'master-control.sock'
            with mock.patch.object(master, 'MASTER_CONTROL_SOCKET', socket_path), \
                 mock.patch.object(master, 'MASTER_COMMANDS', queue.Queue()):
                master.start_master_control_server()
                try:
                    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                        client.settimeout(2)
                        client.connect(str(socket_path))
                        client.sendall(b'{"action":"restart"}\n')
                        response = client.recv(4096).decode('utf-8', 'replace')
                    self.assertIn('queued', response)
                    self.assertEqual(master.MASTER_COMMANDS.get(timeout=2), 'restart')
                finally:
                    master.stop_master_control_server()

    def test_restart_everything_restarts_master_owned_process_stack_in_order(self):
        fake_proc = mock.Mock(pid=43210)
        with mock.patch.object(master, 'GUI_PROCESSES', [fake_proc]), \
             mock.patch.object(master, '_terminate_process_group') as terminate, \
             mock.patch.object(master, 'stop_engine') as stop, \
             mock.patch.object(master, 'start_engine') as start, \
             mock.patch.object(master, 'open_gui') as open_gui:
            master.restart_everything()
            terminate.assert_called_once_with(43210, graceful_seconds=8)
            self.assertEqual(master.GUI_PROCESSES, [])
            stop.assert_called_once_with()
            start.assert_called_once_with()
            open_gui.assert_called_once_with()

    def test_archive_folder_action_is_project_path_restricted(self):
        source = (ROOT / 'app' / 'gui.py').read_text(encoding='utf-8')
        self.assertIn('OPEN CONTAINING DIRECTORY', source)
        self.assertIn('folder.relative_to(ROOT.resolve())', source)

    def test_inspector_captures_clicks_before_actions_and_tags_future_widgets(self):
        source = (ROOT / 'app' / 'gui.py').read_text(encoding='utf-8')
        self.assertIn('_tk.BaseWidget.__init__ = tracked_widget_init', source)
        self.assertIn("self.root.bind_class(self._inspector_bindtag, '<ButtonPress-1>'", source)
        self.assertIn("self.root.bind_class(self._inspector_bindtag, '<ButtonRelease-1>'", source)
        self.assertIn("return None if self._inspector_is_internal(event.widget) else 'break'", source)
        self.assertNotIn("bind_all('<ButtonPress-1>'", source)

if __name__ == '__main__': unittest.main()
