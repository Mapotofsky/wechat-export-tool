# -*- coding: utf-8 -*-
"""仅供 Windows 本机使用的微信聊天记录网页服务。"""
import datetime
import io
import os
from pathlib import Path
import subprocess
import tempfile
from urllib.parse import quote
import zipfile

from fastapi import FastAPI, HTTPException, UploadFile, File
from fastapi.responses import StreamingResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import wechat_export as we


DB_DIR = Path(__file__).resolve().parent.parent / 'decrypted'
ASR_BIN = os.environ.get('ASR_BIN', '/app/asr/llama-funasr-sensevoice')
ASR_MODEL = os.environ.get('ASR_MODEL', '/app/models/sensevoice-small-q8.gguf')
ASR_VAD = os.environ.get('ASR_VAD', '/app/models/fsmn-vad.gguf')

app = FastAPI(title='微信聊天记录导出')


@app.get('/api/data-status')
def data_status():
    contacts = we.load_contacts(str(DB_DIR))
    return {'has_data': bool(contacts), 'contacts': len(contacts)}


@app.get('/api/contacts')
def contacts():
    return we.load_contacts(str(DB_DIR))


class ExportReq(BaseModel):
    usernames: list[str]


@app.post('/api/export')
def export(req: ExportReq):
    available = we.load_contacts(str(DB_DIR), include_official=True)
    contact_map = {contact['username']: contact for contact in available}
    usernames = [username for username in req.usernames if username in contact_map]
    if not usernames:
        raise HTTPException(status_code=400, detail='未选择有效联系人')

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as archive:
        for username in usernames:
            contact = contact_map[username]
            messages = we.get_messages(str(DB_DIR), username, other_name=contact['name'])
            if not messages:
                continue
            safe = we.export_basename(contact)
            archive.writestr(f'{safe}_聊天记录.txt', we.render_txt(contact['name'], messages))
            archive.writestr(f'{safe}_聊天记录.json', we.render_json(contact, messages))

    buf.seek(0)
    ts = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
    filename = f'微信聊天记录_{ts}.zip'
    ascii_name = f'wechat_export_{ts}.zip'
    return StreamingResponse(
        buf,
        media_type='application/zip',
        headers={'Content-Disposition':
                 f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(filename)}"},
    )


@app.post('/api/transcribe')
async def transcribe(file: UploadFile = File(...)):
    if not os.path.exists(ASR_BIN):
        raise HTTPException(status_code=503, detail='语音识别服务未部署')

    suffix = os.path.splitext(file.filename or 'audio.wav')[1].lower() or '.wav'
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp.write(await file.read())
        tmp_path = tmp.name

    try:
        wav_path = tmp_path
        if suffix != '.wav':
            conv = tmp_path + '.wav'
            result = subprocess.run(
                ['ffmpeg', '-y', '-i', tmp_path, '-ar', '16000', '-ac', '1', conv],
                capture_output=True, text=True,
            )
            if result.returncode != 0:
                raise HTTPException(status_code=400, detail=f'音频解码失败: {result.stderr[-200:]}')
            wav_path = conv

        cmd = [ASR_BIN, '-m', ASR_MODEL, '--vad', ASR_VAD, '-a', wav_path]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if result.returncode != 0:
            raise HTTPException(status_code=500, detail=f'识别失败: {result.stderr[-300:]}')
        return {'text': result.stdout.strip()}
    finally:
        for path in (tmp_path, tmp_path + '.wav'):
            if os.path.exists(path):
                os.remove(path)


@app.get('/api/asr-status')
def asr_status():
    return {
        'available': os.path.exists(ASR_BIN) and os.path.exists(ASR_MODEL),
        'model': os.path.basename(ASR_MODEL),
    }


@app.get('/')
def index():
    return FileResponse(os.path.join(os.path.dirname(__file__), 'static', 'index.html'))


app.mount('/static', StaticFiles(directory=os.path.join(os.path.dirname(__file__), 'static')), name='static')
