"""Isolated U2 browser fixture; never extracts keys or calls a recognizer."""
import argparse
import os
from pathlib import Path
import sys
import tempfile

parser = argparse.ArgumentParser()
parser.add_argument('--root', type=Path, required=True)
parser.add_argument('--port', type=int, default=8772)
parser.add_argument('--prepare-only', action='store_true')
parser.add_argument('--fail-reclaim-once', action='store_true')
parser.add_argument('--large-directory', action='store_true')
args = parser.parse_args()
args.root = args.root.resolve()
args.root.mkdir(parents=True, exist_ok=True)
os.environ['WECHAT_ARCHIVE_DATA_DIR'] = str(args.root / 'bootstrap')
sys.path[:0] = [str(Path(__file__).resolve().parent.parent), str(Path(__file__).resolve().parent)]
from u2_synthetic import prepare
from web import app as web
web.manager, source = prepare(args.root)
if args.large_directory:
    import sqlite3
    from contextlib import closing
    with closing(sqlite3.connect(source / 'contact/contact.db')) as c, c:
        c.executemany('INSERT INTO contact VALUES (?,?,3,?,?)',
                      [(f'contact_only_{i}', '', f'仅目录项{i}', '') for i in range(2000)])
    web.manager.archive().prepare_directory(source)
if args.prepare_only:
    web.manager.executor.shutdown()
    print(str(args.root / 'state'))
else:
    import uvicorn
    # Deterministic local mock; no audio, process scan or external API is used.
    import voice_service
    voice_service.probe = lambda **kwargs: {'available': True, 'models': [], 'silk': True, 'ffmpeg': True}
    def mock_transcribe(archive, db_dir, users, model, progress, recognizer, targets=None, force=False, cancellation=None, on_publish=lambda counts: None):
        rows = archive.voice_targets(users, targets)
        success, reused = 0, 0
        for index, (u, message) in enumerate(rows):
            previous = archive.transcript(u, message['message_id'])
            if previous and previous[1] == 'success' and not force:
                reused += 1
            else:
                archive.save_transcript(u, message['message_id'], b'synthetic', 'success', text='U2 模拟识别结果')
                success += 1
            progress('合成识别器', index + 1, len(rows))
        return {'success': success, 'reused': reused, 'missing': 0, 'failed': 0}
    voice_service.transcribe = mock_transcribe
    if args.fail_reclaim_once:
        archive = web.manager.archive()
        original_reclaim = archive.reclaim_space
        def fail_once():
            import time
            time.sleep(3)  # Keep the committed-space-reclaim stage observable in UI acceptance.
            archive.reclaim_space = original_reclaim
            return dict(space_reclaimed=False, warning='档案已清理，空间回收未完成：合成磁盘空间不足；请重试空间回收。')
        archive.reclaim_space = fail_once
    uvicorn.run(web.app, host='127.0.0.1', port=args.port, access_log=False)
