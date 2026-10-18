"""Account-local groups, hidden conversations and destructive cleanup on synthetic data."""
from contextlib import closing
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from maintenance import Manager
from test_maintenance import seed, shard
from web import app as web


class ProjectTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / 'source'
        self.chat = seed(self.source)
        shard(self.source, 1, 'wxid_a', [(1, 201, 1, 8, 1700000000, 'other conversation', 0)])
        self.manager = Manager(self.root / 'state')
        self.addCleanup(self.manager.executor.shutdown)
        self.account = self.manager.select(str(self.source), 'synthetic projects', True)
        self.archive = self.manager.archive()
        self.archive.save_config({'self_username': 'wxid_self', 'followed_usernames': [self.chat]})
        self.archive.update(self.source)
        self.patcher = patch.object(web, 'manager', self.manager)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

    def create_group(self, name, members):
        return web.save_group(web.GroupReq(name=name, usernames=members))['id']

    def test_overlapping_groups_followed_types_and_fixed_task_scope(self):
        first = self.create_group('项目一', [self.chat, 'wxid_a', self.chat, 'wxid_b'])
        second = self.create_group('项目二', [self.chat])
        scope = web.group_scope(first)
        self.assertEqual(len(scope['members']), 3)
        self.assertEqual({r['username'] for r in scope['imported']}, {self.chat, 'wxid_a'})
        self.assertEqual([r['username'] for r in scope['pending']], ['wxid_b'])
        self.assertEqual(web.groups()[0]['usernames'], [self.chat])
        self.assertEqual(len(web.contacts(category='friend,group', group_id=first)), 3)
        self.assertEqual([r['username'] for r in web.contacts(category='group', group_id=first)], [self.chat])
        web.save_hidden(web.HiddenReq(usernames=[self.chat]))
        self.assertEqual([r['username'] for r in web.group_scope(second)['members']], [])
        with patch.object(self.manager.executor, 'submit') as dispatch:
            task = web.task(web.TaskReq(action='update', group_id=first))
            self.assertEqual(task['selected_usernames'], ['wxid_a', 'wxid_b'])
            self.assertEqual(dispatch.call_args.args[-1], ['wxid_a', 'wxid_b'])
            with self.assertRaisesRegex(ValueError, '等待'):
                web.save_hidden(web.HiddenReq(usernames=['wxid_a']))
        self.manager.task = None
        web.save_group(web.GroupReq(id=second, delete=True))
        self.assertEqual(self.archive.message_count(self.chat), 2)
        self.assertIn(self.chat, self.archive.config()['followed_usernames'])
        with self.assertRaisesRegex(ValueError, '内置'):
            web.save_group(web.GroupReq(id='followed', delete=True))

    def test_hidden_is_persistent_and_all_update_does_not_reimport(self):
        group = self.create_group('保留归属', [self.chat])
        self.archive.save_config({'view_state': {'active': self.chat}})
        web.save_hidden(web.HiddenReq(usernames=[self.chat]))
        self.assertNotIn(self.chat, [r['username'] for r in web.contacts()])
        self.assertEqual(web.view_state(), {})
        self.assertEqual(self.archive.message_count(self.chat), 2)
        for read in (lambda: web.messages(self.chat), lambda: web.context(self.chat, 'server:101'),
                     lambda: web.export(web.ExportReq(usernames=[self.chat])),
                     lambda: web.analyze(web.AnalysisReq(account_id=self.account['id'], usernames=[self.chat])),
                     lambda: web.save_view_state(web.ViewStateReq(account_id=self.account['id'], active=self.chat))):
            with self.assertRaisesRegex(ValueError, '隐藏'):
                read()
        self.archive.update(self.source)
        self.assertNotIn(self.chat, [r['username'] for r in web.contacts()])
        self.assertEqual(web.hidden_chats()[0]['username'], self.chat)
        web.save_hidden(web.HiddenReq(usernames=[self.chat], hidden=False))
        self.assertEqual([r['username'] for r in web.group_scope(group)['members']], [self.chat])
        self.assertTrue(next(r for r in web.contacts() if r['username'] == self.chat)['imported'])

    def test_purge_clears_all_relations_and_baseline_then_restores_from_source(self):
        group = self.create_group('恢复导入', [self.chat])
        self.archive.save_transcript(self.chat, 'server:101', b'synthetic voice', 'success', text='saved')
        result = web.save_hidden(web.HiddenReq(usernames=[self.chat], purge=True))
        self.assertEqual(result['deleted_messages'], 2)
        self.assertTrue(result['space_reclaimed'])
        with closing(self.archive.connect()) as c:
            for table in ('chats', 'messages', 'transcripts', 'locations', 'current_sources'):
                self.assertEqual(c.execute(f'SELECT count(*) FROM {table} WHERE username=?', (self.chat,)).fetchone()[0], 0)
        self.assertNotIn(self.chat, self.archive.config()['conversation_baselines'])
        self.assertEqual(self.archive.message_count('wxid_a'), 1)
        self.archive.update(self.source)
        self.assertEqual(self.archive.message_count(self.chat), 0)
        web.save_hidden(web.HiddenReq(usernames=[self.chat], hidden=False))
        self.assertEqual(web.group_scope(group)['pending'][0]['username'], self.chat)
        self.archive.update(self.source, usernames=[self.chat])
        self.assertEqual(self.archive.message_count(self.chat), 2)
        self.assertEqual(web.group_scope(group)['pending'], [])

    def test_account_isolation_and_restart(self):
        group = self.create_group('账号一项目', [self.chat])
        web.save_hidden(web.HiddenReq(usernames=[self.chat]))
        reopened = Manager(self.root / 'state')
        self.addCleanup(reopened.executor.shutdown)
        self.assertEqual(reopened.archive().groups()[-1]['id'], group)
        self.assertEqual(reopened.archive().config()['hidden_usernames'], [self.chat])
        other = self.root / 'other'
        seed(other)
        self.manager.select(str(other), '账号二', True)
        self.assertEqual(len(web.groups()), 1)
        self.assertEqual(web.hidden_chats(), [])


if __name__ == '__main__':
    unittest.main()
