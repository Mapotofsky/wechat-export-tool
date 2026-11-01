"""Synthetic cancellation and publication boundaries; no real account is accessed."""
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from maintenance import Manager
from task_lifecycle import Cancellation, TaskCancelled
import supervised_process
import voice_service
import encrypted_sync
from web import app as web
from test_maintenance import seed, table


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix='U3 合成 ')
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.source = self.root / 'source'
        self.username = seed(self.source)
        self.manager = Manager(self.root / 'state')
        self.addCleanup(self.manager.executor.shutdown)
        self.account = self.manager.select(str(self.source), 'synthetic', True)
        self.archive = self.manager.archive()
        self.archive.save_config({'self_username': 'wxid_self'})
        self.archive.update(self.source, usernames=[self.username])
        with closing(sqlite3.connect(self.source / 'message/message_0.db')) as c, c:
            c.execute(f'INSERT INTO "{table(self.username)}" VALUES (3,103,1,8,1700000002,?,0)', ('synthetic new',))

    def wait(self):
        deadline = time.monotonic() + 10
        while self.manager.busy() and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertFalse(self.manager.busy())
        return self.manager.status()['task']

    def test_cancel_during_batch_rolls_back_and_retry_uses_scope(self):
        before, baseline = self.archive.messages(self.username), self.archive.config()
        entered, release = threading.Event(), threading.Event()
        original = self.archive._import_batch
        def block(*args):
            original(*args)
            entered.set()
            self.assertTrue(release.wait(5))
        with patch.object(self.archive, '_import_batch', side_effect=block):
            task = self.manager.submit('update', [self.username], account_id=self.account['id'])
            self.assertTrue(entered.wait(5))
            self.assertEqual(self.manager.cancel(task['id'], self.account['id'])['status'], 'cancelling')
            self.assertEqual(self.manager.cancel(task['id'], self.account['id'])['status'], 'cancelling')
            self.assertTrue(self.manager.busy())
            release.set()
            self.assertEqual(self.wait()['status'], 'cancelled')
        self.assertEqual(self.archive.messages(self.username), before)
        self.assertEqual(self.archive.config(), baseline)
        self.assertEqual(self.manager.cancel(task['id'], self.account['id'])['status'], 'cancelled')
        with self.assertRaises(ValueError):
            self.manager.cancel(task['id'], 'another-account')
        self.manager.submit('update', task['selected_usernames'], account_id=self.account['id'])
        self.assertEqual(self.wait()['status'], 'success')
        self.assertEqual(len(self.archive.messages(self.username)), 3)

    def test_cancel_before_or_after_commit_boundary(self):
        original = self.archive.update
        for after_boundary in (False, True):
            entered, release = threading.Event(), threading.Event()
            def update(db_dir, progress, **kwargs):
                def boundary(stage, *args):
                    if stage == '提交消息档案':
                        if after_boundary:
                            progress(stage, *args)
                        entered.set()
                        self.assertTrue(release.wait(5))
                        if not after_boundary:
                            progress(stage, *args)
                    else:
                        progress(stage, *args)
                return original(db_dir, boundary, **kwargs)
            before = self.archive.config()
            with patch.object(self.archive, 'update', side_effect=update):
                task = self.manager.submit('update', [self.username], account_id=self.account['id'])
                self.assertTrue(entered.wait(5))
                status = self.manager.cancel(task['id'], self.account['id'])
                self.assertEqual(status['status'], 'running' if after_boundary else 'cancelling')
                release.set()
                result = self.wait()
            self.assertEqual(result['status'], 'success' if after_boundary else 'cancelled')
            if not after_boundary:
                self.assertEqual(self.archive.config(), before)
            else:
                self.assertEqual(len(self.archive.messages(self.username)), 3)

    def test_restart_marks_cancelling_interrupted_and_preserves_scope(self):
        path = self.archive.directory / 'task.json'
        path.write_text(json.dumps({'id':'fixture', 'status':'cancelling', 'account_id':self.account['id'],
                                    'selected_usernames':[self.username]}), encoding='utf-8')
        restarted = Manager(self.manager.root)
        self.addCleanup(restarted.executor.shutdown)
        task = restarted.status()['task']
        self.assertEqual(task['status'], 'interrupted')
        self.assertEqual(task['selected_usernames'], [self.username])
        self.assertEqual(len(restarted.archive().messages(self.username)), 2)

    def test_cancel_is_not_blocked_by_view_position_write_waiting_for_sqlite(self):
        task = {'id':'active', 'account_id':self.account['id'], 'status':'running', 'cancellable':True}
        self.manager.task = task
        self.manager.cancellation = Cancellation(self.manager.lock)
        entered = threading.Event()
        original = self.archive.save_config
        def save(values):
            entered.set()
            return original(values)
        request = web.ViewStateReq(account_id=self.account['id'], active=self.username, offset=0, scroll=0)
        with closing(self.archive.connect()) as writer:
            writer.execute('BEGIN IMMEDIATE')
            with patch.object(web, 'manager', self.manager), patch.object(self.archive, 'save_config', side_effect=save):
                thread = threading.Thread(target=web.save_view_state, args=(request,))
                thread.start()
                self.assertTrue(entered.wait(5))
                started = time.monotonic()
                self.assertEqual(self.manager.cancel('active', self.account['id'])['status'], 'cancelling')
                self.assertLess(time.monotonic() - started, 2)
                writer.rollback()
                thread.join(5)
                self.assertFalse(thread.is_alive())

    def test_voice_cancellation_keeps_previous_success(self):
        with closing(sqlite3.connect(self.source / 'message/message_0.db')) as c, c:
            c.execute(f'INSERT INTO "{table(self.username)}" VALUES (4,104,34,8,1700000003,?,0)', ('voice',))
        self.archive.update(self.source, usernames=[self.username])
        self.archive.save_transcript(self.username, 'server:104', b'old', 'success', text='old')
        signal = Cancellation()
        def recognizer(*args):
            signal.event.set()
            return 'new'
        with patch.object(voice_service, 'find_media', return_value=(b'new', {})):
            with self.assertRaises(TaskCancelled):
                voice_service.transcribe(self.archive, self.source, [self.username], '', recognizer=recognizer,
                    targets=[{'username':self.username,'message_id':'server:104'}], force=True, cancellation=signal)
        self.assertEqual(self.archive.transcript(self.username, 'server:104')[1:], ('success', 'old'))

    def test_published_voice_is_reused_after_cancellation(self):
        with closing(sqlite3.connect(self.source / 'message/message_0.db')) as c, c:
            c.executemany(f'INSERT INTO "{table(self.username)}" VALUES (?,?,34,8,?,"voice",0)',
                          [(4, 104, 1700000003), (5, 105, 1700000004)])
        self.archive.update(self.source, usernames=[self.username])
        signal = Cancellation()
        calls = []
        def recognize(*args):
            calls.append(1)
            if len(calls) == 2:
                signal.event.set()
            return 'synthetic success'
        with patch.object(voice_service, 'find_media', return_value=(b'media', {})):
            with self.assertRaises(TaskCancelled) as caught:
                voice_service.transcribe(self.archive, self.source, [self.username], '', recognizer=recognize, cancellation=signal)
            self.assertEqual(caught.exception.result['success'], 1)
            self.assertEqual(caught.exception.result['remaining'], 1)
            result = voice_service.transcribe(self.archive, self.source, [self.username], '', recognizer=lambda *_:'retry')
        self.assertEqual(result['reused'], 1)
        self.assertEqual(result['success'], 1)

    def test_online_request_waits_for_local_server_then_keeps_old_success(self):
        with closing(sqlite3.connect(self.source / 'message/message_0.db')) as c, c:
            c.execute(f'INSERT INTO "{table(self.username)}" VALUES (4,104,34,8,1700000003,?,0)', ('voice',))
        self.archive.update(self.source, usernames=[self.username])
        self.archive.save_transcript(self.username, 'server:104', b'old', 'success', text='old')
        entered, release = threading.Event(), threading.Event()
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                self.rfile.read(int(self.headers['Content-Length']))
                entered.set()
                release.wait(5)
                body = b'{"text":"synthetic"}'
                self.send_response(200)
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            def log_message(self, *args):
                pass
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        signal, caught = Cancellation(), []
        config = {'asr_provider':'openai', 'asr_api_url':f'http://127.0.0.1:{server.server_port}/', 'asr_api_model':'synthetic'}
        def run():
            try:
                with patch.object(voice_service, 'find_media', return_value=(b'new', {})):
                    voice_service.transcribe(self.archive, self.source, [self.username], '',
                        recognizer=lambda *_: voice_service.request_online(b'synthetic', config),
                        targets=[{'username':self.username,'message_id':'server:104'}], force=True, cancellation=signal)
            except BaseException as exc:
                caught.append(exc)
        thread = threading.Thread(target=run)
        thread.start()
        self.assertTrue(entered.wait(5))
        signal.event.set()
        self.assertTrue(thread.is_alive())
        release.set()
        thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertIsInstance(caught[0], TaskCancelled)
        self.assertEqual(self.archive.transcript(self.username, 'server:104')[1:], ('success', 'old'))

    def test_cleanup_failure_blocks_exit_until_retry(self):
        task = {'id': 'fixture', 'account_id': self.account['id'], 'status': 'running', 'stage': '准备'}
        self.manager.task = task
        generation = self.archive.directory / 'decrypted' / 'staging'
        generation.mkdir(parents=True)
        (generation / 'temporary').write_text('synthetic')
        self.archive.save_config({'keys_file': 'synthetic'})
        def fail_sync(*args, **kwargs):
            from task_lifecycle import CleanupError
            raise CleanupError(generation, True, PermissionError('synthetic'))
        with patch.object(encrypted_sync, 'sync', side_effect=fail_sync):
            self.manager.perform({'id':self.account['id'], 'mode':'encrypted', 'directory':str(self.source)},
                                 self.archive, task, 'prepare', [])
        self.assertEqual(task['status'], 'failed')
        self.assertTrue(self.manager.busy())
        self.assertTrue(generation.exists())
        with patch('maintenance.shutil.rmtree', side_effect=PermissionError('synthetic retry failure')):
            with self.assertRaises(PermissionError):
                self.manager.retry_task_cleanup(task['id'], self.account['id'])
        self.assertIn('未清理完', self.manager.cleanup['stage'])
        self.manager.retry_task_cleanup(task['id'], self.account['id'])
        self.assertFalse(self.manager.busy())
        self.assertFalse(generation.exists())
        self.assertEqual(task['reason'], '')
        self.assertEqual(task['stage'], '资源清理完成')

    @unittest.skipUnless(os.name == 'nt', 'Windows synthetic CNG encryption')
    def test_snapshot_and_decrypt_cancel_remove_only_new_generation(self):
        from test_encrypted_wal import reserved_pages, encrypted_page
        source = self.root / 'encrypted'
        (source / 'contact').mkdir(parents=True)
        (source / 'message').mkdir()
        key, salt = b'k' * 32, b's' * 16
        pages = reserved_pages(self.root / 'plain.db', 'synthetic')
        encrypted = b''.join(encrypted_page(page, i + 1, key, salt) for i, page in enumerate(pages))
        for relative in ('contact/contact.db', 'message/message_0.db'):
            (source / relative).write_bytes(encrypted)
        keys = self.root / 'synthetic-keys.json'
        keys.write_text(json.dumps({relative: {'enc_key': key.hex()} for relative in
                                   ('contact/contact.db', 'message/message_0.db')}))
        account_dir = self.root / 'isolated-account'
        previous = account_dir / 'decrypted' / 'previous'
        previous.mkdir(parents=True)
        (previous / 'sentinel').write_text('published')
        for stage in ('复制快照', '解密主数据库页面'):
            def progress(current, *args):
                if current == stage:
                    raise TaskCancelled('synthetic')
            with self.assertRaises(TaskCancelled):
                encrypted_sync.sync(source, account_dir, keys, [previous], progress)
            self.assertEqual([p.name for p in previous.parent.iterdir()], ['previous'])
            self.assertEqual((previous / 'sentinel').read_text(), 'published')
            # Reopening the source after cancellation also checks the snapshot handle was released.
            with (source / 'contact/contact.db').open('r+b') as f:
                self.assertTrue(f.read(16))

    @unittest.skipUnless(os.name == 'nt', 'Windows process tree')
    def test_cancel_process_tree_and_release_files(self):
        parent, child, marker = self.root / 'parent.py', self.root / 'child.py', self.root / 'child.pid'
        child.write_text('import os,sys,time\nfrom pathlib import Path\nPath(sys.argv[1]).write_text(str(os.getpid()))\ntime.sleep(60)', encoding='utf-8')
        parent.write_text('import subprocess,sys,time\nsubprocess.Popen([sys.executable,sys.argv[1],sys.argv[2]])\ntime.sleep(60)', encoding='utf-8')
        signal = Cancellation()
        caught = []
        def run():
            try:
                supervised_process.run([sys.executable, str(parent), str(child), str(marker)], cancellation=signal, timeout=10)
            except BaseException as exc:
                caught.append(exc)
        thread = threading.Thread(target=run)
        thread.start()
        deadline = time.monotonic() + 5
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(.02)
        self.assertTrue(marker.exists())
        pid = int(marker.read_text())
        signal.event.set()
        thread.join(10)
        self.assertFalse(thread.is_alive())
        self.assertIsInstance(caught[0], TaskCancelled)
        import ctypes
        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
        if handle:
            code = ctypes.c_ulong()
            ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
            ctypes.windll.kernel32.CloseHandle(handle)
            self.assertNotEqual(code.value, 259)
        marker.unlink()

    @unittest.skipUnless(os.name == 'nt', 'Windows process tree')
    def test_voice_worker_timeout_kills_downstream_process(self):
        worker, child, marker = self.root / 'worker.py', self.root / 'decoder.py', self.root / 'decoder.pid'
        child.write_text('import os,sys,time\nfrom pathlib import Path\nPath(sys.argv[1]).write_text(str(os.getpid()))\ntime.sleep(60)', encoding='utf-8')
        worker.write_text('import subprocess,sys,time\nsubprocess.Popen([sys.executable,sys.argv[1],sys.argv[2]])\ntime.sleep(60)', encoding='utf-8')
        with patch.object(voice_service, 'WORKER', worker):
            with self.assertRaises(subprocess.TimeoutExpired):
                voice_service.run_worker(sys.executable, str(child), str(marker), timeout=2, cancellation=Cancellation())
        self.assertTrue(marker.exists())
        pid = int(marker.read_text())
        import ctypes
        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
        if handle:
            code = ctypes.c_ulong()
            ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
            ctypes.windll.kernel32.CloseHandle(handle)
            self.assertNotEqual(code.value, 259)

    @unittest.skipUnless(os.name == 'nt', 'Windows process tree')
    def test_cancel_voice_worker_and_downstream_process(self):
        worker, child, marker = self.root / 'worker.py', self.root / 'decoder.py', self.root / 'decoder.pid'
        child.write_text('import os,sys,time\nfrom pathlib import Path\nPath(sys.argv[1]).write_text(str(os.getpid()))\ntime.sleep(60)', encoding='utf-8')
        worker.write_text('import subprocess,sys,time\nsubprocess.Popen([sys.executable,sys.argv[1],sys.argv[2]])\ntime.sleep(60)', encoding='utf-8')
        signal, caught = Cancellation(), []
        def run():
            try:
                voice_service.run_worker(sys.executable, str(child), str(marker), timeout=20, cancellation=signal)
            except BaseException as exc:
                caught.append(exc)
        with patch.object(voice_service, 'WORKER', worker):
            thread = threading.Thread(target=run)
            thread.start()
            deadline = time.monotonic() + 5
            while not marker.exists() and time.monotonic() < deadline:
                time.sleep(.02)
            self.assertTrue(marker.exists())
            pid = int(marker.read_text())
            signal.event.set()
            thread.join(10)
            self.assertFalse(thread.is_alive())
            self.assertIsInstance(caught[0], TaskCancelled)
        import ctypes
        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
        if handle:
            code = ctypes.c_ulong()
            ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
            ctypes.windll.kernel32.CloseHandle(handle)
            self.assertNotEqual(code.value, 259)

    @unittest.skipUnless(os.name == 'nt', 'Windows process tree')
    def test_cancel_key_tool_preserves_old_key_configuration(self):
        tool = self.root / 'wcdb_key_tool_windows.py'
        marker = self.root / 'tool-ready'
        tool.write_text('import sys,time\nfrom pathlib import Path\n'
                        'Path(sys.argv[sys.argv.index("--output")+1]).write_text("partial")\n'
                        f'Path({str(marker)!r}).write_text("ready")\n'
                        'time.sleep(60)', encoding='utf-8')
        self.archive.save_config({'keys_file':'old synthetic key'})
        self.manager.cancellation = Cancellation(self.manager.lock)
        task = {'id':'tool-task', 'account_id':self.account['id'], 'status':'running', 'stage':'准备', 'cancellable':True}
        self.manager.task = task
        with patch('maintenance.resource_root', return_value=self.root), patch('maintenance.python_executable', return_value=Path(sys.executable)):
            thread = threading.Thread(target=self.manager.perform,
                args=({'id':self.account['id'], 'mode':'encrypted', 'directory':str(self.source)}, self.archive, task, 'extract', []))
            thread.start()
            deadline = time.monotonic() + 5
            while not marker.exists() and time.monotonic() < deadline:
                time.sleep(.02)
            self.assertTrue(marker.exists())
            self.manager.cancel(task['id'], self.account['id'])
            thread.join(10)
            self.assertFalse(thread.is_alive())
        self.assertEqual(task['status'], 'cancelled')
        self.assertEqual(self.archive.config()['keys_file'], 'old synthetic key')
        self.assertEqual(list(self.archive.directory.glob('keys_*.json')), [])
