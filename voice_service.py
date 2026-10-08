"""VoiceInfo lookup uses the media shard's own Name2Id, never message sender ids."""
from contextlib import closing
import json
from pathlib import Path
import subprocess
import tempfile

import wechat_export as we

PYTHON = Path(r'D:\efficiency\anaconda3\envs\pytorch\python.exe')
WORKER = Path(__file__).parent / 'local_asr.py'


class ToolUnavailable(RuntimeError):
    pass


def probe(python=PYTHON):
    if not Path(python).is_file():
        return {'available': False, 'reason': '本地识别 Python 环境不存在', 'models': []}
    try:
        result = subprocess.run([str(python), str(WORKER), '--probe'], capture_output=True,
                                text=True, encoding='utf-8', timeout=60)
        info = json.loads(result.stdout)
        info['python'] = str(python)
        if not info['available']:
            info['reason'] = '需要本地 Whisper、已缓存模型和 ffmpeg'
        elif not info['silk']:
            info['reason'] = 'Whisper 可用；微信 SILK 语音仍需 pilk 解码包'
        return info
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return {'available': False, 'reason': '本地工具检查失败，可重新检查', 'models': []}


def find_media(db_dir, username, message):
    matches = []
    for path in sorted((Path(db_dir) / 'message').glob('media_*.db')):
        with closing(we._connect_ro(str(path))) as c:
            row = c.execute('SELECT rowid FROM Name2Id WHERE user_name=?', (username,)).fetchone()
            if not row:
                continue
            # Match all message association fields to avoid wrong media for reused local ids.
            rows = c.execute('''SELECT voice_data,data_index FROM VoiceInfo
                WHERE chat_name_id=? AND local_id=? AND create_time=? AND svr_id=?''',
                             (row[0], message['local_id'], message['timestamp'], message.get('server_id', 0))).fetchall()
            for data, index in rows:
                if data:
                    matches.append((bytes(data), {'database': path.name, 'data_index': index,
                                                  'local_id': message['local_id'], 'server_id': message.get('server_id', 0)}))
    if not matches:
        return None, None
    if any(data != matches[0][0] for data, _ in matches[1:]):
        raise RuntimeError('多个媒体分片的语音内容不一致，未选择任意媒体')
    return matches[0]


def recognize(media, model, python=PYTHON):
    with tempfile.TemporaryDirectory() as tmp:
        source, output = Path(tmp) / 'voice.bin', Path(tmp) / 'result.json'
        source.write_bytes(media)
        subprocess.run([str(python), str(WORKER), '--audio', str(source), '--model', str(model),
                        '--output', str(output)], capture_output=True, timeout=1800)
        if not output.exists():
            raise RuntimeError('本地识别进程失败，请检查模型与运行环境')
        result = json.loads(output.read_text(encoding='utf-8'))
        if result['status'] != 'success' or not result.get('text'):
            raise RuntimeError(result.get('reason', '未识别到语音文本'))
        return result['text']


def transcribe(archive, db_dir, usernames, model, progress=lambda *args: None, recognizer=recognize):
    selected = [(u, m) for u in usernames for m in archive.messages(u) if (m['type'] & 0xffffffff) == 34]
    counts = {'success': 0, 'reused': 0, 'missing': 0, 'failed': 0}
    for index, (username, m) in enumerate(selected):
        progress('提取与本地转写', index, len(selected))
        key = m['message_id']
        old = archive.transcript(username, key)
        media, source = None, None
        try:
            media, source = find_media(db_dir, username, m)
            if media is None:
                # A previously successful result remains useful after media eviction.
                if old and old[1] == 'success':
                    counts['reused'] += 1
                    continue
                archive.save_transcript(username, key, None, 'media_missing', reason='本地未缓存关联语音媒体')
                counts['missing'] += 1
                continue
            if old and old[0] == media and old[1] == 'success':
                counts['reused'] += 1
                continue
            text = recognizer(media, model)
            archive.save_transcript(username, key, media, 'success', text=text, source=source)
            counts['success'] += 1
        except Exception as exc:
            archive.save_transcript(username, key, media, 'tool_unavailable' if isinstance(exc, ToolUnavailable) else 'failed',
                                    reason=str(exc), source=source)
            counts['failed'] += 1
    progress('完成', len(selected), len(selected))
    return counts
