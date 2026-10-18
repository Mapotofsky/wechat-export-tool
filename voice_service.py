"""VoiceInfo lookup uses the media shard's own Name2Id, never message sender ids."""
from contextlib import closing
import json
import os
from pathlib import Path
import subprocess
import tempfile

import wechat_export as we
from runtime_paths import voice_python_executable, resource_root

PYTHON = voice_python_executable()
WORKER = resource_root() / 'local_asr.py'


class ToolUnavailable(RuntimeError):
    pass


def run_worker(python, *args, timeout):
    # pythonw hosts and Windows consoles can use different default encodings.
    return subprocess.run([str(python), str(WORKER), *args], capture_output=True,
                          text=True, encoding='utf-8', errors='replace', timeout=timeout,
                          env={**os.environ, 'PYTHONIOENCODING': 'utf-8'})


def process_failure(result):
    detail = (result.stderr or result.stdout or '').strip()
    return f'退出码 {result.returncode}；{detail[-4000:] or "进程未返回输出"}'


def probe(python=PYTHON):
    if not Path(python).is_file():
        return {'available': False, 'python': str(python), 'reason': '未包含语音组件。请解压完整语音包（voice-runtime、tools 和 models）到程序目录；开发环境可设置 WECHAT_ARCHIVE_VOICE_PYTHON。', 'models': []}
    try:
        result = run_worker(python, '--probe', timeout=60)
        if result.returncode or not result.stdout:
            raise RuntimeError(process_failure(result))
        info = json.loads(result.stdout)
        info['python'] = str(python)
        if not info['available']:
            info['reason'] = f'请在 "{python}" 中安装 requirements-voice.txt，并预先准备本地 .pt 模型与 ffmpeg；可运行 "{python}" local_asr.py --probe 检查'
        elif not info['silk']:
            info['reason'] = 'Whisper 可用；微信 SILK 语音仍需 pilk 解码包'
        return info
    except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as exc:
        return {'available': False, 'python': str(python), 'reason': f'本地工具检查失败（{type(exc).__name__}：{exc}）。解释器：{python}；工具：{WORKER}', 'models': []}


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
        try:
            process = run_worker(python, '--audio', str(source), '--model', str(model),
                                 '--output', str(output), timeout=1800)
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError('本地识别超时：单条语音处理超过 1800 秒') from exc
        if not output.exists():
            raise RuntimeError(f'本地识别进程未生成结果：{process_failure(process)}；解释器：{python}；模型：{model}')
        result = json.loads(output.read_text(encoding='utf-8'))
        if result['status'] != 'success' or not result.get('text'):
            raise RuntimeError(result.get('reason', '未识别到语音文本'))
        if process.returncode:
            raise RuntimeError(f'本地识别进程异常退出：{process_failure(process)}')
        return result['text']


def transcribe(archive, db_dir, usernames, model, progress=lambda *args: None, recognizer=recognize):
    selected = [(u, m) for u in usernames for m in archive.messages(u) if (m['type'] & 0xffffffff) == 34]
    counts = {'success': 0, 'reused': 0, 'missing': 0, 'failed': 0}
    errors = {}
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
            reason = f'{type(exc).__name__}：{exc}'
            archive.save_transcript(username, key, media, 'tool_unavailable' if isinstance(exc, ToolUnavailable) else 'failed',
                                    reason=reason, source=source)
            errors[reason] = errors.get(reason, 0) + 1
            counts['failed'] += 1
    progress('完成', len(selected), len(selected))
    if errors:
        counts['errors'] = [{'reason': reason, 'count': count} for reason, count in errors.items()]
    return counts
