"""Real manager with synthetic media, a simulated decoder and loopback ASR."""
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import sqlite3
import threading
import time
import unittest
import json
from unittest.mock import patch

import test_u3
from test_maintenance import table
import voice_service
from maintenance import Manager, atomic_json
from web import app as web


class FeedbackTests(unittest.TestCase):
    setUp = test_u3.LifecycleTests.setUp
    wait = test_u3.LifecycleTests.wait

    def test_internal_http_and_saved_stages_remain_equal(self):
        task = dict(id='synthetic-raw-stage', account_id=self.account['id'], action='update',
                    status='failed', stage='提交消息档案', failed_stage='提取与语音转写',
                    done=1, total=3, unit='个会话', cancellable=False)
        self.manager.task = task
        path = self.archive.directory / 'task.json'
        atomic_json(path, task)
        with patch.object(web, 'manager', self.manager):
            returned = web.data_status()['task']
        saved = json.loads(path.read_text(encoding='utf-8'))
        self.assertEqual(returned, task)
        self.assertEqual(saved, task)
        restarted = Manager(self.manager.root)
        self.addCleanup(restarted.executor.shutdown)
        self.assertEqual(restarted.status()['task'], task)
        self.assertEqual((returned['done'], returned['total'], returned['unit'], returned['cancellable']),
                         (1, 3, '个会话', False))
    def test_online_activity_preserves_batch_count_and_original_scope(self):
        with closing(sqlite3.connect(self.source / 'message/message_0.db')) as c, c:
            c.execute(f'UPDATE "{table(self.username)}" SET local_type=34')
        self.archive.update(self.source, usernames=[self.username])
        self.archive.save_config({'asr_provider':'openai', 'asr_api_url':'http://127.0.0.1:1', 'asr_api_model':'synthetic'})
        entered, release = threading.Event(), threading.Event()
        test = self
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                self.rfile.read(int(self.headers['Content-Length']))
                entered.set()
                test.assertTrue(release.wait(5))
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'{"text":"synthetic"}')
            def log_message(self, *args):
                pass
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.archive.save_config({'asr_api_url':f'http://127.0.0.1:{server.server_port}'})
        def decoder(*args, **kwargs):
            from types import SimpleNamespace
            Path(args[args.index('--decode-output')+1]).write_bytes(b'synthetic wav')
            return SimpleNamespace(returncode=0)
        with patch.object(voice_service, 'probe', return_value={'available':True}), patch.object(
                voice_service, 'find_media', return_value=(b'synthetic', {})), patch.object(
                voice_service, 'run_worker', side_effect=decoder):
            task = self.manager.submit('transcribe', [self.username], account_id=self.account['id'])
            self.assertTrue(entered.wait(5))
            waiting = self.manager.status()['task']
            self.assertEqual((waiting['done'], waiting['total'], waiting['unit']), (0, 3, '条语音'))
            self.assertEqual(waiting['stage'], '提取与语音转写')
            self.assertIn('等待当前语音响应', waiting['activity'])
            self.assertNotIn('180', waiting['activity'])
            self.manager.cancel(task['id'], self.account['id'])
            self.assertTrue(self.manager.busy())
            release.set()
            self.assertEqual(self.wait()['status'], 'cancelled')
            self.assertEqual(self.manager.status()['task']['selected_usernames'], [self.username])
