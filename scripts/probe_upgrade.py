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
    manager = Manager(root / 'local_data')
    if args.worker == 'before':
        manager.select(str(output / 'source'), '合成升级测试', True)
        manager.archive().save_config({'self_username': 'wxid_self', 'view_state': {
            'active': 'test@chatroom', 'offset': 0, 'ms': '2023-11-01',
            'me': '2023-11-30', 'scroll': 120}})
        manager.archive().update(output / 'source')
    result = {'messages': manager.archive().message_count('test@chatroom'),
              'view_state': manager.archive().config()['view_state'], 'account': manager.account()['id']}
    (output / (args.worker + '.json')).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
else:
    output.mkdir(parents=True, exist_ok=True)
    checkout = Path(__file__).resolve().parent.parent
    os.environ['WECHAT_ARCHIVE_DATA_DIR'] = str(output / 'fixture_state')
    sys.path.insert(0, str(checkout))
    sys.path.insert(0, str(checkout / 'tests'))
    from test_maintenance import seed
    seed(output / 'source')
    isolated = output / '中文 空格便携升级'
    isolated.mkdir()
    shutil.copytree(root / 'runtime', isolated / 'runtime', ignore=shutil.ignore_patterns('__pycache__'))
    shutil.copytree(root / 'app', isolated / 'app', ignore=shutil.ignore_patterns('__pycache__'))
    env = dict(os.environ, WECHAT_ARCHIVE_HOME=str(isolated), WECHAT_ARCHIVE_DATA_DIR=str(isolated / 'local_data'))
    command = [str(isolated / 'runtime' / 'python.exe'), str(Path(__file__).resolve()),
               '--package', str(isolated), '--output', str(output), '--worker']
    subprocess.run(command + ['before'], check=True, env=env)
    # Replace only program resources, as a user upgrading in place would do.
    shutil.copytree(root / 'app', isolated / 'app', dirs_exist_ok=True, ignore=shutil.ignore_patterns('__pycache__'))
    subprocess.run(command + ['after'], check=True, env=env)
    before = json.loads((output / 'before.json').read_text(encoding='utf-8'))
    after = json.loads((output / 'after.json').read_text(encoding='utf-8'))
    evidence = {'before': before, 'after': after, 'preserved': before == after and after['messages'] == 2}
    (output / 'upgrade-probe.json').write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(evidence, ensure_ascii=False))
