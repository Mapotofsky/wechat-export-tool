"""Deny writes only in an isolated program copy; restore the ACL on exit."""
import argparse
import getpass
import json
import os
from pathlib import Path
import subprocess
import time

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--package',type=Path,required=True)
parser.add_argument('--data',type=Path,required=True)
args = parser.parse_args()
package, data = args.package.resolve(), args.data.resolve()
# Never apply the test ACL to an installed/user program or a personal data directory.
workspace_output = Path(__file__).resolve().parents[1] / 'output/u4'
assert package.is_relative_to(workspace_output) and data.is_relative_to(workspace_output)
data.mkdir(parents=True,exist_ok=True)
os.environ['WECHAT_ARCHIVE_DATA_DIR'] = str(data)
os.environ['WECHAT_ARCHIVE_HOME'] = str(package)
identity = getpass.getuser()
started = time.time()
try:
    subprocess.run(['icacls',str(package),'/deny',identity+':(OI)(CI)(WD,AD,WEA,WA)'],check=True,capture_output=True)
    try:
        (package / 'must-not-write').write_text('unexpected')
    except PermissionError:
        pass
    else:
        raise AssertionError('read-only ACL did not deny the actual write')
    os.startfile(str(package / '启动微信聊天档案.cmd'),arguments='--smoke-test')
    evidence = data / 'logs/smoke-test.json'
    deadline = time.monotonic()+60
    while time.monotonic()<deadline and (not evidence.exists() or evidence.stat().st_mtime<started):
        time.sleep(.2)
    assert evidence.exists() and evidence.stat().st_mtime>=started, 'CMD did not create fresh smoke evidence'
    result = json.loads(evidence.read_text(encoding='utf-8'))
    assert result['subprocess_returncode']==0
    result.update(readonly_write_denied=True, actual_cmd=str(package/'启动微信聊天档案.cmd'))
    (data / 'readonly-probe.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print('Read-only program / writable overridden data: passed')
finally:
    subprocess.run(['icacls',str(package),'/remove:d',identity],check=True,capture_output=True)
