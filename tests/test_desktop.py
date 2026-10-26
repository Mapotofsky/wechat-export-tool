import io
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import urllib.error

import desktop
import runtime_paths


class DesktopTests(unittest.TestCase):
    def test_portable_paths_do_not_depend_on_current_directory(self):
        with tempfile.TemporaryDirectory(prefix='中文 portable ') as tmp:
            root = Path(tmp)
            (root / 'runtime').mkdir()
            (root / 'runtime' / 'python.exe').touch()
            with patch.dict('os.environ', {'WECHAT_ARCHIVE_HOME': str(root)}):
                os.environ.pop('WECHAT_ARCHIVE_DATA_DIR', None)
                self.assertEqual(runtime_paths.data_root(), root / 'local_data')
                self.assertEqual(runtime_paths.models_root(), root / 'models')
                self.assertEqual(runtime_paths.python_executable(), root / 'runtime' / 'python.exe')
                self.assertEqual(runtime_paths.voice_python_executable(), root / 'voice-runtime' / 'python.exe')

    def test_save_search_posts_same_request_and_writes_actual_bytes(self):
        with tempfile.TemporaryDirectory(prefix='保存 export ') as tmp:
            target = Path(tmp) / '命中.json'
            window = SimpleNamespace(create_file_dialog=lambda *args, **kwargs: (str(target),))
            bridge = desktop.NativeBridge('http://127.0.0.1:4567', lambda: False)
            bridge._window = window
            response = io.BytesIO(b'{"saved":true}')
            response.headers = {'Content-Disposition': "attachment; filename*=UTF-8''test.json"}
            with patch.dict(sys.modules, {'webview': SimpleNamespace(FileDialog=SimpleNamespace(SAVE=1))}), patch.object(
                    desktop.urllib.request, 'urlopen', return_value=response) as open_url:
                result = bridge.save_export({'mode': 'search', 'q': '报销', 'format': 'json', 'usernames': ['群']})
            self.assertTrue(result['saved'])
            self.assertEqual(target.read_bytes(), b'{"saved":true}')
            request = open_url.call_args.args[0]
            self.assertEqual(request.full_url, 'http://127.0.0.1:4567/api/export-search')
            self.assertEqual(json.loads(request.data)['q'], '报销')
            self.assertNotIn('mode', json.loads(request.data))

    def test_failed_export_keeps_existing_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / 'existing.txt'
            target.write_text('原有文件', encoding='utf-8')
            bridge = desktop.NativeBridge('http://127.0.0.1:4567', lambda: False)
            bridge._window = SimpleNamespace(create_file_dialog=lambda *args, **kwargs: (str(target),))
            error = urllib.error.HTTPError('url', 400, 'invalid', {}, io.BytesIO(b'{"detail":"invalid"}'))
            with patch.dict(sys.modules, {'webview': SimpleNamespace(FileDialog=SimpleNamespace(SAVE=1))}), patch.object(
                    desktop.urllib.request, 'urlopen', side_effect=error):
                result = bridge.save_export({'format': 'txt'})
            self.assertFalse(result['saved'])
            self.assertEqual(target.read_text(encoding='utf-8'), '原有文件')

    def test_busy_or_saving_blocks_window_close(self):
        bridge = desktop.NativeBridge('', lambda: True)
        bridge._window = SimpleNamespace(create_confirmation_dialog=lambda *args: True)
        self.assertFalse(bridge._can_close())
        bridge._busy = lambda: False
        bridge._saving = True
        self.assertFalse(bridge._can_close())
        bridge._saving = False
        self.assertTrue(bridge._can_close())

    @unittest.skipUnless(sys.platform == 'win32', 'Windows file locking')
    def test_second_instance_cannot_acquire_same_data_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = desktop.InstanceLock(Path(tmp) / 'desktop.lock')
            second = desktop.InstanceLock(Path(tmp) / 'desktop.lock')
            try:
                self.assertTrue(first.acquire())
                self.assertFalse(second.acquire())
                first.close()
                self.assertTrue(second.acquire())
            finally:
                first.close()
                second.close()


if __name__ == '__main__':
    unittest.main()
