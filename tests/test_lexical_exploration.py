"""可控筛选、位置证据、新词和用户别名的隔离合成验证。"""
from contextlib import closing
import datetime as dt
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

# 在导入服务前隔离注册表；不读取真实账号设置。
_initial = tempfile.TemporaryDirectory(prefix='lexical_initial_')
os.environ['WECHAT_ARCHIVE_DATA_DIR'] = _initial.name
from fastapi.testclient import TestClient
from archive_store import Archive
import archive_analysis as analysis
import analysis_lexicon as lex
from web import app as web


class LexicalTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.archive=Archive(Path(self.tmp.name)/'a')
        self.archive.save_config({'analysis_blocklist':[], 'analysis_options':{**lex.DEFAULT_OPTIONS,'deduplicate':False,'complete_phrases':False}})
        self.start=int(dt.datetime(2026,10,19,10).timestamp())
        self.manager=Mock(); self.manager.lock=threading.RLock()
        self.manager.account.return_value={'id':'a'}; self.manager.archive.return_value=self.archive
        p=patch.object(web,'manager',self.manager); p.start();self.addCleanup(p.stop)
        with closing(self.archive.connect()) as c,c:
            for u in ('a','b'): c.execute('INSERT INTO chats VALUES (?,?)',(u,json.dumps(dict(username=u,name='合成'+u,category='group'))))

    def add(self,text,username='a',previous=False,kind=1,status=None):
        with closing(self.archive.connect()) as c,c:
            i=c.execute('SELECT count(*) FROM messages').fetchone()[0]
            stamp=self.start+(-86400 if previous else 0)+i*60
            m=dict(content=text,type=kind,timestamp=stamp,time=dt.datetime.fromtimestamp(stamp).isoformat(' '),
                   type_name='语音' if kind==34 else '文本',sender='合成人',sender_username='peer',local_id=i,sort_seq=i)
            c.execute('INSERT INTO messages VALUES (?,?,?,?,?)',(username,str(i),stamp,json.dumps(m),''))
        if status: self.archive.save_transcript(username,str(i),str(i).encode(),status,text)
        return str(i)

    def req(self,**kwargs):
        return web.AnalysisReq(account_id='a',usernames=['a','b'],**kwargs)

    def result(self,**kwargs): return web.analyze(self.req(**kwargs))
    def hits(self,term,**kwargs):
        return web.analyze_messages(self.req(evidence_filters={'terms':[term],'term_mode':'expression'},**kwargs))
    def terms(self,result,category='words'):
        return {x['term']:x for x in result['content'][category]}
    def options(self,**kwargs):
        opts=lex.options_for(self.archive.config());opts.update(kwargs)
        self.archive.save_config({'analysis_options':opts})
    def aliases(self,groups):
        return web.save_analysis_settings(web.AnalysisSettingsReq(account_id='a',options=lex.options_for(self.archive.config()),aliases=groups))

    def test_controls_restore_interaction_roles_numbers_and_explicit_rules(self):
        for _ in range(2): self.add('收到，好的，我和你我们需要应该可能，可以报销，不能报销，金额1200，编号202610。')
        raw=self.result();words=self.terms(raw)
        for term in ('收到','我','你','我们','需要','应该','可能','1200','202610'):
            self.assertIn(term,words);self.assertEqual(words[term]['count'],self.hits(term)['total'])
        phrases=self.terms(raw,'phrases')
        for term in ('好的','可以报销','不能报销'):self.assertIn(term,phrases)
        self.options(complete_phrases=True)
        self.assertNotIn('好的',self.terms(self.result(),'phrases'))
        self.options(complete_phrases=False)
        self.assertIn('好的',self.terms(self.result(),'phrases'))
        self.options(content_only=True,hide_numbers=True)
        filtered=self.terms(self.result());self.assertNotIn('我',filtered);self.assertNotIn('1200',filtered)
        self.options(hide_numbers=False)
        self.assertIn('1200',self.terms(self.result())) # 数字开关不被词性展示再次拦截
        self.archive.save_config({'analysis_blocklist':[dict(text='收到',mode='exact')]})
        self.options(content_only=False,hide_numbers=False)
        self.assertNotIn('收到',self.terms(self.result()))
        self.archive.save_config({'analysis_blocklist':[]})
        self.assertIn('收到',self.terms(self.result()));self.assertIn('1200',self.terms(self.result()))
        self.assertEqual(self.result()['overview']['total'],2)

    def test_affirmative_negative_and_original_tokenization(self):
        for _ in range(2):self.add('可以报销，不能报销，已经收到，没收到，物品被没收过。')
        self.options(content_only=True)
        terms=self.terms(self.result(),'phrases')
        self.assertTrue({'可以报销','不能报销','已经收到','没收到'}<=terms.keys(),terms.keys())
        words=[t[0] for b in lex.scan('物品被没收过') for t in b]
        self.assertIn('没收',words)
        self.assertEqual(''.join(words),'物品被没收过')

    def test_names_noise_spans_and_boundaries(self):
        text='C++ C# GPT-4 v1.2 API FF14 金额1200 -1200 ￥1,200.50 合同 https://example.com/path?key=abc wxid_secret123 报销'
        for _ in range(3):self.add(text)
        r=self.result();words=self.terms(r)
        self.assertTrue({'c++','c#','gpt-4','v1.2','api','ff14','1200','-1200','￥1,200.50'}<=words.keys(),words.keys())
        for t in ('example','com','path','key','abc','secret123','wxid_secret123','https'):
            self.assertNotIn(t,words)
        self.assertNotIn('合同报销',self.terms(r,'phrases'))
        self.assertEqual(self.hits('example')['total'],0)
        self.assertEqual(self.archive.search('a',q='example')['total'],3)
        self.options(ignore_structured=False)
        self.assertIn('example.com',self.terms(self.result()))
        self.assertEqual(self.hits('example.com')['total'],3)
        self.assertEqual(lex.literal_spans('GPT-4 v1.2 C++','GPT'),[])
        self.assertEqual(lex.literal_spans('GPT-4 v1.2 C++','v1'),[])
        self.assertEqual(lex.literal_spans('GPT-4 v1.2 C++','C'),[])

    def test_representative_does_not_depend_on_any_candidates(self):
        for text in ('收到','1200','🙂'):self.add(text)
        self.archive.save_config({'analysis_blocklist':[dict(text=t,mode='contains') for t in ('收到','1200')]})
        r=web.analyze_messages(self.req());self.assertEqual(r['total'],3)
        self.assertEqual({m['content'] for m in r['samples']},{'收到','1200','🙂'})
        self.assertEqual(r['evidence_state'],'selected')
        self.assertEqual(self.hits('不存在')['evidence_state'],'no_matches')
        self.add('[图片]',username='b',kind=3)
        no_text=web.analyze_messages(self.req(filters={'username':'b'}))
        self.assertEqual(no_text['evidence_state'],'no_text')

    def test_overlap_and_independent_associations_reconcile(self):
        for _ in range(2): self.add('支持方案甲，但不支持方案乙。')
        self.add('不支持方案乙。')
        result=self.result(anchor='支持')
        pair=next(x for x in result['content']['associations'] if x['term']=='不支持')
        self.assertEqual((pair['count'],pair['anchor_count'],pair['term_count']),(2,3,3))
        hits=web.analyze_messages(self.req(evidence_filters={'terms':pair['terms'],'term_mode':'expression','independent_terms':True}))
        self.assertEqual(hits['total'],2)

    def test_nested_requires_same_sets_and_one_to_one_positions(self):
        def item(t,n=2):return dict(term=t,count=n,occurrences=n,days=1)
        items=[item('蓝鲸方案'),item('蓝鲸方案讨论')]
        same={'蓝鲸方案':{0:[(0,4)],1:[(0,4)]},'蓝鲸方案讨论':{0:[(0,6)],1:[(0,6)]}}
        merged=lex.nested_groups(items,same)
        self.assertEqual(len(merged),1);self.assertEqual(len(merged[0]['nested']),1)
        independent={'蓝鲸方案':{0:[(0,4),(10,14)],1:[(0,4),(10,14)]},'蓝鲸方案讨论':{0:[(10,16)],1:[(10,16)]}}
        self.assertEqual(len(lex.nested_groups(items,independent)),2)
        changed={**same,'蓝鲸方案':{**same['蓝鲸方案'],2:[(0,4)]}}
        self.assertEqual(len(lex.nested_groups([item('蓝鲸方案',3),items[1]],changed)),2)
        neg={'支持':{0:[(1,3)],1:[(1,3)]},'不支持':{0:[(0,3)],1:[(0,3)]}}
        self.assertEqual(len(lex.nested_groups([item('支持'),item('不支持')],neg)),2)

    def test_discovery_unknown_name_templates_random_and_evidence(self):
        for text in ['今天星澜智研开始测试','请用星澜智研处理样例','新版星澜智研已经就绪','介绍星澜智研相关接口',
                     '关于星澜智研的报告','欢迎星澜智研团队']:
            self.add(text)
        for _ in range(20):self.add('紧急通知今天大家准时参加会议')
        for text in ('甲乙丙丁','戊己庚辛','壬癸甲乙'):self.add(text)
        for text in ('请准时参加例行会议甲','请准时参加例行会议乙','请准时参加例行会议丙'):self.add(text)
        result=self.result();terms=self.terms(result,'discovered')
        self.assertIn('星澜智研',terms,terms)
        self.assertNotIn('紧急通知今天',terms)
        self.assertNotIn('甲乙丙丁',terms)
        self.assertNotIn('准时参加',terms)
        for term,item in terms.items():
            self.assertEqual(item['count'],self.hits(term)['total'])
            self.assertGreaterEqual(item['distinct_text_support'],3)
        self.options(discover=False);self.assertEqual(self.result()['content']['discovered'],[])

    def test_alias_union_occurrences_members_comparison_transcripts_and_settings(self):
        group=dict(id='wechat',name='微信平台',members=['微信','WeChat'],enabled=True)
        self.aliases([group])
        for text in ['微信 WeChat 微信','微信','WeChat','微信平台','其他','其他']:self.add(text)
        for text in ['WeChat','微信','其他','其他','其他','其他']:self.add(text,previous=True)
        self.add('微信',username='b',kind=34,status='success')
        self.add('微信',username='b',kind=34,status='failed')
        r=self.result(start=self.start-36000,end=self.start+50399,anchor='@alias/wechat')
        item=r['content']['groups'][0]
        self.assertEqual((item['count'],item['occurrences']),(5,7))
        self.assertEqual(sum(m['count'] for m in item['members']),6)
        self.assertEqual(self.hits('@alias/wechat',start=self.start-36000,end=self.start+50399)['total'],5)
        self.assertEqual(self.hits('@alias/wechat',include_transcripts=False,start=self.start-36000,end=self.start+50399)['total'],4)
        change=next(x for x in r['content']['comparison']['items'] if x['term']=='@alias/wechat')
        self.assertEqual((change['current'],change['previous'],change['current_total'],change['previous_total']),(5,2,7,6))
        self.assertEqual(self.hits('@alias/wechat',comparison={},start=self.start-86400-36000,end=self.start-36001)['total'],2)
        self.assertEqual(set(self.hits('@alias/wechat')['highlight_terms']),{'微信','WeChat'})
        overlap=dict(id='overlap',name='重叠核对',members=['微信','微信平台'],enabled=True)
        self.aliases([overlap]);over=self.result()['content']['groups'][0]
        self.assertEqual(over['occurrences'],6) # 同处“微信平台”只算一次
        self.archive.save_config({'analysis_blocklist':[dict(text='微信',mode='exact')]})
        remaining=self.result()['content']['groups'][0]
        self.assertEqual(remaining['count'],1);self.assertTrue(remaining['members'][0]['excluded'])
        self.aliases([{**overlap,'enabled':False}]);self.assertEqual(self.result()['content']['groups'],[])
        self.aliases([]);self.assertEqual(self.archive.config()['analysis_aliases'],[])

    def test_settings_validation_and_account_isolation(self):
        with TestClient(web.app) as client:
            body=dict(account_id='a',options=lex.DEFAULT_OPTIONS,aliases=[dict(id='x',name='平台',members=['微信','WeChat'])])
            self.assertEqual(client.post('/api/analysis/settings',json=body).status_code,200)
            conflict={**body,'aliases':body['aliases']+[dict(id='y',name='另一组',members=['wechat'])]}
            self.assertEqual(client.post('/api/analysis/settings',json=conflict).status_code,400)
            repeated={**body,'aliases':[dict(id='x',name='平台',members=['WeChat','wechat'])]}
            self.assertEqual(client.post('/api/analysis/settings',json=repeated).status_code,400)
            other=Archive(Path(self.tmp.name)/'b');self.manager.account.return_value={'id':'b'};self.manager.archive.return_value=other
            self.assertEqual(client.post('/api/analysis/settings',json=body).status_code,400)
            config=client.get('/api/analysis/settings',params={'account_id':'b'}).json()
            self.assertEqual(config['aliases'],[]);self.assertEqual(config['rules'],[])
            self.assertEqual(config['options'],lex.DEFAULT_OPTIONS)


if __name__=='__main__':unittest.main()
