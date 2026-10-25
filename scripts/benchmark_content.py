"""固定合成范围的完整分析计时；不访问真实数据。"""
import argparse
from contextlib import closing
import datetime as dt
import importlib.util
import json
from pathlib import Path
import platform
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from archive_store import Archive


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--module', type=Path, default=ROOT/'archive_analysis.py')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--messages', type=int, default=600)
    parser.add_argument('--diverse', action='store_true')
    parser.add_argument('--rounds', type=int, default=3)
    args = parser.parse_args()
    spec = importlib.util.spec_from_file_location('measured_analysis', args.module)
    analysis = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(analysis)
    texts = ['可以报销，微信通知我了，金额1200。', '不能报销，WeChat还没收到。',
             '用C++和C#核对GPT-4与v1.2接口。', '收到，好的，我们需要核对编号202610。',
             '支持方案甲，但不支持方案乙。', '物品被没收过，检查登记。',
             '请看https://example.com/a?id=123 和 wxid_abc123，不要漏掉API。',
             '今天星澜智研开始测试。', '请用星澜智研处理样例。', '新版星澜智研已经就绪。']
    with tempfile.TemporaryDirectory(prefix='content_benchmark_') as tmp:
        archive = Archive(tmp)
        start = int(dt.datetime(2026,10,19,10).timestamp())
        with closing(archive.connect()) as c, c:
            for u in ('a','b'):
                c.execute('INSERT INTO chats VALUES (?,?)',(u,json.dumps(dict(username=u,name='合成'+u,category='group'))))
                for i in range(args.messages//2):
                    half=args.messages//4
                    stamp=start+(i//half)*86400+(i%half)*max(1,80000//half)
                    text=texts[i%len(texts)]
                    if args.diverse: text+='项目编号'+str(100000+i)+'等待核对交付进展。'
                    m=dict(timestamp=stamp,time=dt.datetime.fromtimestamp(stamp).isoformat(' '),
                           type=1,type_name='文本',content=text,sender='合成',sender_username='peer',local_id=i,sort_seq=i)
                    c.execute('INSERT INTO messages VALUES (?,?,?,?,?)',(u,str(i),stamp,json.dumps(m),''))
        req=dict(usernames=['a','b'],start=start+86400,end=start+172799,anchor='支持')
        timings=[]
        for _ in range(args.rounds):
            t=time.perf_counter()
            result=analysis.analyze(archive,req)
            timings.append(round(time.perf_counter()-t,4))
        content=result['content']
        record=dict(python=sys.version,platform=platform.platform(),module=str(args.module),
                    messages=args.messages,diverse=args.diverse,current=result['overview']['total'],previous=result['previous']['total'],
                    seconds=timings,words=len(content['words']),phrases=len(content['phrases']),
                    discovered=len(content.get('discovered',[])),candidate_total=content.get('candidate_total'),
                    samples=dict(words=[t['term'] for t in content['words'][:20]],phrases=[t['term'] for t in content['phrases'][:20]]))
        args.output.parent.mkdir(parents=True,exist_ok=True)
        args.output.write_text(json.dumps(record,ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps(record,ensure_ascii=False))


if __name__ == '__main__':
    main()
