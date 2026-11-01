from contextlib import closing
import errno
import json
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import desktop
import encrypted_sync
from maintenance import Manager
import preparation
import runtime_paths
from test_maintenance import seed, table
from web import app as web


class U4Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='U4 中文 空格 ')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / '源 A'
        self.user = seed(self.source)
        self.manager = Manager(self.root / '数据')
        self.addCleanup(self.manager.executor.shutdown)
        self.a = self.manager.select(str(self.source), '账号 A', True)
        self.archive = self.manager.archive()
        self.archive.save_config({'self_username': 'wxid_self'})
        self.archive.update(self.source)

    def test_switch_without_source_preserves_state_and_busy_rejects(self):
        self.archive.save_config({'view_state': {'active': self.user, 'scroll': 88}, 'followed_usernames': [self.user]})
        other = self.root / '源 B'
        seed(other)
        b = self.manager.select(str(other), '账号 B', True)
        self.source.rename(self.root / '源 A 已移走')
        self.manager.switch(self.a['id'])
        self.assertEqual(self.manager.archive().message_count(self.user), 2)
        self.assertEqual(self.manager.archive().config()['view_state']['scroll'], 88)
        self.assertFalse(self.manager.status()['setup']['source_available'])
        for state in ('maintenance', 'cleanup'):
            self.manager.task = {'status': 'running'} if state == 'maintenance' else None
            self.manager.cleanup = {'stage': '清理'} if state == 'cleanup' else None
            with self.assertRaisesRegex(ValueError, '维护或清理'):
                self.manager.switch(b['id'])
        self.manager.task = self.manager.cleanup = None
        restarted = Manager(self.manager.root)
        self.addCleanup(restarted.executor.shutdown)
        self.assertEqual(restarted.account()['id'], self.a['id'])

    def test_file_sets_and_measured_capacity(self):
        (self.source / 'message/media_0.db').write_bytes(b'media')
        (self.source / 'message/message_fts.db').touch()
        (self.source / 'message/message_0.db-wal').write_bytes(b'wal')
        account = dict(self.a, mode='encrypted')
        expected = {'contact.db', 'message_0.db'}
        for action in ('prepare', 'discover', 'update', 'update_all'):
            plan = preparation.plan(account, self.archive, action, [self.user])
            self.assertEqual({p.name for p in plan['files']}, expected)
        self.archive.save_transcript(self.user, 'server:101', b'voice', 'success', text='合成')
        plan = preparation.plan(account, self.archive, 'update', [self.user])
        self.assertEqual({p.name for p in plan['files']}, expected | {'media_0.db'})
        self.assertEqual(encrypted_sync.preparation_files(self.source, False, True), plan['files'])
        info = preparation.capacity(plan, self.archive)
        actual = sum(p.stat().st_size for p in plan['files']) + 3
        self.assertEqual(info['copy_bytes'], actual)
        with patch.object(preparation.shutil, 'disk_usage', return_value=SimpleNamespace(free=actual-1)):
            info = preparation.capacity(plan, self.archive)
            self.assertTrue(info['blocked'])
            with self.assertRaisesRegex(ValueError, '空间不足'):
                preparation.require_capacity(info)
        with patch.object(preparation.shutil, 'disk_usage', side_effect=OSError('unknown')):
            info = preparation.capacity(plan, self.archive)
            self.assertIsNone(info['volumes'][0]['free_bytes'])
            self.assertFalse(info['blocked'])

    def test_voice_file_selection_skip_and_multiple_volumes(self):
        with closing(sqlite3.connect(self.source / 'message/message_0.db')) as c, c:
            c.execute(f'UPDATE "{table(self.user)}" SET local_type=34')
        self.archive.update(self.source)
        self.archive.save_transcript(self.user, 'server:101', None, 'failed', reason='synthetic')
        self.archive.save_transcript(self.user, 'server:102', None, 'failed', reason='synthetic')
        account = dict(self.a, mode='encrypted')
        (self.source / 'message/media_0.db').write_bytes(b'media')
        plan = preparation.plan(account, self.archive, 'transcribe', [self.user])
        self.assertEqual({p.name for p in plan['files']}, {'contact.db', 'media_0.db'})
        with patch.object(preparation, 'volume', side_effect=['data-volume', 'temp-volume']), patch.object(
                preparation.shutil, 'disk_usage', return_value=SimpleNamespace(free=999999)):
            self.assertEqual(len(preparation.capacity(plan, self.archive)['volumes']), 2)
        self.archive.set_voice_skipped([{'username': self.user, 'message_id': 'server:101'}, {'username': self.user, 'message_id': 'server:102'}], True)
        plan = preparation.plan(account, self.archive, 'transcribe', [self.user])
        self.assertEqual(plan['files'], [])
        self.assertFalse(plan['media'])
        self.a['mode'] = 'encrypted'
        self.source.rename(self.root / 'voice source removed')
        with patch('voice_service.probe', side_effect=AssertionError('all skipped must not probe')):
            self.manager.submit('transcribe', [self.user])
            self.manager.executor.shutdown(wait=True)
        self.assertEqual(self.manager.task['status'], 'success')
        self.assertEqual(self.manager.task['result']['skipped'], 2)

    def test_group_voice_preconditions_use_resolved_members(self):
        with closing(sqlite3.connect(self.source / 'message/message_0.db')) as c, c:
            c.execute(f'UPDATE "{table(self.user)}" SET local_type=34')
        self.archive.update(self.source)
        self.archive.save_config({'followed_usernames': [self.user]})
        self.source.rename(self.root / 'group source removed')
        with patch.object(self.manager.executor, 'submit') as submit:
            with self.assertRaisesRegex(ValueError, '请选择 db_storage'):
                self.manager.submit('transcribe', group_id='followed')
            submit.assert_not_called()

    def test_submit_blocks_known_shortage_before_worker(self):
        with patch.object(self.manager, 'preparation_preview', return_value={'blocked': True, 'volumes': [
                {'free_bytes': 1, 'necessary_bytes': 2, 'volume': 'D:'}]}), patch.object(self.manager.executor, 'submit') as submit:
            with self.assertRaisesRegex(ValueError, '空间不足'):
                self.manager.submit('update', [self.user])
            submit.assert_not_called()

    def test_task_record_disk_full_does_not_leave_busy_instance(self):
        with patch('maintenance.atomic_json', side_effect=OSError(errno.ENOSPC, 'No space left')):
            with self.assertRaises(OSError):
                self.manager.submit('update', [self.user])
        self.assertFalse(self.manager.busy())
        self.assertEqual(self.archive.message_count(self.user), 2)

    def test_disk_full_before_publication_preserves_archive_and_config(self):
        before = self.archive.config()
        with patch.object(self.archive, 'update', side_effect=sqlite3.OperationalError('database or disk is full')):
            self.manager.submit('update', [self.user])
            self.manager.executor.shutdown(wait=True)
        self.assertEqual(self.archive.config(), before)
        self.assertEqual(self.archive.message_count(self.user), 2)
        self.assertEqual(self.manager.task['status'], 'failed')
        self.assertIn('disk is full', self.manager.task['reason'])

    def test_startup_failures_are_caught_before_log_or_lock(self):
        for failure in (1, 2):
            effects = [None] * (failure-1) + [PermissionError(errno.EACCES, '拒绝访问', str(self.root / '禁止写入'))]
            with patch.object(desktop, 'check_writable', side_effect=effects), patch.object(desktop.ctypes.windll.user32, 'MessageBoxW') as message:
                self.assertEqual(desktop.main([]), 1)
                self.assertIn(str(runtime_paths.data_root()), message.call_args.args[1])
                self.assertIn('WECHAT_ARCHIVE_DATA_DIR', message.call_args.args[1])

    def test_real_sqlite_full_rolls_back_import(self):
        with closing(sqlite3.connect(self.source / 'message/message_0.db')) as c, c:
            c.execute(f'UPDATE "{table(self.user)}" SET message_content=? WHERE local_id=1', ('synthetic ' * 20000,))
        connect = self.archive.connect
        def limited():
            connection = connect()
            pages = connection.execute('PRAGMA page_count').fetchone()[0]
            connection.execute(f'PRAGMA max_page_count={pages}')
            return connection
        before = self.archive.messages(self.user)
        config = self.archive.config()
        with patch.object(self.archive, 'connect', side_effect=limited):
            self.manager.submit('update', [self.user])
            self.manager.executor.shutdown(wait=True)
        self.assertEqual(self.manager.task['diagnostic_code'], 'disk_full')
        self.assertEqual(self.archive.messages(self.user), before)
        self.assertEqual(self.archive.config(), config)

    def test_encrypted_copy_full_removes_staging_and_preserves_old(self):
        old = self.archive.directory / 'decrypted/old'
        old.mkdir(parents=True)
        (old / 'retained').write_text('published')
        keys = self.root / 'synthetic-keys.json'
        keys.write_text('{}')
        with patch.object(encrypted_sync, 'copy_chunks', side_effect=OSError(errno.ENOSPC, 'No space left')):
            with self.assertRaises(OSError):
                encrypted_sync.sync(self.source, self.archive.directory, keys)
        self.assertEqual(list(old.parent.iterdir()), [old])
        self.assertEqual((old / 'retained').read_text(), 'published')

    def test_resource_open_is_restricted_and_summary_does_not_probe(self):
        bridge = desktop.NativeBridge('', lambda: False)
        with patch.object(desktop.os, 'startfile') as start:
            self.assertFalse(bridge.open_resource('arbitrary')['opened'])
            self.assertTrue(bridge.open_resource('data')['opened'])
            self.assertEqual(start.call_args.args[0], str(runtime_paths.data_root()))
            self.assertFalse(bridge.open_path(str(self.root))['opened'])
        with patch.object(web.voice_service, 'probe', side_effect=AssertionError('no worker on status')):
            self.assertEqual(web.running_resources()['data'], str(runtime_paths.data_root()))
        with patch.object(web, 'manager', self.manager):
            web.switch_account(web.SwitchReq(account_id=self.a['id']))
            self.assertEqual(web.task_preview(web.TaskReq(action='prepare', account_id=self.a['id']))['capacity']['files'], [])

    def test_log_creation_and_lock_errors_use_startup_recovery(self):
        for name, target, stage in [('logging.basicConfig', runtime_paths.logs_root() / 'desktop.log', '创建启动日志'),
                                    ('InstanceLock.acquire', runtime_paths.data_root() / 'desktop.lock', '获取实例锁')]:
            with patch.object(desktop, 'check_writable'), patch('desktop.' + name,
                    side_effect=PermissionError(13, 'Permission denied')), patch.object(
                    desktop.ctypes.windll.user32, 'MessageBoxW') as message:
                self.assertEqual(desktop.main([]), 1)
                self.assertIn(str(target), message.call_args.args[1])
                self.assertIn(stage, message.call_args.args[1])
