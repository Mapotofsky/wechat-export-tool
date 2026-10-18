from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import voice_service
from runtime_paths import voice_python_executable
import local_asr


class LauncherTests(unittest.TestCase):
    def test_voice_uses_runtime_entry_and_missing_path_has_repair(self):
        self.assertEqual(voice_service.PYTHON, voice_python_executable())
        info = voice_service.probe('C:/missing environment/python.exe')
        self.assertFalse(info['available'])
        self.assertIn('完整语音包', info['reason'])
        self.assertEqual(info['python'], 'C:/missing environment/python.exe')

    def test_voice_subprocess_keeps_space_path_as_single_argument(self):
        path = 'C:/voice environment/python.exe'
        with patch.object(Path, 'is_file', return_value=True), patch.object(voice_service.subprocess, 'run') as run:
            run.return_value.stdout = '{"available":false,"models":[]}'
            run.return_value.returncode = 0
            info = voice_service.probe(path)
        self.assertEqual(run.call_args.args[0][0], path)
        self.assertIn(path, info['reason'])

    def test_probe_unicode_paths_with_legacy_windows_encoding(self):
        with tempfile.TemporaryDirectory(prefix='语音 工具 ') as tmp:
            worker = Path(tmp) / 'worker.py'
            worker.write_text('import json\nprint(json.dumps(' + repr({
                'available': True, 'silk': True, 'models': ['D:/中文 模型/small.pt']}) +
                ', ensure_ascii=False))\n', encoding='utf-8')
            with patch.object(voice_service, 'WORKER', worker), patch.dict(
                    'os.environ', {'PYTHONIOENCODING': 'gbk'}):
                info = voice_service.probe(sys.executable)
            self.assertTrue(info['available'])
            self.assertEqual(info['models'], ['D:/中文 模型/small.pt'])

    def test_probe_failure_preserves_exit_code_and_stderr(self):
        with patch.object(voice_service, 'run_worker', return_value=subprocess.CompletedProcess(
                [], 1, '', 'ModuleNotFoundError: 缺少 whisper')):
            info = voice_service.probe(sys.executable)
        self.assertFalse(info['available'])
        self.assertIn('退出码 1', info['reason'])
        self.assertIn('ModuleNotFoundError: 缺少 whisper', info['reason'])

    def test_probe_empty_output_does_not_raise_type_error(self):
        with patch.object(voice_service, 'run_worker', return_value=subprocess.CompletedProcess(
                [], 0, None, '')):
            info = voice_service.probe(sys.executable)
        self.assertFalse(info['available'])
        self.assertIn('进程未返回输出', info['reason'])

    def test_recognize_process_failure_includes_stderr(self):
        with patch.object(voice_service, 'run_worker', return_value=subprocess.CompletedProcess(
                [], 1, '', 'ImportError: DLL load failed')):
            with self.assertRaisesRegex(RuntimeError, '退出码 1；ImportError: DLL load failed'):
                voice_service.recognize(b'audio', 'model.pt')

    def test_portable_models_are_discovered_only_inside_package(self):
        with tempfile.TemporaryDirectory(prefix='语音 portable ') as tmp:
            root = Path(tmp)
            (root / 'runtime').mkdir()
            (root / 'runtime' / 'python.exe').touch()
            (root / 'models').mkdir()
            model = root / 'models' / 'small.pt'
            model.touch()
            with patch.dict('os.environ', {'WECHAT_ARCHIVE_HOME': str(root)}), patch.object(
                    local_asr, 'ffmpeg_executable', return_value='bundled-ffmpeg.exe'), patch.object(
                    local_asr.importlib.util, 'find_spec', return_value=True):
                self.assertEqual(local_asr.capabilities()['models'], [str(model)])

