"""Isolated native recovery and packaged CMD acceptance; synthetic files only."""
import argparse
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--package', type=Path, required=True)
parser.add_argument('--output', type=Path, required=True)
parser.add_argument('--mode', choices=('seed','launch','smoke','state','duplicate','data-error','log-error','runtime-error','headless','stop-headless','tools'), required=True)
parser.add_argument('--data',type=Path)
parser.add_argument('--worker', action='store_true')
args = parser.parse_args()
package, output = args.package.resolve(), args.output.resolve()
output.mkdir(parents=True, exist_ok=True)
state = args.data.resolve() if args.data else output / '隔离 数据目录'
os.environ['WECHAT_ARCHIVE_DATA_DIR'] = str(state)
os.environ['WECHAT_ARCHIVE_HOME'] = str(package)
sys.path.insert(0, str(package / 'app'))

if args.worker:
    import desktop
    if args.mode == 'headless':
        lock = desktop.InstanceLock(state / 'desktop.lock')
        assert lock.acquire()
        (output / 'headless.json').write_text(json.dumps({'pid':os.getpid(), 'data':str(state), 'window':False}), encoding='utf-8')
        while not (output / 'stop-headless').exists():
            time.sleep(.2)
        lock.close()
    else:
        if args.mode == 'runtime-error':
            def missing():
                raise RuntimeError('合成缺运行库：未检测到 Microsoft Edge WebView2 Runtime；请安装 prerequisites 中的离线安装程序后重试。')
            desktop.check_webview2 = missing
        raise SystemExit(desktop.main([]))
elif args.mode == 'seed':
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tests'))
    from test_maintenance import seed, table
    from maintenance import Manager
    manager = Manager(state)
    for name in ('A', 'B'):
        source = output / ('合成 源目录 ' + name)
        if not source.exists():
            seed(source)
            with closing(sqlite3.connect(source / 'message/message_0.db')) as c, c:
                c.execute(f'UPDATE "{table("test@chatroom")}" SET local_type=34')
        account = manager.select(str(source), '合成账号 ' + name, True)
        manager.archive().save_config({'self_username':'wxid_self', 'view_state':{'active':'test@chatroom','offset':0,'scroll':80,'ms':'','me':''}})
        manager.archive().update(source)
        manager.archive().save_transcript('test@chatroom','server:101',b'synthetic','success',text='合成成功转写 '+name)
        manager.archive().save_transcript('test@chatroom','server:102',None,'failed',reason='合成失败')
        manager.archive().set_voice_skipped([{'username':'test@chatroom','message_id':'server:102'}], True)
    manager.executor.shutdown()
elif args.mode == 'stop-headless':
    (output / 'stop-headless').touch()
elif args.mode == 'state':
    from maintenance import Manager
    manager = Manager(state)
    result = {'account_id':manager.account()['id'], 'view_state':manager.archive().config().get('view_state'),
              'messages':manager.archive().message_count('test@chatroom')}
    manager.executor.shutdown()
    (output / 'desktop-state.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(result,ensure_ascii=False))
elif args.mode == 'tools':
    import voice_service
    result = {'local':voice_service.probe(), 'online':voice_service.probe(online=True)}
    (output / 'tools.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(result,ensure_ascii=False))
elif args.mode in ('launch','duplicate','smoke'):
    os.startfile(str(package / '启动微信聊天档案.cmd'),arguments='--smoke-test' if args.mode=='smoke' else '')
else:
    if args.mode == 'data-error':
        target = output / '不可写 数据路径'
        target.write_text('synthetic file blocks directory creation')
        os.environ['WECHAT_ARCHIVE_DATA_DIR'] = str(target)
    if args.mode == 'log-error':
        target = output / '日志受阻 数据'
        target.mkdir(exist_ok=True)
        (target / 'logs').write_text('synthetic file blocks log directory creation')
        os.environ['WECHAT_ARCHIVE_DATA_DIR'] = str(target)
    command = [str(package / 'runtime/pythonw.exe'),str(Path(__file__).resolve()),'--package',str(package),'--output',str(output),'--mode',args.mode,'--worker']
    # Worker inherits the exact fault target instead of resetting it.
    if args.mode == 'data-error':
        command = [str(package / 'runtime/pythonw.exe'),str(package / 'app/desktop.py')]
    if args.mode == 'log-error':
        command = [str(package / 'runtime/pythonw.exe'),str(package / 'app/desktop.py')]
    process = subprocess.Popen(command, env=dict(os.environ), creationflags=subprocess.CREATE_NO_WINDOW)
    (output / ('process-' + args.mode + '.json')).write_text(json.dumps({'pid':process.pid,'mode':args.mode}), encoding='utf-8')
