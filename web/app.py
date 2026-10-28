# -*- coding: utf-8 -*-
"""Loopback-only local maintenance and export API."""
import datetime
import io
import json
import uuid
from pathlib import Path
from urllib.parse import quote
import zipfile
from contextlib import closing

from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse, FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from typing import Literal
from starlette.middleware.trustedhost import TrustedHostMiddleware

from maintenance import Manager, inspect_directory, identity_candidates
import voice_service
import wechat_export as we
from runtime_paths import data_root
import archive_analysis as analysis

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


@app.exception_handler(OSError)
async def file_error(request, exc):
    code = getattr(exc, 'winerror', None) or exc.errno
    return JSONResponse({'detail': f'文件操作失败（系统错误 {code}：{exc.strerror}），请检查所选路径与访问权限后重试'}, status_code=400)


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
    asr_provider: Literal['local', 'openai', 'dashscope'] | None = None
    asr_api_url: str | None = None
    asr_api_model: str | None = None
    asr_api_key: str | None = None


class VoiceTarget(BaseModel):
    username: str
    message_id: str


class TaskReq(BaseModel):
    account_id: str
    action: str
    usernames: list[str] | None = None
    group_id: str | None = None
    voice_targets: list[VoiceTarget] | None = None


class CancelTaskReq(BaseModel):
    account_id: str


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
        archive = manager.archive()
    # A long import can hold SQLite's write lock. Do not hold the task lock
    # while waiting: cancellation must still reach the import's next boundary.
    archive.require_visible([req.active])
    archive.save_config({'view_state': req.model_dump(exclude={'account_id'})})
    return {'saved': True}


class ExportReq(BaseModel):
    account_id: str
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
    status['setup']['has_readable_archive'] = bool(rows)
    return dict(status, has_data=bool(rows), contacts=len(rows))


@app.post('/api/check-directory')
def check_directory(req: DirectoryReq):
    return inspect_directory(req.directory)


@app.get('/api/suggest-directory')
def suggest_directory():
    import wcdb_key_tool_windows as keys
    candidates = []
    for directory in keys.candidate_db_dirs():
        try:
            candidates.append(dict(inspect_directory(directory), account_directory=Path(directory).parent.name))
        except (ValueError, OSError):
            continue
    return {'candidates': candidates, 'directory': '', 'decrypted_default': str(DB_DIR),
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
        values.update(keys_file=str(path), keys_error='')
    if req.asr_model is not None:
        path = Path(req.asr_model).expanduser().resolve()
        if req.asr_model and (not path.is_file() or path.suffix != '.pt'):
            raise ValueError('请选择已缓存的本地 Whisper .pt 模型文件')
        values['asr_model'] = str(path) if req.asr_model else ''
    for name in ('asr_provider', 'asr_api_url', 'asr_api_model', 'asr_api_key'):
        value = getattr(req, name)
        if value is not None:
            values[name] = value.strip()
    if any(name.startswith('asr_') for name in values):
        voice_service.validate_api(dict(archive.config(), **values))
    archive.save_config(values)
    return {'saved': True}


@app.post('/api/tasks')
def task(req: TaskReq):
    if req.action not in ('extract', 'prepare', 'discover', 'update', 'update_all', 'transcribe'):
        raise ValueError('未知任务')
    return manager.submit(req.action, None if req.usernames is None else list(dict.fromkeys(req.usernames)), req.group_id,
                          None if req.voice_targets is None else [t.model_dump() for t in req.voice_targets], req.account_id)


@app.post('/api/task-preview')
def task_preview(req: TaskReq):
    with manager.lock:
        result = manager.operation_preview(req.action, req.usernames, req.group_id, req.account_id)
        if req.voice_targets is not None:
            if req.action != 'transcribe':
                raise ValueError('仅语音转写支持消息选择')
            manager.archive().voice_targets(result['usernames'], [t.model_dump() for t in req.voice_targets])
        return result


@app.post('/api/tasks/{task_id}/cancel')
def cancel_task(task_id: str, req: CancelTaskReq):
    return manager.cancel(task_id, req.account_id)


@app.post('/api/tasks/{task_id}/cleanup')
def retry_task_cleanup(task_id: str, req: CancelTaskReq):
    return manager.retry_task_cleanup(task_id, req.account_id)


class VoiceQuery(BaseModel):
    usernames: list[str]
    status: str = ''
    q: str = ''
    offset: int = 0


@app.post('/api/voices')
def voices(req: VoiceQuery):
    return manager.archive().voice_page(list(dict.fromkeys(req.usernames)), req.status, req.q, req.offset)


@app.post('/api/followed')
def followed(req: FollowReq):
    with manager.lock:
        if manager.busy():
            raise ValueError('请等待当前任务完成后保存关注范围')
        manager.archive().save_config({'followed_usernames': list(dict.fromkeys(req.usernames))})
    return {'saved': True}


class GroupReq(BaseModel):
    id: str | None = None
    name: str = ''
    usernames: list[str] = Field(default_factory=list)
    delete: bool = False


@app.get('/api/groups')
def groups():
    return manager.archive().groups()


@app.post('/api/groups')
def save_group(req: GroupReq):
    with manager.lock:
        if manager.busy():
            raise ValueError('请等待当前任务完成后编辑项目')
        archive = manager.archive()
        if req.id == 'followed':
            if req.delete or req.name != '关注会话':
                raise ValueError('关注会话为内置项目，不能删除或改名')
            archive.save_config({'followed_usernames': list(dict.fromkeys(req.usernames))})
            return {'saved': True, 'id': 'followed'}
        existing = archive.config().get('session_groups', [])
        if req.id and not any(g['id'] == req.id for g in existing):
            raise ValueError('项目不存在，请重新加载')
        name = req.name.strip()
        if not req.delete and (not name or name in [g['name'] for g in archive.groups() if g['id'] != req.id]):
            raise ValueError('请填写不重复的项目名称')
        group_id = req.id or uuid.uuid4().hex
        updated = [g for g in existing if g['id'] != group_id]
        if not req.delete:
            updated.append({'id': group_id, 'name': name, 'usernames': list(dict.fromkeys(req.usernames))})
        archive.save_config({'session_groups': updated})
        return {'saved': True, 'id': group_id}


@app.get('/api/group-scope')
def group_scope(group_id: str):
    return manager.archive().group_scope(group_id)


class HiddenReq(FollowReq):
    account_id: str
    hidden: bool = True
    purge: bool = False
    expected_counts: dict[str, int] | None = None


@app.post('/api/hidden-preview')
def hidden_preview(req: HiddenReq):
    with manager.lock:
        manager.require_account(req.account_id)
        return dict(manager.archive().hidden_preview(req.usernames, req.purge), account_id=manager.account()['id'])


@app.get('/api/hidden')
def hidden_chats():
    return [r for r in manager.archive().directory_contacts(include_hidden=True) if r['hidden']]


@app.post('/api/hidden')
def save_hidden(req: HiddenReq):
    with manager.lock:
        manager.require_account(req.account_id)
        if manager.busy():
            raise ValueError('请等待当前维护任务完成后隐藏、恢复或清理会话')
        archive = manager.archive()
        preview = archive.hidden_preview(req.usernames, req.purge)
        if req.purge and req.expected_counts is None:
            raise ValueError('请先预览清理影响并核对数量')
        if req.expected_counts is not None and preview['counts'] != req.expected_counts:
            return dict(review_required=True, preview=dict(preview, account_id=manager.account()['id']))
        manager.cleanup = dict(account_id=manager.account()['id'], stage='准备清理' if req.purge else '保存隐藏状态', usernames=preview['usernames'])
    try:
        return archive.set_hidden(preview['usernames'], req.hidden, req.purge,
                                  lambda stage: manager.cleanup.update(stage=stage))
    finally:
        with manager.lock:
            manager.cleanup = None


@app.post('/api/reclaim-space')
def reclaim_space(req: HiddenReq):
    with manager.lock:
        manager.require_account(req.account_id)
        if manager.busy():
            raise ValueError('请等待当前操作完成')
        archive = manager.archive()
        manager.cleanup = dict(account_id=manager.account()['id'], stage='正在重试空间回收', usernames=[])
    try:
        return archive.reclaim_space()
    finally:
        with manager.lock:
            manager.cleanup = None


class SelectionReq(BaseModel):
    account_id: str
    filters: dict = Field(default_factory=dict)
    category: str = ''
    archive_state: str = ''
    min_count: int | None = Field(default=None, ge=0)
    max_count: int | None = Field(default=None, ge=0)
    last_start: int | None = None
    last_end: int | None = None
    include_unknown: bool = False


@app.post('/api/selection-preview')
def selection_preview(req: SelectionReq):
    with manager.lock:
        manager.require_account(req.account_id)
        validate_range(req.min_count, req.max_count)
        validate_range(req.last_start, req.last_end)
        allowed = {'q', 'category', 'start', 'end', 'sort', 'date_mode', 'include_unknown', 'group_id'}
        rows = contacts(**{k: v for k, v in req.filters.items() if k in allowed})
        result = []
        for row in rows:
            if req.category and row['category'] not in req.category.split(','):
                continue
            if req.archive_state and row['imported'] != (req.archive_state == 'imported'):
                continue
            numeric = req.min_count is not None or req.max_count is not None
            dated = req.last_start is not None or req.last_end is not None
            if not row['imported']:
                if (numeric or dated) and not req.include_unknown:
                    continue
            elif ((req.min_count is not None and row['total'] < req.min_count) or
                  (req.max_count is not None and row['total'] > req.max_count)):
                continue
            if row['imported'] and dated:
                last = row.get('last_timestamp')
                if last is None:
                    if not req.include_unknown:
                        continue
                elif ((req.last_start is not None and last < req.last_start) or
                      (req.last_end is not None and last > req.last_end)):
                    continue
            result.append(row)
        return dict(account_id=req.account_id, members=result, usernames=[r['username'] for r in result])


@app.get('/api/contacts')
def contacts(q: str = '', category: str = '', start: int | None = None, end: int | None = None,
             sort: str = 'recent', date_mode: str = 'span', include_unknown: bool = False,
             group_id: str = ''):
    validate_range(start, end)
    rows = manager.archive().directory_contacts() if manager.account() else []
    if group_id:
        members = {r['username'] for r in manager.archive().group_scope(group_id)['members']}
        rows = [r for r in rows if r['username'] in members]
    q = q.strip().casefold()
    fields = ('name', 'remark', 'nick_name', 'alias', 'username')
    names = manager.archive().config().get('contact_directory', []) if q and manager.account() else []
    matches = {r['username']: r for r in [*names, *rows] if q and any(q in (r.get(k) or '').casefold() for k in fields)}
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
        if category and r['category'] not in category.split(','):
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
    manager.archive().require_visible([username])
    validate_range(start, end)
    if offset < 0:
        raise ValueError('分页位置不能小于零')
    return manager.archive().messages(username, limit=5, start=start, end=end, offset=offset)


@app.get('/api/message-count')
def message_count(username: str, start: int | None = None, end: int | None = None):
    manager.archive().require_visible([username])
    validate_range(start, end)
    return {'total': manager.archive().message_count(username, start, end)}


@app.get('/api/messages')
def messages(username: str, q: str = '', start: int | None = None, end: int | None = None,
             day: str | None = None, include_transcripts: bool = True, offset: int = 0, limit: int = 50):
    archive = manager.archive()
    archive.require_visible([username])
    return archive.search(username, q, start, end, day, include_transcripts, offset, limit)


@app.get('/api/context')
def context(username: str, message_id: str, before: int = 10, after: int = 10):
    manager.archive().require_visible([username])
    return manager.archive().context(username, message_id, before, after)


class AnalysisFilters(BaseModel):
    username: str | None = None
    sender_key: str | None = None
    kind: int | None = None
    voice_status: str | None = None
    hour: int | None = Field(default=None, ge=0, le=23)
    weekday: int | None = Field(default=None, ge=0, le=6)
    start: int | None = None
    end: int | None = None
    terms: list[str] = Field(default_factory=list)
    term_mode: Literal['text', 'word', 'phrase', 'expression'] = 'text'
    independent_terms: bool = False


class AnalysisWindow(BaseModel):
    start: int | None = None
    end: int | None = None


class AnalysisReq(AnalysisWindow):
    account_id: str
    usernames: list[str]
    q: str = ''
    include_transcripts: bool = True
    filters: AnalysisFilters = Field(default_factory=AnalysisFilters)
    evidence_filters: AnalysisFilters = Field(default_factory=AnalysisFilters)
    comparison: AnalysisWindow = Field(default_factory=AnalysisWindow)
    gap_minutes: int = Field(default=30, gt=0)
    keywords: list[str] = Field(default_factory=list)
    anchor: str = ''
    session_offset: int = Field(default=0, ge=0)
    offset: int = Field(default=0, ge=0)


def analysis_archive(account_id):
    with manager.lock:
        if not manager.account() or manager.account()['id'] != account_id:
            raise ValueError('账号已切换，请重新加载分析')
        return manager.archive()


@app.post('/api/analysis')
def analyze(req: AnalysisReq):
    archive = analysis_archive(req.account_id)
    archive.require_visible(req.usernames)
    return analysis.analyze(archive, req)


@app.post('/api/analysis/messages')
def analyze_messages(req: AnalysisReq):
    validate_range(req.start, req.end)
    archive = analysis_archive(req.account_id)
    archive.require_visible(req.usernames)
    return analysis.analysis_messages(archive, req)


class BlockRule(BaseModel):
    text: str
    mode: Literal['exact', 'contains'] = 'exact'


class BlocklistReq(BaseModel):
    account_id: str
    rules: list[BlockRule]


@app.get('/api/analysis/blocklist')
def analysis_blocklist(account_id: str):
    return {'rules': analysis.rules_for(analysis_archive(account_id))}


@app.post('/api/analysis/blocklist')
def save_analysis_blocklist(req: BlocklistReq):
    rules = analysis.normalize_rules([r.model_dump() for r in req.rules])
    with manager.lock:
        archive = analysis_archive(req.account_id)
        archive.save_config({'analysis_blocklist': rules})
    return {'saved': True, 'rules': rules}


class AnalysisOptions(BaseModel):
    content_only: bool = False
    hide_numbers: bool = False
    ignore_structured: bool = True
    deduplicate: bool = True
    discover: bool = True
    complete_phrases: bool = True
    min_phrase_messages: int = Field(default=2,ge=1,le=100)


class AliasGroup(BaseModel):
    id: str
    name: str
    members: list[str]
    enabled: bool = True


class AnalysisSettingsReq(BaseModel):
    account_id: str
    options: AnalysisOptions
    aliases: list[AliasGroup]


@app.get('/api/analysis/settings')
def analysis_settings(account_id: str):
    with manager.lock:
        archive=analysis_archive(account_id)
        with closing(archive.connect()) as c:
            return analysis.analysis_context(c)


@app.post('/api/analysis/settings')
def save_analysis_settings(req: AnalysisSettingsReq):
    aliases=analysis.lex.normalize_aliases([g.model_dump() for g in req.aliases])
    with manager.lock:
        archive=analysis_archive(req.account_id)
        archive.save_config(dict(analysis_options=req.options.model_dump(),analysis_aliases=aliases))
        with closing(archive.connect()) as c:
            return analysis.analysis_context(c)


def download(data, filename, media_type):
    return StreamingResponse(io.BytesIO(data), media_type=media_type,
        headers={'Content-Disposition': f"attachment; filename=\"wechat_export.{filename.rsplit('.',1)[-1]}\"; filename*=UTF-8''{quote(filename)}"})


@app.post('/api/export-search')
def export_search(req: SearchExportReq):
    if len(set(req.usernames)) != 1:
        raise ValueError('命中导出请选择一个会话')
    if req.format not in ('txt', 'json', 'zip'):
        raise ValueError('格式必须为 txt、json 或 zip')
    with manager.lock:
        manager.require_account(req.account_id)
        archive = manager.archive()
    archive.require_visible(req.usernames)
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
    with manager.lock:
        manager.require_account(req.account_id)
        archive = manager.archive()
    with closing(archive.connect()) as connection:
        connection.execute('BEGIN')
        return export_snapshot(req, archive, connection)


def export_snapshot(req, archive, connection):
    archive.require_visible(req.usernames)
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
    config = manager.archive().config() if manager.account() else {}
    online = config.get('asr_provider', 'local') != 'local'
    info = voice_service.probe(online=True) if online else voice_service.probe()
    if not online and config.get('asr_model') and Path(config['asr_model']).is_file():
        info['available'] = bool(info.get('whisper') and info.get('ffmpeg'))
    return dict(info, provider=config.get('asr_provider', 'local'))


@app.get('/')
def index():
    return FileResponse(Path(__file__).parent / 'static' / 'index.html')


app.mount('/static', StaticFiles(directory=Path(__file__).parent / 'static'), name='static')
