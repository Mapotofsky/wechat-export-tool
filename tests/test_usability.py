import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ui_fixture_server import rich_seed
from maintenance import Manager
from web import app as web


class UsabilityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        source = root / 'source'
        rich_seed(source)
        self.manager = Manager(root / 'state')
        self.addCleanup(self.manager.executor.shutdown)
        self.manager.select(str(source), 'synthetic', True)
        self.manager.archive().save_config({'self_username': 'wxid_self'})
        self.manager.archive().update(source)
        self.patch = patch.object(web, 'manager', self.manager)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def test_identity_fields_group_association_and_duplicates(self):
        for q in (' 项目备注 ', '独立昵称', 'public_alice', 'wxid_a'):
            rows = web.contacts(q=q)
            usernames = [r['username'] for r in rows]
            self.assertEqual(len(usernames), len(set(usernames)))
            self.assertTrue({'wxid_a', 'test@chatroom', 'second@chatroom'} <= set(usernames))
            group = next(r for r in rows if r['username'] == 'second@chatroom')
            self.assertIn('已收录发送者', ' '.join(group['match_reasons']))
        self.assertIn('wxid_a', {r['username'] for r in web.contacts(q='项目备注')})
        self.assertNotIn('wxid_b', {r['username'] for r in web.contacts(q='项目备注')})

    def test_activity_is_real_message_and_unknown_is_explicit(self):
        start, end = 1700086400, 1700172800
        rows = web.contacts(start=start, end=end, date_mode='activity')
        self.assertNotIn('wxid_a', {r['username'] for r in rows})
        self.assertIn('second@chatroom', {r['username'] for r in rows})
        self.assertNotIn('pending', {r['username'] for r in rows})
        self.assertNotIn('pending', {r['username'] for r in web.contacts(start=start, end=end, date_mode='activity', include_unknown=True)})

    def test_preview_pages_count_and_export_share_range(self):
        username = 'second@chatroom'
        first = web.preview(username)
        earlier = web.preview(username, offset=5)
        self.assertEqual(len(first), 5)
        self.assertEqual(len(earlier), 3)
        self.assertLess(earlier[-1]['timestamp'], first[0]['timestamp'])
        start, end = 1700172800, 1700345600
        messages = web.preview(username, start, end)
        self.assertEqual(web.message_count(username, start, end)['total'], len(messages))
        async def body(response):
            return b''.join([p async for p in response.body_iterator])
        result = json.loads(asyncio.run(body(web.export(web.ExportReq(account_id=self.manager.account()['id'], usernames=[username], format='json', start=start, end=end)))))
        self.assertEqual(result['total'], len(messages))
        self.assertEqual(result['contact']['total'], len(messages))
        self.assertEqual([m['timestamp'] for m in result['messages']], [m['timestamp'] for m in messages])
        with self.assertRaises(ValueError):
            web.preview(username, end, start)
