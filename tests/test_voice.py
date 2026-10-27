"""Synthetic-only ASR protocol, device selection and selected-message updates."""
from contextlib import closing
import base64
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import wave

from archive_store import Archive
import local_asr
from maintenance import Manager
from test_maintenance import seed, table
import voice_service
from web import app as web


def wav_bytes():
    data = io.BytesIO()
    with wave.open(data, 'wb') as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(16000)
        out.writeframes(b'\0\0' * 1600)
    return data.getvalue()


class DeviceTests(unittest.TestCase):
    def test_device_uses_cuda_only_when_allocation_works(self):
        torch = SimpleNamespace(cuda=Mock(), empty=Mock(), version=SimpleNamespace(cuda='12.1'))
        torch.cuda.is_available.return_value = True
        torch.cuda.get_device_name.return_value = 'synthetic GPU'
        with patch.dict(sys.modules, {'torch': torch}):
            self.assertEqual(local_asr.select_device()['device'], 'cuda')
            torch.empty.side_effect = RuntimeError('driver unavailable')
            self.assertEqual(local_asr.select_device()['device'], 'cpu')
            torch.cuda.is_available.return_value = False
            torch.version.cuda = None
            self.assertIn('CPU 版本', local_asr.select_device()['device_reason'])

    def test_cuda_oom_retries_cpu_and_reports_actual_device(self):
        gpu, cpu = Mock(), Mock()
        gpu.transcribe.side_effect = RuntimeError('CUDA out of memory')
        cpu.transcribe.return_value = {'text': '合成语音'}
        whisper = SimpleNamespace(load_model=Mock(side_effect=[gpu, cpu]))
        with tempfile.TemporaryDirectory() as tmp:
            model = Path(tmp) / 'model.pt'
            model.touch()
            def decode(source, output):
                Path(output).write_bytes(wav_bytes())
            with patch.dict(sys.modules, {'whisper': whisper, 'torch': SimpleNamespace(cuda=Mock())}), patch.object(
                    local_asr, 'select_device', return_value={'device': 'cuda', 'device_name': 'GPU'}), patch.object(
                    local_asr, 'decode_audio', side_effect=decode):
                result = local_asr.recognize('synthetic', model)
            self.assertEqual(result['device'], 'cpu')
            self.assertIn('回退 CPU', result['device_reason'])
            self.assertTrue(gpu.transcribe.call_args.kwargs['fp16'])
            self.assertFalse(cpu.transcribe.call_args.kwargs['fp16'])


class OnlineTests(unittest.TestCase):
    def setUp(self):
        self.requests = []
        owner = self
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                owner.requests.append((self.headers, self.rfile.read(int(self.headers['Content-Length']))))
                self.send_response(401 if self.path == '/failure' else 200)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({'message': 'secret-test-key'} if self.path == '/failure' else
                    {'output': {'text': '阿里合成结果'}} if self.path == '/dashscope' else {'text': '兼容合成结果'}).encode())
            def log_message(self, *args):
                pass
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.thread.join)
        self.addCleanup(self.server.shutdown)

    def config(self, provider, path):
        return {'asr_provider': provider, 'asr_api_url': f'http://127.0.0.1:{self.server.server_port}/{path}',
                'asr_api_model': 'synthetic-model', 'asr_api_key': 'secret-test-key'}

    def test_dashscope_direct_wav_base64_and_authentication(self):
        result = voice_service.request_online(wav_bytes(), self.config('dashscope', 'dashscope'))
        self.assertEqual(result['text'], '阿里合成结果')
        headers, body = self.requests[0]
        self.assertEqual(headers['Authorization'], 'Bearer secret-test-key')
        request = json.loads(body)
        uri = request['input']['messages'][0]['content'][0]['input_audio']['data']
        self.assertEqual(base64.b64decode(uri.split(',')[1]), wav_bytes())
        self.assertEqual(request['parameters']['format'], 'wav')

    def test_compatible_multipart_and_error_redaction(self):
        result = voice_service.request_online(wav_bytes(), self.config('openai', 'openai'))
        self.assertEqual(result['text'], '兼容合成结果')
        headers, body = self.requests[0]
        self.assertTrue(headers['Content-Type'].startswith('multipart/form-data; boundary='))
        self.assertIn(b'name="file"; filename="voice.wav"', body)
        self.assertIn(wav_bytes(), body)
        self.assertIn(b'synthetic-model', body)
        with self.assertRaisesRegex(RuntimeError, 'HTTP 401') as caught:
            voice_service.request_online(wav_bytes(), self.config('openai', 'failure'))
        self.assertNotIn('secret-test-key', str(caught.exception))


class VoiceManagementTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.source = self.root / 'source'
        self.username = seed(self.source)
        with closing(sqlite3.connect(self.source / 'message/message_0.db')) as c, c:
            c.executemany(f'INSERT INTO "{table(self.username)}" VALUES (?,?,34,8,?,"voice",0)',
                          [(3, 103, 1700000002), (4, 104, 1700000003)])
        self.manager = Manager(self.root / 'state')
        self.addCleanup(self.manager.executor.shutdown)
        self.manager.select(str(self.source), 'synthetic', True)
        self.archive = self.manager.archive()
        self.archive.save_config({'self_username': 'wxid_self'})
        self.archive.update(self.source)
        self.targets = [{'username': self.username, 'message_id': 'server:103'}]
        for key in ('server:103', 'server:104'):
            self.archive.save_transcript(self.username, key, b'media', 'success', text='原成功文本')

    def test_selected_force_updates_one_and_retains_success_on_failure(self):
        with patch.object(voice_service, 'find_media', return_value=(b'media', {})):
            recognize = Mock(return_value={'text': '重新识别', 'provider': 'openai', 'model': 'test', 'device': 'online'})
            result = voice_service.transcribe(self.archive, self.source, [self.username], '', recognizer=recognize,
                                               targets=self.targets, force=True)
            self.assertEqual(result['success'], 1)
            self.assertEqual(self.archive.transcript(self.username, 'server:103')[2], '重新识别')
            self.assertEqual(self.archive.transcript(self.username, 'server:104')[2], '原成功文本')
            recognize.side_effect = RuntimeError('API error')
            result = voice_service.transcribe(self.archive, self.source, [self.username], '', recognizer=recognize,
                                               targets=self.targets, force=True)
            self.assertEqual(result['failed'], 1)
            self.assertEqual(self.archive.transcript(self.username, 'server:103')[1:], ('success', '重新识别'))
            self.assertIn('API error', self.archive.voice_page([self.username])['messages'][1]['transcription']['reason'])

    def test_missing_media_force_is_failure_and_keeps_success(self):
        result = voice_service.transcribe(self.archive, self.source, [self.username], '', targets=self.targets, force=True)
        self.assertEqual(result['failed'], 1)
        self.assertEqual(self.archive.transcript(self.username, 'server:103')[1:], ('success', '原成功文本'))

    def test_voice_pagination_status_search_and_scope_validation(self):
        self.assertEqual(self.archive.voice_page([self.username], q='原成功')['total'], 2)
        self.assertEqual(self.archive.voice_page([self.username], status='failed')['total'], 0)
        self.assertEqual(self.archive.voice_page([self.username], limit=1, offset=1)['messages'][0]['message_id'], 'server:103')
        for targets in ([], [{'username': self.username, 'message_id': 'server:101'}],
                        [{'username': 'other', 'message_id': 'server:103'}]):
            with self.assertRaises(ValueError):
                self.manager.submit('transcribe', [self.username], voice_targets=targets)

    def test_settings_key_hidden_and_online_manager_dispatch(self):
        with patch.object(web, 'manager', self.manager):
            web.settings(web.SettingsReq(asr_provider='dashscope', asr_api_url='https://example.test/asr',
                                          asr_api_model='fun-asr-flash-test', asr_api_key='secret-test-key'))
            self.assertNotIn('asr_api_key', web.data_status()['config'])
            self.assertTrue(web.data_status()['config']['asr_api_key_set'])
            web.settings(web.SettingsReq(asr_api_model='another'))
            self.assertEqual(self.archive.config()['asr_api_key'], 'secret-test-key')
            with patch.object(voice_service, 'probe', return_value={'available': True, 'models': []}) as probe, patch.object(
                    voice_service, 'find_media', return_value=(b'media', {})), patch.object(
                    voice_service, 'recognize_online', return_value={'text': '在线更新', 'provider': 'dashscope'}) as online:
                web.task(web.TaskReq(account_id=self.manager.account()['id'], action='transcribe', usernames=[self.username], voice_targets=self.targets))
                self.manager.executor.shutdown()
            self.assertEqual(self.manager.status()['task']['status'], 'success')
            probe.assert_called_once_with(online=True)
            online.assert_called_once()
            self.assertEqual(self.archive.transcript(self.username, 'server:103')[2], '在线更新')


if __name__ == '__main__':
    unittest.main()
