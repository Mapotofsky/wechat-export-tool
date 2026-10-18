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
from runtime_paths import data_root

ROOT = Path(__file__).resolve().parent.parent
DB_DIR = ROOT / 'decrypted'
DB_SUGGESTION_EXPLICIT = False
manager = Manager(data_root())
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


class ViewStateReq(BaseModel):
    account_id: str
    active: str = ''
    offset: int = 0
    ms: str = ''
    me: str = ''
    scroll: float = 0


@app.get('/api/view-state')
def view_state(account_id: str | None = None):
    with manager.lock:
        if account_id is not None and (not manager.account() or manager.account()['id'] != account_id):
            raise ValueError('账号已切换，请重新加载阅读位置')
        return manager.archive().config().get('view_state', {})


@app.post('/api/view-state')
def save_view_state(req: ViewStateReq):
    with manager.lock:
        if not manager.account() or manager.account()['id'] != req.account_id:
            raise ValueError('账号已切换，未保存旧账号阅读位置')
        if req.offset < 0 or req.scroll < 0:
            raise ValueError('阅读位置不能小于零')
        manager.archive().save_config({'view_state': req.model_dump(exclude={'account_id'})})
    return {'saved': True}


class ExportReq(BaseModel):
    usernames: list[str]
    format: str = 'zip'
    start: int | None = None
    end: int | None = None


class SearchExportReq(ExportReq):
    q: str = ''
    day: str | None = None
    include_transcripts: bool = True


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


@app.get('/api/messages')
def messages(username: str, q: str = '', start: int | None = None, end: int | None = None,
             day: str | None = None, include_transcripts: bool = True, offset: int = 0, limit: int = 50):
    return manager.archive().search(username, q, start, end, day, include_transcripts, offset, limit)


@app.get('/api/context')
def context(username: str, message_id: str, before: int = 10, after: int = 10):
    return manager.archive().context(username, message_id, before, after)


def download(data, filename, media_type):
    return StreamingResponse(io.BytesIO(data), media_type=media_type,
        headers={'Content-Disposition': f"attachment; filename=\"wechat_export.{filename.rsplit('.',1)[-1]}\"; filename*=UTF-8''{quote(filename)}"})


@app.post('/api/export-search')
def export_search(req: SearchExportReq):
    if len(set(req.usernames)) != 1:
        raise ValueError('命中导出请选择一个会话')
    if req.format not in ('txt', 'json', 'zip'):
        raise ValueError('格式必须为 txt、json 或 zip')
    archive = manager.archive()
    with closing(archive.connect()) as connection:
        connection.execute('BEGIN')
        username = req.usernames[0]
        contact = next((c for c in archive.contacts(connection) if c['username'] == username), None)
        if contact is None:
            raise ValueError('所选会话尚未导入；请先导入所选会话')
        result = archive.search(username, req.q, req.start, req.end, req.day,
                                req.include_transcripts, limit=None, connection=connection)
        metadata = dict(result['query'], count=result['total'],
                        generated_at=datetime.datetime.now().astimezone().isoformat(timespec='seconds'))
        contact = dict(contact, total=result['total'], export_search=metadata)
        text = '命中导出条件：' + json.dumps(metadata, ensure_ascii=False) + '\n\n' + we.render_txt(contact['name'], result['messages'])
        record = we.render_json(contact, result['messages'])
        base = we.export_basename(contact) + '_搜索命中'
        if req.format != 'zip':
            content = text if req.format == 'txt' else record
            return download(content.encode('utf-8'), base + '.' + req.format,
                            'text/plain; charset=utf-8' if req.format == 'txt' else 'application/json')
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as bundle:
            bundle.writestr(base + '.txt', text)
            bundle.writestr(base + '.json', record)
        return download(buffer.getvalue(), base + '.zip', 'application/zip')


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
