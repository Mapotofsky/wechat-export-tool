"""U2 scope snapshots, condition selection and cleanup publication contracts."""
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch
from u2_synthetic import prepare, username
from test_maintenance import seed, table
from web import app as web
from desktop import NativeBridge
from fastapi.testclient import TestClient


class U2Tests(unittest.TestCase):
    def test_large_directory_does_not_expand_conversations_or_execution(self):
        from test_maintenance import shard
        with closing(sqlite3.connect(self.source / 'contact/contact.db')) as c, c:
            c.executemany('INSERT INTO contact VALUES (?,?,3,?,?)',
                          [(f'contact_only_{i}', '', f'目录项{i}', '') for i in range(2000)])
        historical = 'outside_directory@chatroom'
        shard(self.source, 8, historical, [(1, 88001, 1, 8, 1700000000, 'history', 0)])
        with closing(sqlite3.connect(self.source / 'message/message_8.db')) as c, c:
            c.execute('INSERT INTO Name2Id VALUES (?,1)', (historical,))
        self.archive.update(self.source, usernames=[historical])
        (self.source / 'message/message_8.db').unlink()
        pending_history = 'pending_outside@chatroom'
        shard(self.source, 9, pending_history, [])
        with closing(sqlite3.connect(self.source / 'message/message_9.db')) as c, c:
            c.execute('INSERT INTO Name2Id VALUES (?,1)', (pending_history,))
        self.archive.prepare_directory(self.source)
        expected = {username(i) for i in range(320)} | {'test@chatroom', historical, pending_history}
        self.assertGreater(len(self.archive.config()['contact_directory']), 2000)
        self.archive.set_hidden([username(0)])
        expected.remove(username(0))
        self.assertEqual({r['username'] for r in web.contacts()}, expected)
        preview = web.task_preview(web.TaskReq(action='update_all', account_id=self.account))
        self.assertEqual(set(preview['usernames']), expected)
        self.assertEqual({r['username'] for r in preview['pending']},
                         {username(i) for i in range(160, 320)} | {'test@chatroom', pending_history})
        task = web.task(web.TaskReq(action='update_all', account_id=self.account, usernames=preview['usernames']))
        self.manager.executor.submit(lambda: None).result(timeout=30)
        self.assertEqual(self.manager.task['status'], 'success', self.manager.task.get('reason'))
        self.assertEqual(set(task['selected_usernames']), expected)
        self.assertEqual({r['username'] for r in self.archive.contacts()}, expected | {username(0)})
        self.assertEqual(self.archive.message_count(historical), 1)
        self.assertEqual(self.archive.transcript(username(0), 'server:10001')[1], 'success')
        self.assertEqual(self.archive.config()['followed_usernames'], [username(0), username(159), username(200)])

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='u2-data-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.manager, self.source = prepare(self.root)
        self.addCleanup(self.manager.executor.shutdown)
        self.archive = self.manager.archive()
        self.account = self.manager.account()['id']
        p = patch.object(web, 'manager', self.manager)
        p.start()
        self.addCleanup(p.stop)

    def selection(self, **kwargs):
        return web.selection_preview(web.SelectionReq(account_id=self.account, **kwargs))['usernames']

    def purge_request(self, users):
        req = web.HiddenReq(account_id=self.account, usernames=users, purge=True)
        req.expected_counts = web.hidden_preview(req)['counts']
        return req

    def test_http_requires_explicit_account_and_cleanup_preview(self):
        with TestClient(web.app) as client:
            for endpoint, body in (('tasks',dict(action='update',usernames=[username(0)])),
                                   ('export',dict(usernames=[username(0)])),
                                   ('hidden',dict(usernames=[username(0)]))):
                self.assertEqual(client.post('/api/'+endpoint,json=body).status_code,422)
        with self.assertRaisesRegex(ValueError,'先预览'):
            web.save_hidden(web.HiddenReq(account_id=self.account,usernames=[username(0)],purge=True))

    def test_conditions_320_complete_set_unknown_count_and_dates(self):
        self.assertEqual(len(self.selection(category='friend,group')), 321)
        self.assertEqual(len(self.selection(archive_state='imported')), 160)
        self.assertEqual(len(self.selection(archive_state='pending')), 161)
        self.assertEqual(len(self.selection(min_count=0, max_count=0)), 0)
        self.assertEqual(len(self.selection(min_count=0, max_count=0, include_unknown=True)), 161)
        self.assertEqual(self.selection(last_end=1700000001), [username(0)])
        self.assertEqual(len(self.selection(last_end=1700000001, include_unknown=True)), 162)
        self.assertEqual(len(self.selection(filters={'q':'合成群'}, archive_state='pending')), 80)
        self.assertIn(username(319), self.selection(filters={'q':'319'}))

    def test_preview_followed_project_hidden_pending_and_empty(self):
        self.archive.set_hidden([username(0)])
        req = web.TaskReq(account_id=self.account, action='analysis', group_id='u2-project')
        preview = web.task_preview(req)
        self.assertEqual(preview['usernames'], [username(159)])
        self.assertEqual(len(preview['pending']), 2)
        self.assertEqual(preview['excluded'], [username(0)])
        self.assertEqual(web.task_preview(web.TaskReq(action='update', account_id=self.account))['usernames'], [username(159),username(200)])
        self.assertEqual(web.task_preview(web.TaskReq(action='update_all', account_id=self.account))['usernames'].__len__(), 320)
        self.assertEqual(web.task_preview(web.TaskReq(action='update', account_id=self.account, usernames=[]))['usernames'], [])
        with self.assertRaisesRegex(ValueError, '为空'):
            web.task(web.TaskReq(action='update_all', account_id=self.account, usernames=[]))

    def test_all_task_and_retry_use_explicit_snapshot_and_account(self):
        with patch.object(self.manager.executor, 'submit') as dispatch:
            task = web.task(web.TaskReq(account_id=self.account, action='update_all'))
            users = task['selected_usernames'][:]
            self.assertEqual(set(users), {username(i) for i in range(320)} | {'test@chatroom'})
            self.assertEqual(dispatch.call_args.args[-1], users)
            self.archive.save_config({'followed_usernames': [username(1)]})
            self.manager.task = None
            retry = web.task(web.TaskReq(account_id=self.account, action='update_all', usernames=users))
            self.assertEqual(retry['selected_usernames'], users)
        self.manager.task = None
        other = self.root / 'other'
        seed(other)
        self.manager.select(str(other), 'B', True)
        for call in (lambda: web.task(web.TaskReq(account_id=self.account, action='update', usernames=users)),
                     lambda: web.task_preview(web.TaskReq(account_id=self.account, action='analysis', usernames=users)),
                     lambda: self.selection(),
                     lambda: web.hidden_preview(web.HiddenReq(account_id=self.account, usernames=users)),
                     lambda: web.save_hidden(web.HiddenReq(account_id=self.account, usernames=users)),
                     lambda: web.export(web.ExportReq(account_id=self.account, usernames=users))):
            with self.assertRaisesRegex(ValueError, '账号已切换'):
                call()

    def test_cleanup_counts_changed_preview_and_missing_source_history(self):
        u = username(0)
        self.archive.save_transcript(u, 'old-missing', b'old', 'failed', reason='source gone')
        req = web.HiddenReq(account_id=self.account, usernames=[u], purge=True)
        preview = web.hidden_preview(req)
        self.assertEqual(preview['counts'], dict(messages=1, transcripts=2, successful_transcripts=1))
        self.archive.save_transcript(u, 'another-old', b'old', 'success', text='source gone')
        req.expected_counts = preview['counts']
        changed = web.save_hidden(req)
        self.assertTrue(changed['review_required'])
        self.assertEqual(self.archive.message_count(u), 1)
        with closing(sqlite3.connect(self.source / 'message/message_0.db')) as c, c:
            c.execute(f'DELETE FROM "{table(u)}"')
        req.expected_counts = changed['preview']['counts']
        result = web.save_hidden(req)
        self.assertEqual(result['deleted_messages'], 1)
        self.assertTrue(result['space_reclaimed'])
        web.save_hidden(web.HiddenReq(account_id=self.account, usernames=[u], hidden=False))
        self.assertFalse(next(r for r in self.archive.directory_contacts() if r['username'] == u)['imported'])
        self.archive.update(self.source, usernames=[u])
        self.assertEqual(self.archive.message_count(u), 0)
        self.assertIn(u, self.archive.config()['followed_usernames'])
        self.assertIn(u, self.archive.config()['session_groups'][0]['usernames'])

    def test_cleanup_transaction_rolls_back_every_relation_and_setting(self):
        u = username(0)
        before = self.archive.config()
        with closing(self.archive.connect()) as c, c:
            c.execute("CREATE TRIGGER fail_cleanup BEFORE DELETE ON chats BEGIN SELECT RAISE(ABORT,'synthetic transaction failure'); END")
        with self.assertRaisesRegex(sqlite3.Error, 'synthetic'):
            web.save_hidden(self.purge_request([u]))
        self.assertEqual(self.archive.config(), before)
        self.assertEqual(self.archive.message_count(u), 1)
        with closing(self.archive.connect()) as c:
            self.assertEqual(c.execute('SELECT count(*) FROM transcripts WHERE username=?', (u,)).fetchone()[0], 1)
            self.assertGreater(c.execute('SELECT count(*) FROM locations WHERE username=?', (u,)).fetchone()[0], 0)
        self.assertFalse(self.manager.busy())

    def test_space_failure_does_not_undo_cleanup_and_busy_blocks_exit(self):
        entered, release = threading.Event(), threading.Event()
        def reclaim():
            entered.set()
            self.assertTrue(release.wait(8))
            return dict(space_reclaimed=False, warning='档案已清理，空间回收未完成：合成失败')
        result = []
        with patch.object(self.archive, 'reclaim_space', reclaim):
            req = self.purge_request([username(0)])
            thread = threading.Thread(target=lambda: result.append(web.save_hidden(req)))
            thread.start()
            try:
                self.assertTrue(entered.wait(8))
                self.assertTrue(self.manager.busy())
                self.assertIn('回收空间', self.manager.status()['cleanup']['stage'])
                window = unittest.mock.Mock()
                bridge = NativeBridge('http://unused', self.manager.busy)
                bridge._window = window
                self.assertFalse(bridge._can_close())
                with self.assertRaisesRegex(ValueError, '等待'):
                    web.save_hidden(web.HiddenReq(account_id=self.account, usernames=[username(1)]))
                with self.assertRaisesRegex(ValueError, '任务正在执行'):
                    self.manager.select(str(self.source), 'U2 合成账号 A', True)
            finally:
                release.set()
                thread.join(10)
        self.assertTrue(result[0]['saved'])
        self.assertFalse(result[0]['space_reclaimed'])
        self.assertEqual(self.archive.message_count(username(0)), 0)
        self.assertTrue(web.reclaim_space(web.HiddenReq(account_id=self.account, usernames=[]))['space_reclaimed'])

    def test_query_export_and_maintenance_are_independent(self):
        u = username(3)
        self.assertEqual(web.task_preview(web.TaskReq(account_id=self.account, action='update', usernames=[u]))['usernames'], [u])
        self.assertEqual(len(self.archive.messages(u)), 4)
        self.assertEqual(self.archive.search(u, '报销', limit=None)['total'], 3)
        import asyncio, json
        async def read(response):
            return b''.join([part async for part in response.body_iterator])
        full = web.export(web.ExportReq(account_id=self.account, usernames=[u], format='json'))
        hits = web.export_search(web.SearchExportReq(account_id=self.account, usernames=[u], q='报销', format='json'))
        self.assertEqual(json.loads(asyncio.run(read(full)))['total'], 4)
        self.assertEqual(json.loads(asyncio.run(read(hits)))['total'], 3)
        targets = [dict(username=u,message_id='server:10031')]
        with patch.object(self.manager.executor, 'submit'):
            task = web.task(web.TaskReq(account_id=self.account, action='transcribe', usernames=[u], voice_targets=targets))
        self.assertEqual(task['voice_targets'], targets)
        self.assertEqual(task['selected_usernames'], [u])

    def test_actual_vacuum_error_reports_committed_cleanup_and_retry(self):
        class FailVacuum(sqlite3.Connection):
            def execute(self, sql, *args):
                if sql == 'VACUUM':
                    raise sqlite3.OperationalError('synthetic disk full')
                return super().execute(sql, *args)
        with patch.object(self.archive, 'connect', lambda: sqlite3.connect(self.archive.path, factory=FailVacuum)):
            result = web.save_hidden(self.purge_request([username(0)]))
        self.assertTrue(result['saved'])
        self.assertFalse(result['space_reclaimed'])
        self.assertIn('synthetic disk full', result['warning'])
        self.assertTrue(self.archive.config()['space_reclaim_pending'])
        self.assertEqual(self.archive.message_count(username(0)), 0)
        self.assertTrue(web.reclaim_space(web.HiddenReq(account_id=self.account, usernames=[]))['space_reclaimed'])
        self.assertFalse(self.archive.config()['space_reclaim_pending'])
