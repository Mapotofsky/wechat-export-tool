"""Create a large synthetic source and open the actual portable desktop entry."""
import argparse
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import sys

parser = argparse.ArgumentParser()
parser.add_argument('--package', type=Path, required=True)
parser.add_argument('--evidence', type=Path, required=True)
parser.add_argument('--append', action='store_true')
parser.add_argument('--reopen', action='store_true')
args = parser.parse_args()
package, evidence = args.package.resolve(), args.evidence.resolve()
evidence.mkdir(parents=True, exist_ok=True)
os.environ['WECHAT_ARCHIVE_DATA_DIR'] = str(evidence / 'state')
root = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(root), str(root / 'tests')]
from u2_synthetic import prepare
from test_maintenance import table

if args.append or args.reopen:
    source = evidence / '合成 源库'
else:
    manager, source = prepare(evidence)
    manager.executor.shutdown()
if not args.reopen:
    with closing(sqlite3.connect(source / 'message/message_0.db')) as connection, connection:
        count = 500000 if args.append else 100000
        start = connection.execute(f'SELECT coalesce(max(local_id), 0) + 1 FROM "{table("u2_159")}"').fetchone()[0] if args.append else 100000
        connection.executemany(f'INSERT INTO "{table("u2_159")}" VALUES (?,?,1,8,?,?,0)',
            ((start + i, 1000000 + start + i, 1700000000 + i, 'synthetic U3 desktop') for i in range(count)))
if args.append:
    print('Appended synthetic messages')
    sys.exit(0)
entry = package / '启动微信聊天档案.cmd'
os.startfile(str(entry))
(evidence / 'launch.json').write_text(json.dumps({'entry':str(entry), 'state':str(evidence / 'state'),
    'runtime':str(package / 'runtime/pythonw.exe'), 'method':'ShellExecute'}, ensure_ascii=False), encoding='utf-8')
print(str(evidence / 'launch.json'))
