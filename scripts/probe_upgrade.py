"""Restart packaged code across an app replacement, retaining synthetic archive state."""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--package', type=Path, required=True)
parser.add_argument('--output', type=Path, required=True)
parser.add_argument('--worker', choices=('before', 'after'))
args = parser.parse_args()
root = args.package.resolve()
output = args.output.resolve()

if args.worker:
    sys.path.insert(0, str(root / 'app'))
    from maintenance import Manager
    from runtime_paths import data_root
    manager = Manager(data_root())
    if args.worker == 'before':
        manager.select(str(output / 'source'), '合成升级测试', True)
        manager.archive().save_config({'self_username': 'wxid_self', 'my_name': '合成本人',
            'followed_usernames': ['test@chatroom'], 'hidden_usernames': ['wxid_hidden'],
            'session_groups': [{'id':'project','name':'合成项目','usernames':['test@chatroom']}],
            'keys_file': str(output / 'external-keys.json'), 'asr_model': str(output / 'external-model.pt'), 'view_state': {
            'active': 'test@chatroom', 'offset': 0, 'ms': '2023-11-01',
            'me': '2023-11-30', 'scroll': 120}})
        manager.archive().update(output / 'source')
        manager.archive().save_transcript('test@chatroom', 'server:101', b'synthetic media', 'success',
                                         text='合成成功转写\n' + '升级阅读位置合成证据\n' * 40)
        manager.archive().save_transcript('test@chatroom', 'server:102', None, 'failed', reason='合成失败')
        manager.archive().set_voice_skipped([{'username':'test@chatroom','message_id':'server:102'}], True)
    result = {'messages': manager.archive().message_count('test@chatroom'),
              'config': manager.archive().config(), 'account': manager.account(),
              'transcript': manager.archive().transcript('test@chatroom','server:101')[1:4],
              'skipped': sorted(manager.archive().skipped_voices()), 'data': str(data_root())}
    (output / (args.worker + '.json')).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
else:
    output.mkdir(parents=True, exist_ok=True)
    checkout = Path(__file__).resolve().parent.parent
    os.environ['WECHAT_ARCHIVE_DATA_DIR'] = str(output / 'fixture_state')
    sys.path.insert(0, str(checkout))
    sys.path.insert(0, str(checkout / 'tests'))
    from test_maintenance import seed
    seed(output / 'source')
    import sqlite3
    from contextlib import closing
    from test_maintenance import table
    with closing(sqlite3.connect(output / 'source/message/message_0.db')) as c, c:
        c.execute(f'UPDATE "{table("test@chatroom")}" SET local_type=34')
    (output / 'external-keys.json').write_text('{}')
    (output / 'external-model.pt').write_bytes(b'synthetic placeholder; never loaded')
    isolated = output / '中文 空格便携升级'
    isolated.mkdir()
    shutil.copytree(root / 'runtime', isolated / 'runtime', ignore=shutil.ignore_patterns('__pycache__'))
    shutil.copytree(root / 'app', isolated / 'app', ignore=shutil.ignore_patterns('__pycache__'))
    env = dict(os.environ, WECHAT_ARCHIVE_HOME=str(isolated), WECHAT_ARCHIVE_DATA_DIR=str(output / '保留数据 覆盖目录'))
    command = [str(isolated / 'runtime' / 'python.exe'), str(Path(__file__).resolve()),
               '--package', str(isolated), '--output', str(output), '--worker']
    subprocess.run(command + ['before'], check=True, env=env)
    new = output / '新解压 程序目录'
    shutil.copytree(isolated / 'runtime', new / 'runtime', ignore=shutil.ignore_patterns('__pycache__'))
    shutil.copytree(root / 'app', new / 'app', ignore=shutil.ignore_patterns('__pycache__'))
    shutil.copy2(root / '启动微信聊天档案.cmd', new / '启动微信聊天档案.cmd')
    env['WECHAT_ARCHIVE_HOME'] = str(new)
    relocated = [str(new / 'runtime/python.exe'), str(Path(__file__).resolve()), '--package', str(new), '--output', str(output), '--worker', 'after']
    subprocess.run(relocated, check=True, env=env)
    (output / 'relocated.json').write_bytes((output / 'after.json').read_bytes())
    env['WECHAT_ARCHIVE_HOME'] = str(isolated)
    # Replace only program resources, as a user upgrading in place would do.
    shutil.copytree(root / 'app', isolated / 'app', dirs_exist_ok=True, ignore=shutil.ignore_patterns('__pycache__'))
    subprocess.run(command + ['after'], check=True, env=env)
    before = json.loads((output / 'before.json').read_text(encoding='utf-8'))
    after = json.loads((output / 'after.json').read_text(encoding='utf-8'))
    relocated_state = json.loads((output / 'relocated.json').read_text(encoding='utf-8'))
    evidence = {'before': before, 'after': after, 'relocated':relocated_state,
                'preserved': before == after == relocated_state and after['messages'] == 2}
    (output / 'upgrade-probe.json').write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'preserved':evidence['preserved'], 'messages':after['messages'],
                      'account_id':after['account']['id'], 'evidence':str(output/'upgrade-probe.json')}, ensure_ascii=False))
