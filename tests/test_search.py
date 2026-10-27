"""合成档案的搜索、上下文、导出与发布快照语义。"""
from contextlib import closing
import asyncio
import datetime
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
import zipfile
from fastapi.testclient import TestClient

from archive_store import Archive
from web import app as web


class SearchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.archive = Archive(Path(self.temp.name))
        self.contact = {'username': 'group', 'name': '合成群', 'category': 'group'}
        self.day = datetime.date(2026, 10, 17)
        self.start = int(datetime.datetime.combine(self.day, datetime.time()).timestamp())
        self.add_message(1, '报销地址 100%_ABC', 0)
        self.add_message(2, '普通前后文', 10)
        self.add_message(3, '[语音]', 20, 34)
        self.add_message(4, '报销合同', 86400)
        self.add_message(5, '[语音]', 86410, 34)
        self.archive.save_transcript('group', 'server:3', b'a', 'success', '语音报销内容')
        self.archive.save_transcript('group', 'server:5', b'b', 'failed', '报销失败内容')
        self.manager = Mock()
        self.manager.lock = threading.RLock()
        self.manager.archive.return_value = self.archive
        self.patcher = patch.object(web, 'manager', self.manager)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

    def add_message(self, key, content, delta, kind=1):
        stamp = self.start + delta
        message = dict(type=kind, type_name='语音' if kind == 34 else '文本', content=content,
                       timestamp=stamp, time=datetime.datetime.fromtimestamp(stamp).strftime('%Y-%m-%d %H:%M:%S'),
                       sender='甲', sender_id=1, local_id=key, sort_seq=key, source_shard='message_0.db')
        with closing(self.archive.connect()) as c, c:
            c.execute('INSERT INTO messages VALUES (?,?,?,?,?)',
                      ('group', 'server:' + str(key), stamp, json.dumps(message), ''))
            c.execute('INSERT OR REPLACE INTO chats VALUES (?,?)', ('group', json.dumps(self.contact)))

    def test_two_character_literal_transcript_and_daily_counts(self):
        result = web.messages('group', q='报销', limit=1)
        self.assertEqual(result['total'], 3)
        self.assertEqual([d['count'] for d in result['daily']], [2, 1])
        self.assertEqual(result['messages'][0]['message_id'], 'server:4')
        result = web.messages('group', q='报销', day=self.day.isoformat())
        self.assertEqual(result['total'], 2)
        self.assertEqual(result['messages'][1]['search_sources'], ['transcript'])
        self.assertIn('报销', result['messages'][1]['snippet'])
        self.assertEqual(web.messages('group', q='报销', include_transcripts=False)['total'], 2)
        self.assertEqual(web.messages('group', q='%_')['total'], 1)
        self.assertEqual(web.messages('group', q='abc')['total'], 1)
        self.assertEqual(web.messages('group', q='报销', end=self.start+10)['total'], 1)
        self.assertEqual(web.messages('group', q='报销', offset=1, limit=1)['messages'][0]['message_id'], 'server:3')

    def test_context_crosses_search_and_date_limits(self):
        result = web.context('group', 'server:3', before=1, after=1)
        self.assertEqual([m['message_id'] for m in result['messages']], ['server:2', 'server:3', 'server:4'])
        self.assertEqual(result['target_index'], 1)
        self.assertTrue(result['messages'][1]['is_target'])
        with self.assertRaises(ValueError):
            web.context('other', 'server:3')

    def test_only_body_and_successful_voice_text_are_searchable(self):
        with closing(self.archive.connect()) as c, c:
            c.execute("UPDATE messages SET data=json_set(data,'$.sender','报销','$.source_shard','报销.db') WHERE message_key='server:2'")
            c.execute('INSERT INTO transcripts VALUES (?,?,?,?,?,?,?)',
                      ('group', 'server:2', b'a', 'success', '仅转写字段的词语', '', 'null'))
        self.assertEqual(web.messages('group', q='报销')['total'], 3)
        self.assertEqual(web.messages('group', q='仅转写字段')['total'], 0)
        self.assertEqual(web.messages('group', q='失败内容')['total'], 0)

    def test_snapshot_and_refresh_include_committed_changes_together(self):
        with closing(self.archive.connect()) as c:
            c.execute('BEGIN')
            self.assertEqual(self.archive.search('group', q='报销', connection=c)['total'], 3)
            self.add_message(6, '新报销', 30)
            old = self.archive.search('group', q='报销', connection=c)
            self.assertEqual(old['total'], 3)
            self.assertEqual(sum(d['count'] for d in old['daily']), 3)
            self.assertEqual(len(old['messages']), 3)
        fresh = web.messages('group', q='报销')
        self.assertEqual(fresh['total'], 4)
        self.assertEqual(sum(d['count'] for d in fresh['daily']), 4)
        self.assertEqual(len(fresh['messages']), 4)

    def test_export_requeries_and_records_actual_scope(self):
        self.add_message(6, '新报销', 30)
        async def collect(response):
            return b''.join([part async for part in response.body_iterator])
        for fmt in ('json', 'txt', 'zip'):
            req = web.SearchExportReq(account_id='synthetic', usernames=['group'], format=fmt, q='报销', day=self.day.isoformat())
            body = asyncio.run(collect(web.export_search(req)))
            if fmt == 'txt':
                self.assertIn('"count": 3', body.decode())
                self.assertNotIn('报销合同', body.decode())
                continue
            if fmt == 'zip':
                with zipfile.ZipFile(io.BytesIO(body)) as bundle:
                    self.assertEqual(len(bundle.namelist()), 2)
                    body = bundle.read(next(n for n in bundle.namelist() if n.endswith('.json')))
            record = json.loads(body)
            self.assertEqual(record['total'], 3)
            self.assertEqual(record['contact']['export_search']['count'], 3)
            self.assertEqual(record['contact']['export_search']['q'], '报销')
            self.assertTrue(record['contact']['export_search']['generated_at'])
            self.assertNotIn('server:2', [m['message_id'] for m in record['messages']])

    def test_validation(self):
        for values in ({'day': 'bad'}, {'limit': 0}, {'offset': -1}, {'start': 2, 'end': 1}):
            with self.assertRaises(ValueError):
                web.messages('group', **values)
        with self.assertRaises(ValueError):
            web.export_search(web.SearchExportReq(account_id='synthetic', usernames=['a', 'b']))

    def test_http_query_context_and_download_contract(self):
        with TestClient(web.app) as client:
            response = client.get('/api/messages', params={'username': 'group', 'q': '报销',
                                                          'include_transcripts': 'false'})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()['total'], 2)
            self.assertEqual(response.json()['limit'], 50)
            self.assertEqual(client.get('/api/context', params={'username': 'group',
                'message_id': 'server:3'}).json()['target_id'], 'server:3')
            response = client.post('/api/export-search', json={'account_id':'synthetic', 'usernames': ['group'], 'format': 'json',
                                                              'q': '报销', 'day': self.day.isoformat()})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()['total'], 2)
            self.assertIn('attachment', response.headers['content-disposition'])
            self.assertEqual(client.get('/api/messages', params={'username': 'group', 'day': 'bad'}).status_code, 400)


if __name__ == '__main__':
    unittest.main()
