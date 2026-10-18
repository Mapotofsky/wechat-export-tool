"""合成档案验证完整范围聚合、联动回查、分段和账号级黑名单。"""
from contextlib import closing
import datetime as dt
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

from archive_store import Archive
import archive_analysis as analysis
from web import app as web


class AnalysisTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.archive = Archive(Path(self.temp.name) / 'account-a')
        self.archive.save_config({'self_username': 'self', 'analysis_blocklist': []})
        self.start = int(dt.datetime(2026, 10, 12).timestamp())
        for u in ('a', 'b'):
            with closing(self.archive.connect()) as c, c:
                c.execute('INSERT INTO chats VALUES (?,?)',
                          (u, json.dumps(dict(username=u, name='合成'+u, category='group'))))
        for i in range(65):
            self.add('a', i, i*60, '合同 报销 合同', sender='self' if i%2 else 'peer')
        self.add('a', 100, 86400, '[语音]', kind=34)
        self.archive.save_transcript('a', 'server:100', b'synthetic', 'success', '语音 合同')
        self.add('a', 101, 86460, '[语音]', kind=34)
        self.archive.save_transcript('a', 'server:101', b'synthetic2', 'failed', '合同失败结果')
        self.add('b', 1, 0, '通知 通知书 通知 合同')
        self.add('b', 2, 1200, '中间消息')
        self.add('b', 3, 2400, '通知 合同')
        self.manager = Mock()
        self.manager.lock = threading.RLock()
        self.manager.account.return_value = {'id': 'a'}
        self.manager.archive.return_value = self.archive
        self.patcher = patch.object(web, 'manager', self.manager)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

    def add(self, username, key, delta, content, kind=1, sender='peer'):
        stamp = self.start + delta
        m = dict(timestamp=stamp, time=dt.datetime.fromtimestamp(stamp).isoformat(' '),
                 type=kind, type_name='语音' if kind==34 else '文本', content=content,
                 sender='同名', sender_username=sender, sender_id=1,
                 sort_seq=key, local_id=key, source_shard='synthetic.db')
        with closing(self.archive.connect()) as c, c:
            c.execute('INSERT INTO messages VALUES (?,?,?,?,?)',
                      (username, 'server:'+str(key), stamp, json.dumps(m), ''))

    def req(self, **changes):
        return web.AnalysisReq(account_id='a', usernames=['a','b'], **changes)

    def test_complete_scope_counts_and_descending_pages(self):
        req = self.req()
        result = web.analyze(req)
        self.assertEqual(result['overview']['total'], 70)
        self.assertEqual(sum(r['count'] for r in result['types']), 70)
        self.assertEqual(sum(r['count'] for r in result['heat']), 70)
        self.assertEqual(sum(r['count'] for r in result['senders']), 70)
        self.assertEqual(len(result['senders']), 2)  # 同名的两个稳定身份不合并
        self.assertEqual(result['self_count'], 32)
        self.assertEqual(result['daily'][0]['day'], '2026-10-13')
        first = web.analyze_messages(req)
        second = web.analyze_messages(self.req(offset=50))
        self.assertEqual((len(first['messages']),len(second['messages'])), (50,20))
        messages = first['messages'] + second['messages']
        keys = [(m['timestamp'],m['sort_seq'],m['local_id'],m['message_id'],m['username']) for m in messages]
        self.assertEqual(keys, sorted(keys, reverse=True))
        self.assertEqual(len({(m['username'],m['message_id']) for m in messages}), 70)

    def test_facets_reconcile_with_original_messages(self):
        result = web.analyze(self.req())
        for name, field in (('senders','sender_key'),('types','kind'),('voices','voice_status')):
            for item in result[name]:
                with self.subTest(name=name,key=item['key']):
                    req = self.req(filters={field:item['key']})
                    self.assertEqual(web.analyze(req)['overview']['total'], item['count'])
                    self.assertEqual(web.analyze_messages(req)['total'], item['count'])
        for day in result['daily']:
            date = dt.date.fromisoformat(day['day'])
            a = int(dt.datetime.combine(date,dt.time()).timestamp())
            req = self.req(filters={'start':a,'end':a+86399})
            response = web.analyze(req)
            self.assertEqual(response['overview']['total'], day['count'])
            self.assertEqual(response['overview']['calendar_days'], 1)
            self.assertEqual(web.analyze_messages(req)['total'], day['count'])
        for word in result['content']['words']:
            req = self.req(filters={'terms':[word['term']],'term_mode':'word'})
            self.assertEqual(web.analyze_messages(req)['total'], word['count'])
        for phrase in result['content']['phrases']:
            req = self.req(filters={'terms':[phrase['term']],'term_mode':'phrase'})
            self.assertEqual(web.analyze_messages(req)['total'], phrase['count'])
        for pair in result['content']['cooccurrences']:
            req = self.req(filters={'terms':pair['terms'],'term_mode':'word'})
            self.assertEqual(web.analyze_messages(req)['total'], pair['count'])

    def test_system_notices_are_separate_from_people_in_existing_archives(self):
        # 模拟旧档案将系统通知误记到真实身份或未知编号的情况。
        self.add('a', 200, 90000, '你撤回了一条消息', kind=10000, sender='self')
        self.add('b', 201, 90060, '“成员甲”撤回了一条消息', kind=10000, sender=None)
        result = web.analyze(self.req())
        self.assertEqual(result['overview']['total'], 72)
        self.assertEqual(result['overview']['participants'], 2)
        self.assertEqual((result['self_count'], result['other_count']), (32, 38))
        self.assertIn(dict(key='system', name='系统消息', count=2), result['senders'])
        self.assertEqual(sum(s['count'] for s in result['senders']), 72)
        req = self.req(filters={'sender_key': 'system'})
        systems = web.analyze(req)
        self.assertEqual(systems['overview']['total'], 2)
        self.assertEqual(systems['overview']['participants'], 0)
        self.assertTrue(all(d['participants'] == 0 for d in systems['daily']))
        self.assertTrue(all(s['participants'] == 0 for s in systems['sessions']))
        for m in web.analyze_messages(req)['messages']:
            self.assertEqual(m['sender'], '系统消息')
            self.assertIsNone(m['sender_username'])
        for messages in (self.archive.messages('a'), self.archive.search('a', q='撤回')['messages'],
                         self.archive.context('a', 'server:200')['messages']):
            m = next(m for m in messages if m['message_id'] == 'server:200')
            self.assertEqual(m['sender'], '系统消息')
            self.assertIsNone(m['sender_username'])
        with closing(self.archive.connect()) as c, c:
            self.archive.metadata(c, 'b', dict(username='b', name='合成b', category='group'))
        contact = next(x for x in self.archive.contacts() if x['username'] == 'b')
        self.assertEqual(contact['recorded_sender_count'], 1)
        self.assertEqual(contact['recorded_senders'][0]['username'], 'peer')

    def test_query_semantics_match_existing_search_and_transcripts(self):
        for include in (True,False):
            req = self.req(q='合同',include_transcripts=include)
            result = web.analyze(req)
            expected = sum(self.archive.search(u,q='合同',include_transcripts=include)['total'] for u in ['a','b'])
            self.assertEqual(result['overview']['total'], expected)
            self.assertEqual(web.analyze_messages(req)['total'],expected)
            self.assertEqual(result['overview']['total'],68 if include else 67)
        self.assertEqual(web.analyze(self.req(q='失败结果'))['overview']['total'],0)
        result = web.analyze(self.req(keywords=['合同','通知']))
        self.assertEqual([k['count'] for k in result['keywords']],[68,2])

    def test_sessions_use_full_timeline_then_filter_and_manual_gap(self):
        req = web.AnalysisReq(account_id='a',usernames=['b'],q='通知',gap_minutes=30)
        sessions = web.analyze(req)['sessions']
        self.assertEqual(len(sessions),1)
        self.assertEqual((sessions[0]['total'],sessions[0]['count']),(3,2))
        self.assertEqual(web.analyze(req.model_copy(update={'gap_minutes':17}))['session_total'],2)
        self.assertEqual(web.analyze(req.model_copy(update={'gap_minutes':40}))['session_total'],1)
        with self.assertRaises(ValueError):
            analysis.analyze(self.archive,dict(usernames=['b'],gap_minutes=0))

    def test_blocklist_exact_contains_persistence_and_account_isolation(self):
        baseline = web.analyze(self.req())
        self.assertIn('通知书',[w['term'] for w in baseline['content']['words']])
        saved = web.save_analysis_blocklist(web.BlocklistReq(account_id='a',rules=[
            dict(text=' 通知 ',mode='exact'),dict(text='通知',mode='exact')]))
        self.assertEqual(saved['rules'],[dict(text='通知',mode='exact')])
        result = web.analyze(self.req())
        words = {w['term'] for w in result['content']['words']}
        self.assertNotIn('通知',words)
        self.assertIn('通知书',words)
        self.assertEqual(result['overview'],baseline['overview'])
        self.assertEqual(Archive(self.archive.directory).config()['analysis_blocklist'],saved['rules'])
        web.save_analysis_blocklist(web.BlocklistReq(account_id='a',rules=[dict(text='通知',mode='contains')]))
        words = {w['term'] for w in web.analyze(self.req())['content']['words']}
        self.assertNotIn('通知书',words)
        # 普通搜索及原始消息不受黑名单影响。
        self.assertEqual(self.archive.search('b',q='通知')['total'],2)
        other = Archive(Path(self.temp.name)/'account-b')
        self.assertEqual(analysis.rules_for(other),analysis.DEFAULT_BLOCKLIST)
        self.manager.account.return_value = {'id':'b'}
        self.manager.archive.return_value = other
        with self.assertRaises(ValueError):
            web.save_analysis_blocklist(web.BlocklistReq(account_id='a',rules=[]))
        self.assertNotIn('analysis_blocklist',other.config())

    def test_removing_single_character_rule_restores_word_and_never_joins_phrases(self):
        self.add('b',4,3000,'合同 的 报销')
        self.add('b',5,3060,'合同 的 报销')
        req = self.req(filters={'username':'b','start':self.start+3000,'end':self.start+3060})
        self.archive.save_config({'analysis_blocklist':[dict(text='的',mode='exact')]})
        result = web.analyze(req)
        self.assertNotIn('的',[w['term'] for w in result['content']['words']])
        self.assertNotIn('合同报销',[w['term'] for w in result['content']['phrases']])
        self.archive.save_config({'analysis_blocklist':[]})
        result = web.analyze(req)
        self.assertIn('的',[w['term'] for w in result['content']['words']])

    def test_comparison_windows_and_empty_results(self):
        a,b = self.start,self.start+86399
        result = web.analyze(self.req(start=a,end=b))
        self.assertEqual(result['overview']['calendar_days'],1)
        self.assertEqual(result['previous']['total'],0)
        self.assertEqual(result['previous']['calendar_days'],1)
        result = web.analyze(self.req(start=a,end=b,comparison={'start':a,'end':b+86400}))
        self.assertEqual(result['previous']['total'],70)
        self.assertEqual(result['previous']['calendar_days'],2)
        self.assertEqual(result['previous']['daily_average'],35)
        empty = web.analyze(self.req(q='不存在的合成词'))
        self.assertEqual(empty['overview']['total'],0)
        self.assertEqual(empty['sessions'],[])

    def test_http_validation_and_blocklist_management(self):
        with TestClient(web.app) as client:
            body = self.req().model_dump()
            r = client.post('/api/analysis',json=body)
            self.assertEqual(r.status_code,200,r.text)
            self.assertEqual(r.json()['overview']['total'],70)
            self.assertEqual(client.post('/api/analysis',json={**body,'gap_minutes':0}).status_code,422)
            self.assertEqual(client.post('/api/analysis',json={**body,'filters':{'hour':24}}).status_code,422)
            self.assertEqual(client.post('/api/analysis',json={**body,'usernames':['pending']}).status_code,400)
            self.assertEqual(client.post('/api/analysis',json={**body,'account_id':'other'}).status_code,400)
            self.assertEqual(client.post('/api/analysis/blocklist',json={'account_id':'a','rules':[{'text':'','mode':'exact'}]}).status_code,400)
            r=client.post('/api/analysis/blocklist',json={'account_id':'a','rules':[{'text':'合同','mode':'contains'}]})
            self.assertEqual(r.status_code,200)
            self.assertEqual(client.get('/api/analysis/blocklist',params={'account_id':'a'}).json()['rules'],[dict(text='合同',mode='contains')])


if __name__ == '__main__':
    unittest.main()
