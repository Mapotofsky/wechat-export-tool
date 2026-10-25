"""第一阶段内容分析的隔离合成材料；可加载源码或指定便携 app。"""
import argparse
from contextlib import closing
import datetime as dt
import json
import os
from pathlib import Path
import sys


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--app', type=Path, default=Path(__file__).resolve().parent.parent)
    parser.add_argument('--output', type=Path, required=True, help='新的隔离目录，不使用已有数据')
    parser.add_argument('--serve', action='store_true')
    parser.add_argument('--desktop', action='store_true')
    parser.add_argument('--port', type=int, default=8767)
    parser.add_argument('--lexical', action='store_true', help='词句筛选、专名、新词与别名验收材料')
    args = parser.parse_args()
    root = args.output.resolve()
    root.mkdir(parents=True, exist_ok=False)
    os.environ['WECHAT_ARCHIVE_DATA_DIR'] = str(root / 'state')
    sys.path.insert(0, str(args.app.resolve()))
    from maintenance import Manager, atomic_json
    from web import app as web
    import archive_analysis
    assert Path(archive_analysis.__file__).resolve().parent == args.app.resolve()
    manager = Manager(root / 'state')
    account = dict(id='synthetic-content', label='合成内容分析验收', directory=str(root/'synthetic-source'), mode='plaintext')
    manager.registry = dict(active=account['id'], accounts=[account])
    atomic_json(manager.registry_path, manager.registry)
    archive = manager.archive()
    start = int(dt.datetime(2026, 10, 19, 10).timestamp())
    with closing(archive.connect()) as c, c:
        for username, name in [('a','合成研发群'),('b','合成协作群')]:
            c.execute('INSERT INTO chats VALUES (?,?)', (username,json.dumps(dict(username=username,name=name,category='group'))))
            for i in range(44):
                previous = i < 28
                j = i if previous else i-28
                messages = ['这版语音识别不支持API批量导出，合同没收到。',
                            'OpenAI和FF14接口检查采样率，保留解码日志。',
                            'API不支持批量导出，合同没收到，需要核对采样率。',
                            '采样率没问题，我再检查解码。',
                            '语音识别请保留原始音频和日志。', '收到']
                text = ('周末运动，今天也去跑步。' if j>=3 else '合同审批已经提交。') if previous else messages[j%6]
                if args.lexical:
                    current_texts=['微信 WeChat 微信，收到，好的，我们需要核对1200元。',
                                  '可以报销，不能报销，已经收到，没收到。',
                                  '支持方案甲，但不支持方案乙。',
                                  '物品被没收过，编号202610。',
                                  '用C++和C#核对GPT-4与v1.2接口。',
                                  '合同 https://example.com/path?id=abc wxid_secret123 报销。',
                                  '微信平台已经更新，WeChat请核对API。',
                                  '收到，好的，我和你我们应该可能继续核对。',
                                  '紧急通知今天大家准时参加会议。',
                                  '今天星澜智研开始测试。',
                                  '请用星澜智研处理样例。',
                                  '新版星澜智研已经就绪。']
                    text=('微信消息需要核对。' if j<5 else 'WeChat收到通知。' if j<8 else '周末运动。') if previous else current_texts[j%len(current_texts)]
                stamp=start+(-86400 if previous else 0)+(j//6)*7200+(j%6)*60
                kind=34 if not previous and j==14 else 1
                m=dict(timestamp=stamp,time=dt.datetime.fromtimestamp(stamp).isoformat(' '),type=kind,
                       type_name='语音' if kind==34 else '文本',content='[语音]' if kind==34 else text,
                       sender='合成甲' if j%2 else '合成乙',sender_username='self' if j%2 else 'peer',
                       sort_seq=i,local_id=i,source_shard='synthetic.db')
                c.execute('INSERT INTO messages VALUES (?,?,?,?,?)',(username,'server:'+str(i),stamp,json.dumps(m),''))
            archive.metadata(c, username, dict(username=username,name=name,category='group'))
    for username in ('a','b'):
        archive.save_transcript(username,'server:42',b'synthetic','success','微信 WeChat 支持方案甲，但不支持方案乙。' if args.lexical else 'API不支持批量导出，合同没收到。')
    archive.save_config(dict(self_username='self',view_state=dict(active='a',ms='2026-10-19',me='2026-10-19')))
    web.manager.executor.shutdown()
    web.manager = manager
    req=web.AnalysisReq(account_id=account['id'],usernames=['a','b'],start=start-36000,end=start+50399,anchor='api')
    result=web.analyze(req)
    (root/'analysis.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(dict(app=str(args.app.resolve()),state=str(root/'state'),total=result['overview']['total'],
                          text_messages=result['content']['text_messages'],changes=len(result['content']['comparison']['items'])),ensure_ascii=False),flush=True)
    if args.desktop:
        import desktop
        return desktop.main(['--smoke-test'])
    if args.serve:
        import uvicorn
        uvicorn.run(web.app,host='127.0.0.1',port=args.port,access_log=False)
    manager.executor.shutdown()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
