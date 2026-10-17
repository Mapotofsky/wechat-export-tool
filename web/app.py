# -*- coding: utf-8 -*-
"""Loopback-only local maintenance and export API."""
import datetime
import io
import json
from pathlib import Path
from urllib.parse import quote
import zipfile
from contextlib import closing

from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse, FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.middleware.trustedhost import TrustedHostMiddleware

from maintenance import Manager, inspect_directory, identity_candidates
import voice_service
import wechat_export as we

ROOT = Path(__file__).resolve().parent.parent
DB_DIR = ROOT / 'decrypted'
DB_SUGGESTION_EXPLICIT = False
manager = Manager(ROOT / 'local_data')
app = FastAPI(title='微信聊天档案', docs_url=None, redoc_url=None)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=['127.0.0.1', 'localhost', 'testserver'])


@app.middleware('http')
async def local_origin(request: Request, call_next):
    origin = request.headers.get('origin')
    if request.method != 'GET' and origin and origin != f'http://{request.headers.get("host")}':
        return JSONResponse({'detail': '请从本机工具网页操作'}, status_code=403)
    return await call_next(request)


@app.exception_handler(ValueError)
async def invalid_request(request, exc):
    return JSONResponse({'detail': str(exc)}, status_code=400)


class DirectoryReq(BaseModel):
    directory: str


class AccountReq(DirectoryReq):
    label: str
    confirmed: bool = False


class IdentityReq(BaseModel):
    query: str


class SettingsReq(BaseModel):
    self_username: str | None = None
    my_name: str = '我'
    confirmed: bool = False
    keys_file: str | None = None
    asr_model: str | None = None


class TaskReq(BaseModel):
    action: str
    usernames: list[str] | None = None


class FollowReq(BaseModel):
    usernames: list[str]


class ExportReq(BaseModel):
    usernames: list[str]
    format: str = 'zip'
    start: int | None = None
    end: int | None = None


def validate_range(start, end):
    if start is not None and end is not None and start > end:
        raise ValueError('开始日期不能晚于结束日期')


@app.get('/api/data-status')
def data_status():
    status = manager.status()
    rows = manager.archive().contacts() if manager.account() else []
    return dict(status, has_data=bool(rows), contacts=len(rows))


@app.post('/api/check-directory')
def check_directory(req: DirectoryReq):
    return inspect_directory(req.directory)


@app.get('/api/suggest-directory')
def suggest_directory():
    import wcdb_key_tool_windows as keys
    return {'directory': keys.auto_detect_db_dir() or '', 'decrypted_default': str(DB_DIR),
            'explicit': DB_SUGGESTION_EXPLICIT}


@app.post('/api/account')
def account(req: AccountReq):
    return manager.select(req.directory, req.label, req.confirmed)


@app.post('/api/identity-candidates')
def identity(req: IdentityReq):
    config = manager.archive().config()
    return identity_candidates(config.get('decrypted_dir', ''), req.query.strip())


@app.post('/api/settings')
def settings(req: SettingsReq):
    if manager.busy():
        raise ValueError('请等待当前任务完成后保存配置')
    archive = manager.archive()
    values = {}
    if req.self_username is not None:
        if not req.confirmed:
            raise ValueError('请确认选择的内部 username 确实属于自己')
        candidates = identity_candidates(archive.config().get('decrypted_dir', ''), req.self_username)
        if not any(c['username'] == req.self_username for c in candidates):
            raise ValueError('此内部 username 未出现在当前账号数据库中；请核对后再确认')
        values.update(self_username=req.self_username, my_name=req.my_name.strip() or '我')
    if req.keys_file is not None:
        path = Path(req.keys_file).expanduser().resolve()
        if not path.is_file():
            raise ValueError('密钥文件不存在')
        values['keys_file'] = str(path)
    if req.asr_model is not None:
        path = Path(req.asr_model).expanduser().resolve()
        if not path.is_file() or path.suffix != '.pt':
            raise ValueError('请选择已缓存的本地 Whisper .pt 模型文件')
        values['asr_model'] = str(path)
    archive.save_config(values)
    return {'saved': True}


@app.post('/api/tasks')
def task(req: TaskReq):
    if req.action not in ('extract', 'prepare', 'discover', 'update', 'update_all', 'transcribe'):
        raise ValueError('未知任务')
    return manager.submit(req.action, None if req.usernames is None else list(dict.fromkeys(req.usernames)))


@app.post('/api/followed')
def followed(req: FollowReq):
    if manager.busy():
        raise ValueError('请等待当前任务完成后保存关注范围')
    manager.archive().save_config({'followed_usernames': list(dict.fromkeys(req.usernames))})
    return {'saved': True}


@app.get('/api/contacts')
def contacts(q: str = '', category: str = '', start: int | None = None, end: int | None = None,
             sort: str = 'recent', date_mode: str = 'span', include_unknown: bool = False):
    validate_range(start, end)
    rows = manager.archive().directory_contacts() if manager.account() else []
    q = q.strip().casefold()
    fields = ('name', 'remark', 'nick_name', 'alias', 'username')
    matches = {r['username']: r for r in rows if q and any(q in (r.get(k) or '').casefold() for k in fields)}
    result = []
    for r in rows:
        reasons = []
        if q:
            if q in r['name'].casefold():
                reasons.append('聊天名称命中')
            if r['username'] in matches:
                reasons.append('联系人命中：' + r['name'] + ' (' + r['username'] + ')')
            if r['category'] == 'group':
                for sender in r.get('recorded_senders', []):
                    u = sender.get('username')
                    if u in matches or q in (sender.get('name') or '').casefold() or q in (u or '').casefold():
                        person = matches.get(u, sender)
                        reasons.append('群关联命中（已收录发送者）：' + person['name'] + ' (' + (u or '身份未知') + ')')
            if not reasons:
                continue
        if category and r['category'] != category:
            continue
        if start is not None or end is not None:
            unknown = r['first_timestamp'] is None
            if unknown:
                if not include_unknown:
                    continue
            elif date_mode == 'activity':
                if not manager.archive().message_count(r['username'], start, end):
                    continue
            elif ((start is not None and r['first_timestamp'] < start) or
                  (end is not None and r['last_timestamp'] > end)):
                continue
        result.append(dict(r, match_reasons=reasons))
    rows = result
    rows.sort(key=lambda r: (r['total'] if sort == 'count' else r['last_timestamp'] or 0, r['username']), reverse=True)
    return rows


@app.get('/api/preview')
def preview(username: str, start: int | None = None, end: int | None = None, offset: int = 0):
    validate_range(start, end)
    if offset < 0:
        raise ValueError('分页位置不能小于零')
    return manager.archive().messages(username, limit=5, start=start, end=end, offset=offset)


@app.get('/api/message-count')
def message_count(username: str, start: int | None = None, end: int | None = None):
    validate_range(start, end)
    return {'total': manager.archive().message_count(username, start, end)}


def download(data, filename, media_type):
    return StreamingResponse(io.BytesIO(data), media_type=media_type,
        headers={'Content-Disposition': f"attachment; filename=\"wechat_export.{filename.rsplit('.',1)[-1]}\"; filename*=UTF-8''{quote(filename)}"})


@app.post('/api/export')
def export(req: ExportReq):
    archive = manager.archive()
    with closing(archive.connect()) as connection:
        connection.execute('BEGIN')
        return export_snapshot(req, archive, connection)


def export_snapshot(req, archive, connection):
    validate_range(req.start, req.end)
    available = {c['username']: c for c in archive.contacts(connection)}
    if set(req.usernames) - available.keys():
        raise ValueError('所选会话尚未导入；请先导入所选会话')
    selected = [available[u] for u in dict.fromkeys(req.usernames) if u in available]
    if not selected:
        raise ValueError('未选择有效聊天')
    if req.format not in ('txt', 'json', 'zip'):
        raise ValueError('格式必须为 txt、json 或 zip')
    if req.format != 'zip' and len(selected) == 1:
        contact = selected[0]
        messages = archive.messages(contact['username'], connection, start=req.start, end=req.end)
        contact = dict(contact, total=len(messages), export_range={'start': req.start, 'end': req.end})
        content = we.render_txt(contact['name'], messages) if req.format == 'txt' else we.render_json(contact, messages)
        return download(content.encode('utf-8'), we.export_basename(contact) + '.' + req.format,
                        'text/plain; charset=utf-8' if req.format == 'txt' else 'application/json')
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as zip_file:
        for contact in selected:
            messages = archive.messages(contact['username'], connection, start=req.start, end=req.end)
            contact = dict(contact, total=len(messages), export_range={'start': req.start, 'end': req.end})
            base = we.export_basename(contact)
            if req.format in ('zip', 'txt'):
                zip_file.writestr(base + '_聊天记录.txt', we.render_txt(contact['name'], messages))
            if req.format in ('zip', 'json'):
                zip_file.writestr(base + '_聊天记录.json', we.render_json(contact, messages))
    return download(buffer.getvalue(), '微信聊天记录_' + datetime.datetime.now().strftime('%Y%m%d_%H%M%S') + '.zip', 'application/zip')


@app.get('/api/asr-status')
def asr_status():
    return voice_service.probe()


@app.get('/')
def index():
    return FileResponse(Path(__file__).parent / 'static' / 'index.html')


app.mount('/static', StaticFiles(directory=Path(__file__).parent / 'static'), name='static')
