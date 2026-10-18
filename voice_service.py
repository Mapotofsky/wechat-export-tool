"""VoiceInfo lookup uses the media shard's own Name2Id, never message sender ids."""
from contextlib import closing
import json
import base64
import os
from pathlib import Path
import subprocess
import tempfile
import urllib.request
import urllib.error
from urllib.parse import urlsplit
import uuid

import wechat_export as we
from runtime_paths import voice_python_executable, resource_root

PYTHON = voice_python_executable()
WORKER = resource_root() / 'local_asr.py'


class ToolUnavailable(RuntimeError):
    pass


def run_worker(python, *args, timeout):
    # pythonw hosts and Windows consoles can use different default encodings.
    return subprocess.run([str(python), str(WORKER), *args], capture_output=True,
                          creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
                          text=True, encoding='utf-8', errors='replace', timeout=timeout,
                          env={**os.environ, 'PYTHONIOENCODING': 'utf-8'})


def process_failure(result):
    detail = (result.stderr or result.stdout or '').strip()
    return f'退出码 {result.returncode}；{detail[-4000:] or "进程未返回输出"}'


def probe(python=PYTHON, *, online=False):
    if not Path(python).is_file():
        return {'available': False, 'python': str(python), 'reason': '未包含语音组件。请解压完整语音包（voice-runtime、tools 和 models）到程序目录；开发环境可设置 WECHAT_ARCHIVE_VOICE_PYTHON。', 'models': []}
    try:
        result = run_worker(python, '--probe', *(['--decoder-only'] if online else []), timeout=60)
        if result.returncode or not result.stdout:
            raise RuntimeError(process_failure(result))
        info = json.loads(result.stdout)
        if online:
            info['available'] = info.get('decoder_available', False)
        info['python'] = str(python)
        if not info['available']:
            info['reason'] = ('在线识别需要本地 FFmpeg 音频转换工具及微信 SILK 解码包 pilk；请安装语音组件。' if online else
                              f'请在 "{python}" 中安装 requirements-voice.txt，并预先准备本地 .pt 模型与 ffmpeg；可运行 "{python}" local_asr.py --probe 检查')
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


def recognize(media, model, python=PYTHON, *, details=False):
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
        return result if details else result['text']


def validate_api(config):
    provider = config.get('asr_provider', 'local')
    if provider not in ('local', 'openai', 'dashscope'):
        raise ValueError('请选择本地、OpenAI 兼容或阿里 Fun-ASR-Flash 接口')
    if provider == 'local':
        return
    url = urlsplit(config.get('asr_api_url', '').strip())
    if url.scheme not in ('http', 'https') or not url.netloc or url.username or url.password or url.fragment:
        raise ValueError('请填写完整的 HTTP(S) 语音识别接口地址，凭据请填入 API Key')
    if not config.get('asr_api_model', '').strip():
        raise ValueError('请填写在线识别模型名')
    if provider == 'dashscope' and not config.get('asr_api_key', '').strip():
        raise ValueError('阿里接口需要 API Key')


def recognize_online(media, config, python=PYTHON):
    validate_api(config)
    with tempfile.TemporaryDirectory() as tmp:
        source, wav = Path(tmp) / 'voice.bin', Path(tmp) / 'voice.wav'
        source.write_bytes(media)
        process = run_worker(python, '--audio', str(source), '--decode-output', str(wav), timeout=180)
        if process.returncode or not wav.is_file():
            raise RuntimeError('在线识别前音频解码失败：' + process_failure(process))
        return request_online(wav.read_bytes(), config)


def request_online(audio, config):
    """Send decoded WAV directly; never upload source databases or create public URLs."""
    provider, model = config['asr_provider'], config['asr_api_model'].strip()
    headers = {}
    key = config.get('asr_api_key', '').strip()
    if key:
        headers['Authorization'] = 'Bearer ' + key
    if provider == 'dashscope':
        headers.update({'Content-Type': 'application/json', 'X-DashScope-SSE': 'disable'})
        body = json.dumps({'model': model, 'input': {'messages': [{'role': 'user', 'content': [
            {'type': 'input_audio', 'input_audio': {'data': 'data:audio/wav;base64,' + base64.b64encode(audio).decode()}}
        ]}]}, 'parameters': {'format': 'wav', 'sample_rate': '16000'}}).encode()
    else:
        boundary = uuid.uuid4().hex
        headers['Content-Type'] = 'multipart/form-data; boundary=' + boundary
        body = (f'--{boundary}\r\nContent-Disposition: form-data; name="model"\r\n\r\n{model}\r\n'
                f'--{boundary}\r\nContent-Disposition: form-data; name="response_format"\r\n\r\njson\r\n'
                f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="voice.wav"\r\n'
                'Content-Type: audio/wav\r\n\r\n').encode() + audio + f'\r\n--{boundary}--\r\n'.encode()
    request = urllib.request.Request(config['asr_api_url'].strip(), data=body, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=180) as response:
            result = json.load(response)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode('utf-8', errors='replace')[:2000]
        if key:
            detail = detail.replace(key, '[API Key]')
        raise RuntimeError(f'在线识别 HTTP {exc.code}：{detail}') from None
    except (urllib.error.URLError, TimeoutError) as exc:
        raise RuntimeError(f'在线识别连接失败：{exc.reason if isinstance(exc, urllib.error.URLError) else exc}') from None
    except ValueError:
        raise RuntimeError('在线识别未返回 JSON；请检查接口地址及协议') from None
    output = result.get('output', {})
    text = (output.get('text') or output.get('sentence', {}).get('text') or
            output.get('output', {}).get('sentence', {}).get('text')) if provider == 'dashscope' else result.get('text')
    if not isinstance(text, str) or not text.strip():
        raise RuntimeError(f'在线识别未返回文本：{result.get("code") or "空结果"}；请检查模型和接口协议')
    return {'text': text.strip(), 'provider': provider, 'model': model, 'device': 'online'}


def transcribe(archive, db_dir, usernames, model, progress=lambda *args: None, recognizer=recognize,
               targets=None, force=False):
    selected = archive.voice_targets(usernames, targets)
    counts = {'success': 0, 'reused': 0, 'missing': 0, 'failed': 0}
    errors = {}
    for index, (username, m) in enumerate(selected):
        progress('提取与语音转写', index, len(selected))
        key = m['message_id']
        old = archive.transcript(username, key)
        media, source = None, None
        try:
            media, source = find_media(db_dir, username, m)
            if media is None:
                # A previously successful result remains useful after media eviction.
                if old and old[1] == 'success':
                    if force:
                        raise RuntimeError('本地媒体缺失，无法重新识别；已保留原成功转写')
                    counts['reused'] += 1
                    continue
                archive.save_transcript(username, key, None, 'media_missing', reason='本地未缓存关联语音媒体')
                counts['missing'] += 1
                continue
            if not force and old and old[0] == media and old[1] == 'success':
                counts['reused'] += 1
                continue
            result = recognizer(media, model)
            text = result['text'] if isinstance(result, dict) else result
            if isinstance(result, dict):
                source.update({k: result[k] for k in ('provider', 'model', 'device', 'device_name') if k in result})
                counts['device'] = result.get('device_name') or result.get('device', '')
                if result.get('device_reason'):
                    counts.setdefault('notes', [])
                    if result['device_reason'] not in counts['notes']:
                        counts['notes'].append(result['device_reason'])
            archive.save_transcript(username, key, media, 'success', text=text, source=source)
            counts['success'] += 1
        except Exception as exc:
            reason = f'{type(exc).__name__}：{exc}'
            if force and old and old[1] == 'success':
                archive.record_transcript_error(username, key, reason)
            else:
                archive.save_transcript(username, key, media, 'tool_unavailable' if isinstance(exc, ToolUnavailable) else 'failed',
                                        reason=reason, source=source)
            errors[reason] = errors.get(reason, 0) + 1
            counts['failed'] += 1
    progress('完成', len(selected), len(selected))
    if errors:
        counts['errors'] = [{'reason': reason, 'count': count} for reason, count in errors.items()]
    return counts
