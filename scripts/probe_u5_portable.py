"""Check a packaged CMD entry with isolated synthetic data and its actual served assets."""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import time
import urllib.request

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--package', type=Path, required=True)
parser.add_argument('--output', type=Path, required=True)
parser.add_argument('--entry', type=Path)
args = parser.parse_args()
repo = Path(__file__).resolve().parents[1]
package, output = args.package.resolve(), args.output.resolve()
output.mkdir(parents=True, exist_ok=False)
fixture = output / 'fixture'
subprocess.run([str(package/'runtime/python.exe'), str(repo/'scripts/content_analysis_fixture.py'),
                '--app', str(package/'app'), '--output', str(fixture), '--u5'], check=True)
os.environ['WECHAT_ARCHIVE_DATA_DIR'] = str(fixture/'state')
entry = args.entry.resolve() if args.entry else package/'启动微信聊天档案.cmd'
os.startfile(str(entry), arguments='--smoke-test')
report = fixture/'state/logs/smoke-test.json'
deadline = time.monotonic()+40
while not report.exists() and time.monotonic()<deadline:
    time.sleep(.1)
if not report.exists():
    raise RuntimeError('CMD entry did not reach the native loaded event; inspect '+str(fixture/'state/logs'))
smoke = json.loads(report.read_text(encoding='utf-8'))
assert smoke['subprocess_returncode'] == 0, smoke
assert Path(smoke['package_root']).resolve() == package
with urllib.request.urlopen(smoke['url'], timeout=5) as response:
    html = response.read()
static = package/'app/web/static'
assert html == (static/'index.html').read_bytes() == (repo/'web/static/index.html').read_bytes()
assets = re.findall(r'(?:src|href)="(/static/assets/[^"]+)"',html.decode('utf-8'))
for asset in assets:
    with urllib.request.urlopen(smoke['url']+asset, timeout=5) as response:
        served = response.read()
    relative = asset.removeprefix('/static/')
    assert served == (static/relative).read_bytes() == (repo/'web/static'/relative).read_bytes()
assert any(a.endswith('.js') for a in assets) and any(a.endswith('.css') for a in assets)
# The smoke window closes itself; wait for its local server to stop too.
stopped = False
deadline = time.monotonic()+20
while time.monotonic()<deadline:
    time.sleep(.2)
    try:
        urllib.request.urlopen(smoke['url']+'/api/data-status', timeout=1).close()
    except OSError:
        stopped = True
        break
assert stopped, 'native smoke server did not stop'
result = dict(entry=str(entry), method='ShellExecute (double-click association), --smoke-test',
              package=str(package), assets=assets, native_loaded=True, bundled_subprocess=0,
              served_bytes_equal=True, stopped=stopped, data=str(fixture/'state'))
(output/'result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps(result,ensure_ascii=False))
