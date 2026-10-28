"""A slow, isolated import for manual task-panel acceptance."""
import argparse
import os
from pathlib import Path
import sys
import time

parser = argparse.ArgumentParser()
parser.add_argument('--root', type=Path, required=True)
parser.add_argument('--port', type=int, default=8773)
args = parser.parse_args()
args.root = args.root.resolve()
args.root.mkdir(parents=True, exist_ok=True)
os.environ['WECHAT_ARCHIVE_DATA_DIR'] = str(args.root / 'bootstrap')
sys.path[:0] = [str(Path(__file__).resolve().parent.parent), str(Path(__file__).resolve().parent)]
from u2_synthetic import prepare
from web import app as web
import uvicorn

web.manager, source = prepare(args.root)
archive = web.manager.archive()
original = archive.update
first = True

def slow_update(db_dir, progress, **kwargs):
    global first
    if first:
        first = False
        try:
            for i in range(100):
                progress('合成导入批次', i, 100)
                time.sleep(.2)
        finally:
            time.sleep(2)
    return original(db_dir, progress, **kwargs)

archive.update = slow_update
uvicorn.run(web.app, host='127.0.0.1', port=args.port, access_log=False)
