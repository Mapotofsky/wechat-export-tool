"""Launch the actual portable CMD with an isolated, synthetic U1 archive."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

parser = argparse.ArgumentParser()
parser.add_argument('--package',type=Path,required=True)
parser.add_argument('--evidence',type=Path,required=True)
parser.add_argument('--smoke-test',action='store_true')
args = parser.parse_args()
package = args.package.resolve()
evidence = args.evidence.resolve()
evidence.mkdir(parents=True,exist_ok=True)
state = evidence / 'local_data'
os.environ['WECHAT_ARCHIVE_DATA_DIR'] = str(state)
root = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(root),str(root / 'tests')]
from test_maintenance import seed
from maintenance import Manager
source = evidence / '合成源目录'
if not source.exists():
    seed(source)
manager = Manager(state)
manager.select(str(source),'U1 桌面合成',True)
manager.archive().save_config({'self_username':'wxid_self'})
manager.archive().update(source)
manager.executor.shutdown()
entry = package / '启动微信聊天档案.cmd'
if args.smoke_test:
    subprocess.run([str(package / 'runtime/pythonw.exe'),str(package / 'app/desktop.py'),'--smoke-test'],
        env=dict(os.environ),check=True,timeout=60,creationflags=subprocess.CREATE_NO_WINDOW)
    print((state / 'logs/smoke-test.json').read_text(encoding='utf-8'))
    sys.exit(0)
os.startfile(str(entry))
(evidence / 'launch.json').write_text(json.dumps({'entry':str(entry),'state':str(state),
    'launch_method':'ShellExecute open (same association as double-click)',
    'runtime':str(package / 'runtime/pythonw.exe')},ensure_ascii=False,indent=2),encoding='utf-8')
print(str(evidence / 'launch.json'))
