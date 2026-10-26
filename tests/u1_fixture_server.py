"""U1 browser fixture, including simulated encrypted preparation and extraction."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import sqlite3
from contextlib import closing

parser = argparse.ArgumentParser()
parser.add_argument('--root', type=Path, required=True)
parser.add_argument('--port', type=int, default=8767)
parser.add_argument('--fail-prepare-once', action='store_true')
args = parser.parse_args()
root = args.root.resolve()
root.mkdir(parents=True, exist_ok=True)
os.environ['WECHAT_ARCHIVE_DATA_DIR'] = str(root / 'state')
sys.path[:0] = [str(Path(__file__).resolve().parent.parent), str(Path(__file__).resolve().parent)]
from test_maintenance import seed
from web import app as web
from maintenance import inspect_directory
import maintenance
import uvicorn

sources = [root / name for name in ('账号 A/db_storage', '账号 B/db_storage', '加密账号/db_storage')]
for source in sources:
    if not source.exists():
        seed(source)
for source in sources[:2]:
    with closing(sqlite3.connect(source / 'contact/contact.db')) as c,c:
        c.execute("UPDATE contact SET nick_name='同名' WHERE username IN ('wxid_a','wxid_b')")
if (sources[2] / 'contact/contact.db').read_bytes().startswith(b'SQLite'):
    (sources[2] / 'contact/contact.db').write_bytes(b'synthetic encrypted contact')
original_run = subprocess.run


def simulated_tool(command, **options):
    if len(command)>2 and command[2]=='extract':
        Path(command[-1]).write_text('{}', encoding='utf-8')
        Path(command[6]).write_text(json.dumps({'stage':'complete','databases':2,'verified_databases':2,
            'processes':1,'opened_processes':1,'candidate_count':2}), encoding='utf-8')
        return subprocess.CompletedProcess(command,0,b'',b'')
    return original_run(command, **options)


maintenance.subprocess.run = simulated_tool
fail_prepare = args.fail_prepare_once
def simulated_prepare(*a, **kw):
    global fail_prepare
    if fail_prepare:
        fail_prepare = False
        raise maintenance.encrypted_sync.KeyPreparationError('合成密钥未通过所选数据库认证，请重新保存密钥或提取')
    return sources[0], {}
maintenance.encrypted_sync.sync = simulated_prepare
def candidates():
    return {'directory':'', 'candidates':[dict(inspect_directory(s),account_directory=s.parent.name) for s in sources]}
for route in web.app.routes:
    if getattr(route,'path','')=='/api/suggest-directory':
        route.dependant.call = candidates
print('U1_FIXTURE_ROOT='+str(root), flush=True)
uvicorn.run(web.app,host='127.0.0.1',port=args.port,access_log=False)
