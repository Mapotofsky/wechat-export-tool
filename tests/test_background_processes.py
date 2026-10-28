"""Background launch contracts; no real WeChat process or external API is used."""
from pathlib import Path
import subprocess
import json
import sys
import tempfile
import unittest
from unittest.mock import patch

import local_asr
import maintenance
import voice_service


class BackgroundProcessTests(unittest.TestCase):
    def test_worker_preserves_arguments_capture_encoding_and_timeout(self):
        with patch.object(subprocess, 'run') as run:
            voice_service.run_worker('C:/voice runtime/python.exe', '--audio',
                                     'C:/audio files/中文.wav', timeout=17)
        args, options = run.call_args
        self.assertEqual(args[0], ['C:/voice runtime/python.exe', str(voice_service.WORKER),
                                   '--audio', 'C:/audio files/中文.wav'])
        self.assertEqual(options['creationflags'], getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        self.assertTrue(options['capture_output'])
        self.assertTrue(options['text'])
        self.assertEqual(options['encoding'], 'utf-8')
        self.assertEqual(options['errors'], 'replace')
        self.assertEqual(options['env']['PYTHONIOENCODING'], 'utf-8')
        self.assertEqual(options['timeout'], 17)

    def test_real_worker_failure_and_timeout_with_space_paths(self):
        with tempfile.TemporaryDirectory(prefix='后台 工具 ') as tmp:
            worker = Path(tmp) / 'worker space.py'
            worker.write_text('import sys\nprint("输出")\nprint("失败详情", file=sys.stderr)\nsys.exit(7)', encoding='utf-8')
            with patch.object(voice_service, 'WORKER', worker):
                result = voice_service.run_worker(sys.executable, timeout=10)
                self.assertEqual(result.returncode, 7)
                self.assertIn('输出', result.stdout)
                self.assertIn('失败详情', voice_service.process_failure(result))
                worker.write_text('import time\ntime.sleep(5)', encoding='utf-8')
                with self.assertRaises(subprocess.TimeoutExpired):
                    voice_service.run_worker(sys.executable, timeout=.1)

    def test_ffmpeg_hidden_launch_and_failure_and_timeout(self):
        with tempfile.TemporaryDirectory(prefix='音频 空格 ') as tmp:
            source, output = Path(tmp) / 'input voice.wav', Path(tmp) / 'output voice.wav'
            source.write_bytes(b'synthetic')
            with patch.object(local_asr, 'capabilities', return_value={
                    'ffmpeg': 'C:/audio tools/ffmpeg.exe'}), patch.object(subprocess, 'run') as run:
                run.return_value = subprocess.CompletedProcess([], 9, b'', '转换错误'.encode())
                with self.assertRaisesRegex(RuntimeError, '退出码 9.*转换错误'):
                    local_asr.decode_audio(source, output)
                args, options = run.call_args
                self.assertEqual(args[0][0], 'C:/audio tools/ffmpeg.exe')
                self.assertEqual(args[0][4], str(source))
                self.assertEqual(args[0][-1], str(output))
                self.assertEqual(options, dict(capture_output=True, timeout=120,
                    creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0)))
                run.side_effect = subprocess.TimeoutExpired('ffmpeg', 120)
                with self.assertRaises(subprocess.TimeoutExpired):
                    local_asr.decode_audio(source, output)

    def test_key_tool_hidden_launch_and_task_failure(self):
        with tempfile.TemporaryDirectory(prefix='密钥 模拟 ') as tmp:
            manager = maintenance.Manager(tmp)
            try:
                account = {'id': 'synthetic', 'mode': 'encrypted', 'directory': 'C:/synthetic db/中文'}
                archive = manager.archive(account)
                for outcome in ('success', 'failure', 'timeout'):
                    def fake_run(command, **options):
                        self.assertEqual(command[:3], [str(Path('C:/base runtime/python.exe')),
                            str(maintenance.resource_root() / 'wcdb_key_tool_windows.py'), 'extract'])
                        self.assertEqual(command[4], account['directory'])
                        self.assertEqual(options['timeout'], 600)
                        self.assertIsNotNone(options['cancellation'])
                        if outcome == 'timeout':
                            raise subprocess.TimeoutExpired(command, 600)
                        if outcome == 'success':
                            Path(command[-1]).write_text('{}')
                            Path(command[6]).write_text(json.dumps({'stage':'complete','databases':1,'verified_databases':1}))
                        return subprocess.CompletedProcess(command, 0 if outcome == 'success' else 4,
                                                           b'', b'synthetic private key output')
                    task = {'status': 'running'}
                    with patch.object(maintenance, 'python_executable', return_value=Path('C:/base runtime/python.exe')), patch.object(
                            maintenance.supervised_process, 'run', side_effect=fake_run):
                        manager.perform(account, archive, task, 'extract', [])
                    self.assertEqual(task['status'], 'success' if outcome == 'success' else 'failed')
                    if outcome != 'success':
                        self.assertTrue(task['error_location'])
                        self.assertNotIn('synthetic private key output', task['reason'])
                        self.assertIn('TimeoutExpired' if outcome == 'timeout' else '密钥提取失败', task['reason'])
            finally:
                manager.executor.shutdown()

    def test_probe_and_recognize_timeout_remain_actionable(self):
        with patch.object(voice_service, 'run_worker', side_effect=subprocess.TimeoutExpired('worker', 60)):
            self.assertIn('TimeoutExpired', voice_service.probe(sys.executable)['reason'])
            with self.assertRaisesRegex(RuntimeError, '本地识别超时'):
                voice_service.recognize(b'synthetic', 'model.pt')
            with self.assertRaises(subprocess.TimeoutExpired):
                voice_service.recognize_online(b'synthetic', {'asr_provider': 'openai',
                    'asr_api_url': 'http://127.0.0.1:1/transcribe', 'asr_api_model': 'synthetic'})

    @unittest.skipUnless(sys.platform == 'win32', 'Windows tasklist tool')
    def test_key_tool_nested_tasklist_is_already_hidden(self):
        import wcdb_key_tool_windows as key_tool
        with patch.object(subprocess, 'run', return_value=subprocess.CompletedProcess(
                [], 0, '"Weixin.exe","123","Console","1","1,000 K"', '')) as run:
            self.assertEqual(key_tool._get_pids_windows(), [(123, 1000)])
        self.assertEqual(run.call_args.args[0][0], 'tasklist')
        self.assertEqual(run.call_args.kwargs['creationflags'], subprocess.CREATE_NO_WINDOW)
        self.assertTrue(run.call_args.kwargs['capture_output'])


if __name__ == '__main__':
    unittest.main()
