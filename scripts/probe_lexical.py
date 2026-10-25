"""用指定应用和随包解释器核验词句算法；同时保存新词筛选消融样例。"""
import argparse
from contextlib import closing
import datetime as dt
import json
import os
from pathlib import Path
import sys
import tempfile


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--app',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
    args=p.parse_args();sys.path.insert(0,str(args.app.resolve()))
    with tempfile.TemporaryDirectory(prefix='lexical_probe_') as tmp:
        os.environ['WECHAT_ARCHIVE_DATA_DIR']=str(Path(tmp)/'initial')
        from archive_store import Archive
        import archive_analysis as a
        import analysis_lexicon as lex
        from web.app import AnalysisReq
        assert Path(a.__file__).resolve().parent==args.app.resolve()
        archive=Archive(Path(tmp)/'account')
        texts=['微信 WeChat 微信，C++与C#核对GPT-4、v1.2。',
               '微信1200元，收到，好的。', 'WeChat支持方案甲，但不支持方案乙。',
               '物品被没收过，已经收到，没收到，可以报销，不能报销。',
               '合同 https://example.com/path?id=abc wxid_secret123 报销',
               '今天星澜智研开始测试','请用星澜智研处理样例','新版星澜智研已经就绪',
               '介绍星澜智研相关接口','关于星澜智研的报告','欢迎星澜智研团队']
        texts+=['紧急通知今天大家准时参加会议']*12+['甲乙丙丁','戊己庚辛','壬癸甲乙']
        texts+=['请准时参加例行会议甲','请准时参加例行会议乙','请准时参加例行会议丙']
        start=int(dt.datetime(2026,10,19,10).timestamp())
        with closing(archive.connect()) as c,c:
            c.execute('INSERT INTO chats VALUES (?,?)',('a',json.dumps(dict(username='a',name='合成',category='group'))))
            for i,text in enumerate(texts):
                m=dict(timestamp=start+i*60,time=dt.datetime.fromtimestamp(start+i*60).isoformat(' '),type=1,
                       content=text,sender='合成',sender_username='peer',type_name='文本',sort_seq=i,local_id=i)
                c.execute('INSERT INTO messages VALUES (?,?,?,?,?)',('a',str(i),m['timestamp'],json.dumps(m),''))
        archive.save_config(dict(analysis_aliases=[dict(id='platform',name='平台组',members=['微信','WeChat'],enabled=True)]))
        req=AnalysisReq(account_id='a',usernames=['a'])
        result=a.analyze(archive,req)
        words={x['term'] for x in result['content']['words']}
        assert {'c++','c#','gpt-4','v1.2','1200','收到'}<=words
        assert not {'example','com','abc','secret123','https'} & words
        group=result['content']['groups'][0]
        assert (group['count'],group['occurrences'])==(3,5)
        evidence=a.analysis_messages(archive,req.model_copy(update={'evidence_filters':type(req.filters)(terms=['@alias/platform'],term_mode='expression')}))
        assert evidence['total']==group['count']
        for m in evidence['messages']:
            assert all(m['content'][x:y] in ('微信','WeChat') for x,y in m['highlights'])
        assert '星澜智研' in {t['term'] for t in result['content']['discovered']}
        blocks=[lex.scan(t) for t in texts]
        study={}
        for name,cohesion,boundary in [('support_only',-100,0),('with_cohesion',1.5,0),('boundary_08',1.5,0.8),('boundary_10',1.5,1.0)]:
            terms=lex.discover(texts,blocks,[],cohesion,boundary)
            study[name]=dict(count=len(terms),terms=terms)
        output=dict(passed=True,python=sys.executable,app=str(args.app.resolve()),
                    candidate_total=result['content']['candidate_total'],group=group,discovery_study=study,
                    content=result['content'])
        args.output.parent.mkdir(parents=True,exist_ok=True)
        args.output.write_text(json.dumps(output,ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps(dict(passed=True,group_count=group['count'],discovery_counts={k:v['count'] for k,v in study.items()})))


if __name__=='__main__':main()
