"""Open a portable desktop entry using only the generated U2 fixture."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

parser = argparse.ArgumentParser()
parser.add_argument('--package', type=Path, required=True)
parser.add_argument('--evidence', type=Path, required=True)
parser.add_argument('--smoke-test', action='store_true')
args = parser.parse_args()
package, evidence = args.package.resolve(), args.evidence.resolve()
evidence.mkdir(parents=True, exist_ok=True)
os.environ['WECHAT_ARCHIVE_DATA_DIR'] = str(evidence / 'state')
root = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(root), str(root / 'tests')]
from u2_synthetic import prepare
manager, source = prepare(evidence)
manager.executor.shutdown()
if args.smoke_test:
    subprocess.run([str(package / 'runtime/pythonw.exe'), str(package / 'app/desktop.py'), '--smoke-test'],
                   env=dict(os.environ), check=True, timeout=60, creationflags=subprocess.CREATE_NO_WINDOW)
    print((evidence / 'state/logs/smoke-test.json').read_text(encoding='utf-8'))
else:
    entry = package / '启动微信聊天档案.cmd'
    os.startfile(str(entry))
    record = dict(entry=str(entry), state=str(evidence / 'state'), runtime=str(package / 'runtime/pythonw.exe'),
                  launch_method='ShellExecute open (same association as double-click)')
    (evidence / 'launch.json').write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding='utf-8')
    print(str(evidence / 'launch.json'))
