"""Account registry and one background maintenance task; reads stay available."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import json
import errno
import sqlite3
import logging
from pathlib import Path
import subprocess
import shutil
import threading
import time
import traceback
import uuid
import preparation

from archive_store import Archive, now
import encrypted_sync
import voice_service
import supervised_process
import wechat_export as we
from task_lifecycle import Cancellation, TaskCancelled, CleanupError
from runtime_paths import python_executable, resource_root


def extraction_feedback(diagnostic, returncode, output_exists):
    total = diagnostic.get('databases', 0)
    verified = diagnostic.get('verified_databases', 0)
    if diagnostic.get('exception_type') in ('UnicodeEncodeError', 'UnicodeDecodeError'):
        return 'output_failed', '工具输出编码失败，请查看本次诊断后重试'
    if diagnostic.get('stage') == 'preflight' and diagnostic.get('system_code'):
        return 'file_read_failed', '数据库文件读取失败，请按诊断中的系统错误码检查源目录后重试'
    if diagnostic.get('databases') == 0:
        return 'no_databases', '未找到可验证的数据库，请重新检查所选源目录'
    if diagnostic.get('process_list_returncode'):
        return 'process_list_failed', '进程列表命令执行失败，请查看退出码并重试'
    if diagnostic.get('stage') == 'write_result' or (diagnostic.get('stage') == 'complete' and not output_exists):
        return 'write_failed', '结果文件未写入，请检查数据目录可写性和剩余空间后重试'
    if diagnostic.get('open_errors') and not diagnostic.get('opened_processes') and 5 in diagnostic['open_errors']:
        return 'access_denied', '系统拒绝读取微信进程，可尝试以管理员身份启动本工具后重试'
    if total and 0 < verified < total and output_exists and not returncode:
        return 'partial', f'仅验证通过 {verified}/{total} 个数据库；已保存可用密钥，可尝试准备通讯录，或核对目录后重试提取'
    if total and verified == total and output_exists and not returncode:
        return 'complete', f'验证通过 {verified}/{total} 个数据库；请继续准备通讯录'
    if diagnostic.get('processes') == 0:
        return 'not_running', '未发现运行中的微信，请登录对应账号后重试'
    if diagnostic.get('candidate_count'):
        return 'validation_failed', '发现候选但未通过校验；请核对当前登录账号的存储目录，确认数据库来自该目录后重试。现有证据不能确定是账号或权限问题'
    if diagnostic.get('opened_processes'):
        return 'no_candidates', '未取得可校验候选；请核对当前微信账号和存储目录，查看扫描覆盖及读取失败计数后重试'
    return 'unknown', '工具未能完成诊断，未取得可用密钥；请确认微信已登录，核对当前账号存储目录后重试'


def atomic_json(path, data):
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    # Windows readers/scanners can briefly hold a handle without delete sharing.
    # Retrying replacement preserves the previous complete JSON until publish.
    for attempt in range(21):
        try:
            tmp.replace(path)
            break
        except PermissionError:
            if attempt == 20:
                raise
            time.sleep(.05)


def inspect_directory(directory):
    path = Path(directory).expanduser().resolve()
    contact = path / 'contact' / 'contact.db'
    messages = [p for p in (path / 'message').glob('message_*.db') if we.is_message_shard(p.name)]
    if not contact.is_file() or not messages:
        raise ValueError('请选择 db_storage 或解密库根目录，需含 contact/contact.db 和 message/message_*.db')
    with contact.open('rb') as f:
        plain = f.read(16) == b'SQLite format 3\x00'
    return {'directory': str(path), 'mode': 'plaintext' if plain else 'encrypted',
            'account_directory': path.parent.name if path.name == 'db_storage' else path.name,
            'message_shards': len(messages),
            'wal_files': sum(Path(str(p) + '-wal').exists() for p in [contact] + messages)}


def identity_candidates(directory, query):
    if not query.strip():
        raise ValueError('请输入微信号、内部 username、昵称或备注')
    path = Path(directory) / 'contact' / 'contact.db'
    if not path.is_file():
        raise ValueError('请先解密准备数据，再查找与确认身份')
    with closing(we._connect_ro(str(path))) as c:
        columns = {r[1] for r in c.execute('PRAGMA table_info(contact)')}
        alias = 'alias' if 'alias' in columns else "'' AS alias"
        rows = c.execute(f'SELECT username,{alias},remark,nick_name FROM contact WHERE username=?' +
                         (' OR alias=?' if 'alias' in columns else '') +
                         ' OR instr(remark,?)>0 OR instr(nick_name,?)>0',
                         (query, query, query, query) if 'alias' in columns else (query, query, query)).fetchall()
        candidates = [{'username': u, 'alias': a, 'name': r or n or u, 'basis': 'contact.alias/username'}
                      for u, a, r, n in rows]
    # Explicit internal usernames may exist only in a shard's Name2Id.
    if not candidates:
        for shard in (Path(directory) / 'message').glob('message_*.db'):
            if not we.is_message_shard(shard.name):
                continue
            with closing(we._connect_ro(str(shard))) as c:
                try:
                    r = c.execute('SELECT user_name FROM Name2Id WHERE user_name=?', (query,)).fetchone()
                except Exception:
                    r = None
                if r:
                    candidates.append({'username': r[0], 'alias': '', 'name': r[0], 'basis': 'Name2Id.user_name'})
                    break
    return candidates


class Manager:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.registry_path = self.root / 'accounts.json'
        self.registry = json.loads(self.registry_path.read_text(encoding='utf-8')) if self.registry_path.exists() else {'active': None, 'accounts': []}
        self.lock = threading.RLock()
        self.archives = {}
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='wechat-maintenance')
        self.task = None
        self.cancellation = None
        for account in self.registry['accounts']:
            task_file = self.root / account['id'] / 'task.json'
            if task_file.exists():
                task = json.loads(task_file.read_text(encoding='utf-8'))
                if task['status'] in ('running', 'cancelling'):
                    task.update(status='interrupted', stage='上次任务已中断', reason='服务已重启；之前可用数据仍保留',
                                next_step='重新执行对应操作', finished=now())
                    atomic_json(task_file, task)
                if task.get('cleanup_resources') and account['id'] == self.registry.get('active'):
                    self.cleanup = {'stage': '任务资源清理未完成，请重试', 'type': 'task_resources',
                                    'task_id': task.get('id'), 'account_id': account['id']}

    def account(self):
        return next((a for a in self.registry['accounts'] if a['id'] == self.registry['active']), None)

    def archive(self, account=None):
        account = account or self.account()
        if not account:
            raise ValueError('请先检查数据目录并确认账号归属')
        with self.lock:
            if account['id'] not in self.archives:
                self.archives[account['id']] = Archive(self.root / account['id'])
            return self.archives[account['id']]

    def busy(self):
        return bool(getattr(self, 'cleanup', None)) or (self.task is not None and self.task['status'] in ('running', 'cancelling'))

    def cancel(self, task_id, account_id):
        with self.lock:
            self.require_account(account_id)
            task = self.task
            if not task or task['id'] != task_id or task['account_id'] != account_id:
                if self.account():
                    path = self.archive().directory / 'task.json'
                    if path.exists():
                        saved = json.loads(path.read_text(encoding='utf-8'))
                        if saved.get('id') == task_id and saved.get('account_id') == account_id and saved.get('status') not in ('running', 'cancelling'):
                            return saved
                raise ValueError('任务不存在或不属于当前账号')
            if task['status'] == 'running' and task.get('cancellable', True):
                self.cancellation.event.set()
                task.update(status='cancelling', cancel_boundary='等待当前安全边界及资源清理')
                atomic_json(self.archive().directory / 'task.json', task)
            return dict(task)

    def retry_task_cleanup(self, task_id, account_id):
        with self.lock:
            self.require_account(account_id)
            if not getattr(self, 'cleanup', None) or self.cleanup.get('type') != 'task_resources' or self.cleanup.get('task_id') != task_id:
                raise ValueError('没有需要重试的任务资源清理')
            path = self.archive().directory / 'task.json'
            task = self.task if self.task and self.task.get('id') == task_id else json.loads(path.read_text(encoding='utf-8'))
            self.cleanup['stage'] = '正在重试任务资源清理'
            try:
                for resource in task['cleanup_resources']:
                    target = Path(resource['path'])
                    if resource['directory']:
                        if target.exists():
                            shutil.rmtree(target)
                    else:
                        target.unlink(missing_ok=True)
            except OSError as exc:
                self.cleanup['stage'] = f'任务资源清理未完成：{exc}；请重试'
                raise
            task.pop('cleanup_resources', None)
            if task.get('reason', '').startswith('任务资源清理失败'):
                task.update(reason='', stage='资源清理完成')
            task.update(next_step='资源清理完成，可按原范围重新执行任务')
            atomic_json(path, task)
            self.cleanup = None
            return dict(task)

    def require_account(self, account_id):
        if account_id is not None and (not self.account() or self.account()['id'] != account_id):
            raise ValueError('账号已切换，请刷新并重新核对范围')

    def operation_preview(self, action, usernames=None, group_id=None, account_id=None):
        with self.lock:
            self.require_account(account_id)
            archive = self.archive()
            rows = archive.maintenance_contacts() if action == 'update_all' else archive.directory_contacts()
            available = {r['username']: r for r in rows}
            if group_id is not None:
                requested = next((g['usernames'] for g in archive.groups() if g['id'] == group_id), None)
                if requested is None:
                    raise ValueError('项目不存在，请重新选择')
                rows = archive.group_scope(group_id)['members']
                available = {r['username']: r for r in rows}
            elif action == 'update_all' and usernames is None:
                requested = list(available) + archive.config().get('hidden_usernames', [])
            else:
                requested = archive.config().get('followed_usernames', []) if usernames is None else usernames
            requested = list(dict.fromkeys(requested))
            members = [available[u] for u in requested if u in available]
            pending = [r for r in members if not r['imported']]
            actual = [r for r in members if r['imported']] if action in ('analysis', 'export', 'transcribe') else members
            return dict(account_id=self.account()['id'], account_label=self.account()['label'], action=action,
                        usernames=[r['username'] for r in actual], members=members, pending=pending,
                        excluded=[u for u in requested if u not in available])

    def select(self, directory, label, confirmed):
        if not confirmed or not label.strip():
            raise ValueError('请填写账号标签并明确确认此目录属于该账号')
        info = inspect_directory(directory)
        with self.lock:
            if self.busy():
                raise ValueError('任务正在执行，完成后再切换账号')
            account = next((a for a in self.registry['accounts'] if a['directory'] == info['directory']), None)
            if account and account['label'] != label.strip():
                raise ValueError('此目录已有独立档案，请使用其原账号标签；不能改绑到其他账号')
            if not account:
                account = dict(info, id=uuid.uuid4().hex, label=label.strip())
                self.registry['accounts'].append(account)
            else:
                account.update(info)
            self.registry['active'] = account['id']
            atomic_json(self.registry_path, self.registry)
            archive = self.archive()
            if info['mode'] == 'plaintext':
                archive.save_config({'decrypted_dir': info['directory']})
                archive.prepare_directory(info['directory'])
            return account

    def switch(self, account_id):
        with self.lock:
            if self.busy():
                raise ValueError('维护或清理正在执行，完成后再切换账号')
            account = next((a for a in self.registry['accounts'] if a['id'] == account_id), None)
            if account is None:
                raise ValueError('已保存账号不存在，请刷新后重试')
            registry = dict(self.registry, active=account_id)
            atomic_json(self.registry_path, registry)
            self.registry = registry
            return account

    def preparation_preview(self, action, usernames, targets=None):
        return preparation.capacity(preparation.plan(self.account(), self.archive(), action, usernames, targets), self.archive())

    def status(self):
        account = self.account()
        config, task = {}, None
        setup = None
        if account:
            archive = self.archive()
            config = archive.config()
            setup = self.setup(account, config)
            # 会话接口提供通讯录；定时状态轮询不重复传输整份目录。
            config.pop('contact_directory', None)
            config['asr_api_key_set'] = bool(config.pop('asr_api_key', ''))
        with self.lock:
            if self.task and account and self.task['account_id'] == account['id']:
                task = dict(self.task)
            elif account:
                path = archive.directory / 'task.json'
                if path.exists():
                    task = json.loads(path.read_text(encoding='utf-8'))
        return {'account': account, 'accounts': self.registry['accounts'], 'config': config, 'cleanup': getattr(self, 'cleanup', None), 'setup': setup or self.setup(config={}),
                'task': task, 'wal_validation': '尚未执行真实微信加密 WAL 端到端验证'}

    def setup(self, account=None, config=None):
        account = account or self.account()
        config = config if config is not None else (self.archive(account).config() if account else {})
        source = bool(account and (Path(account['directory']) / 'contact/contact.db').is_file()
                      and (Path(account['directory']) / 'message').is_dir())
        needs_keys = bool(account and account['mode'] == 'encrypted')
        has_keys = bool(config.get('keys_file') and Path(config['keys_file']).is_file())
        contacts_ready = bool(config.get('decrypted_dir') and
                              'contact_directory' in config and
                              (Path(config['decrypted_dir']) / 'contact/contact.db').is_file())
        identity_saved = bool(config.get('self_username'))
        has_archive = False
        if account:
            with closing(self.archive(account).connect()) as c:
                has_archive = bool(c.execute('SELECT 1 FROM chats LIMIT 1').fetchone())
        action = ('directory' if not source else 'extract' if needs_keys and (not has_keys or config.get('keys_error')) else
                  'prepare' if not contacts_ready else 'identity' if not identity_saved else 'read')
        return dict(directory_saved=bool(account), source_available=source, needs_keys=needs_keys,
                    has_keys=has_keys, contacts_ready=contacts_ready, identity_saved=identity_saved, has_readable_archive=has_archive,
                    next_action=action, blocked_reason=('源目录不可用，请重新选择目录；已有档案仍可阅读' if account and not source else
                    config.get('keys_error') or ('请先提取密钥或保存已有密钥路径' if needs_keys and not has_keys else '')))

    def submit(self, action, usernames=None, group_id=None, voice_targets=None, account_id=None):
        with self.lock:
            self.require_account(account_id)
            explicit = usernames is not None and group_id is None
            if self.busy():
                raise ValueError('已有维护任务正在执行，可继续查看列表和导出')
            account = dict(self.account() or {})
            archive = self.archive(account)
            config = archive.config()
            if action in ('extract', 'prepare', 'discover', 'update', 'update_all'):
                inspect_directory(account['directory'])
            if action == 'extract' and account['mode'] != 'encrypted':
                raise ValueError('明文库无需提取密钥')
            if action not in ('extract', 'transcribe') and account['mode'] == 'encrypted' and not (
                    config.get('keys_file') and Path(config['keys_file']).is_file()):
                raise ValueError('请先提取密钥或保存有效的密钥文件路径')
            if action in ('update', 'update_all') and not config.get('self_username'):
                raise ValueError('请先查找并保存本人身份，再导入会话')
            if group_id is not None:
                if action not in ('update', 'transcribe'):
                    raise ValueError('此任务不支持项目范围')
                usernames = [r['username'] for r in archive.group_scope(group_id)['members']]
            if action == 'update' and usernames is None:
                usernames = archive.config().get('followed_usernames', [])
            if action == 'update_all' and usernames is None:
                usernames = [r['username'] for r in archive.maintenance_contacts()]
            usernames = list(dict.fromkeys(usernames or []))
            if explicit and account_id is not None:
                archive.require_visible(usernames)
            if action in ('update', 'update_all', 'transcribe'):
                # Hidden followed members keep their membership but never enter maintenance.
                blocked = set(archive.config().get('hidden_usernames', []))
                usernames = [u for u in usernames if u not in blocked]
            if action in ('update', 'update_all', 'transcribe') and not usernames:
                raise ValueError('处理范围为空；请先保存关注会话或明确选择会话')
            if action in ('update', 'update_all', 'transcribe') and set(usernames) - {r['username'] for r in (archive.maintenance_contacts() if action == 'update_all' else archive.directory_contacts())}:
                raise ValueError('范围包含未知会话，请刷新并重新选择')
            if action == 'transcribe' and set(usernames) - {r['username'] for r in archive.contacts()}:
                raise ValueError('所选会话尚未导入；请先导入所选会话')
            if voice_targets is not None:
                if action != 'transcribe':
                    raise ValueError('仅语音转写支持消息选择')
                archive.voice_targets(usernames, voice_targets)
            if action == 'transcribe' and preparation.plan(account, archive, action, usernames, voice_targets)['media']:
                inspect_directory(account['directory'])
                if account['mode'] == 'encrypted' and not (config.get('keys_file') and Path(config['keys_file']).is_file()):
                    raise ValueError('请先提取密钥或保存有效的密钥文件路径')
            preparation.require_capacity(self.preparation_preview(action, usernames, voice_targets))
            task = {'id': uuid.uuid4().hex, 'account_id': account['id'], 'action': action,
                    'status': 'running', 'stage': '准备', 'done': 0, 'total': 0, 'started': now(),
                    'next_step': '', 'reason': '', 'selected_usernames': usernames, 'selected_count': len(usernames),
                    'scope': 'all' if action == 'update_all' else 'selected', 'cancellable': True,
                    'cancel_boundary': '等待当前安全边界', 'publication': '未发布'}
            if voice_targets is not None:
                task['voice_targets'] = voice_targets
            atomic_json(archive.directory / 'task.json', task)
            self.task = task
            self.cancellation = Cancellation(self.lock)
            self.executor.submit(self.perform, account, archive, task, action, usernames)
            return dict(task)

    def perform(self, account, archive, task, action, usernames):
        task_path = archive.directory / 'task.json'
        cancellation = self.cancellation or Cancellation()
        generation = None
        keys = None
        outcome = 'failed'
        final_stage = None
        last_publication = 0.
        def progress(stage, done=0, total=0):
            nonlocal last_publication
            with self.lock:
                current_activity = ''
                if stage.startswith(('读取消息批次（', '导入消息（')):
                    current_activity, stage = stage, '导入消息'
                if stage.startswith('解密与 WAL 合并'):
                    stage = '解密与 WAL 合并'
                if stage in ('提交消息档案', '提交通讯录', '提交密钥'):
                    cancellation.check()
                    task.update(cancellable=False, cancel_boundary='正在提交，等待实际结果')
                elif task.get('cancellable', True):
                    cancellation.check()
                if stage == '完成':
                    if not task.get('cancellable', True):
                        task['publication'] = '已提交'
                    stage = '正在完成当前阶段'
                unit = (task.get('unit', '') if stage == '正在完成当前阶段' else
                        '字节' if stage == '复制快照' else '页' if '页面' in stage else
                        '帧' if stage == '验证加密 WAL 提交边界' else
                        '条语音' if '语音转写' in stage else '个会话' if '导入消息' in stage or '会话元数据' in stage or stage == '发现历史会话' else
                        '个会话' if stage in ('提交消息档案', '复核明文源库签名') else
                        '个数据库' if '稳定快照' in stage or '解密与 WAL' in stage else '')
                task.update(stage=stage, done=done, total=total, unit=unit, activity=current_activity)
                if time.monotonic() - last_publication >= .5:
                    atomic_json(task_path, task)
                    last_publication = time.monotonic()
        def activity(message, *_):
            with self.lock:
                cancellation.check()
                task['activity'] = message
        try:
            config = archive.config()
            file_plan = preparation.plan(account, archive, action, usernames, task.get('voice_targets'))
            preparation.require_capacity(preparation.capacity(file_plan, archive))
            manifest = {}
            if action == 'extract':
                if account['mode'] != 'encrypted':
                    raise ValueError('此目录已是明文数据库，无需提取密钥')
                progress('读取微信进程并验证候选密钥')
                # The existing tool can print identity/key details: capture and do not expose its logs.
                keys = archive.directory / ('keys_' + uuid.uuid4().hex + '.json')
                diagnostic_path = keys.with_suffix('.diagnostic.json')
                try:
                    process = supervised_process.run([str(python_executable()), str(resource_root() / 'wcdb_key_tool_windows.py'),
                                             'extract', '--db-dir', account['directory'], '--diagnostics-output', str(diagnostic_path), '--output', str(keys)],
                                            cancellation=cancellation, timeout=600)
                    diagnostic = {}
                    if diagnostic_path.exists():
                        try:
                            raw = json.loads(diagnostic_path.read_text(encoding='utf-8'))
                            # Only typed counters and known stages cross the subprocess boundary.
                            diagnostic = {k: raw[k] for k in ('databases', 'verified_databases', 'processes',
                                'opened_processes', 'readable_regions', 'readable_bytes', 'read_failures', 'candidate_count', 'system_code', 'process_list_returncode',
                                'skipped_regions', 'skipped_bytes', 'needle_occurrences', 'string_object_refs', 'node_candidates', 'verified_candidates',
                                'missing_contact_databases', 'missing_message_databases', 'missing_other_databases')
                                if type(raw.get(k)) is int}
                            if raw.get('stage') in ('preflight', 'process_list', 'scan', 'validation', 'write_result', 'complete'):
                                diagnostic['stage'] = raw['stage']
                            diagnostic['open_errors'] = [n for n in raw.get('open_errors', []) if type(n) is int]
                            if raw.get('exception_type') in ('OSError', 'PermissionError', 'FileNotFoundError', 'RuntimeError',
                                    'ValueError', 'UnicodeEncodeError', 'UnicodeDecodeError', 'SystemExit'):
                                diagnostic['exception_type'] = raw['exception_type']
                        except (ValueError, TypeError):
                            pass
                    diagnostic['returncode'] = process.returncode
                    code, feedback = extraction_feedback(diagnostic, process.returncode, keys.exists())
                    task.update(diagnostic=diagnostic, diagnostic_code=code, next_step=feedback)
                    if code not in ('complete', 'partial'):
                        raise RuntimeError('密钥提取失败：' + feedback)
                except subprocess.TimeoutExpired:
                    task.update(diagnostic_code='timeout', diagnostic={'stage': 'scan', 'timeout_seconds': 600},
                                next_step='提取超时，请核对目录后重试；此前可用档案仍保留')
                    raise RuntimeError('TimeoutExpired：密钥提取超时（600 秒）') from None
                finally:
                    diagnostic_path.unlink(missing_ok=True)
                    if cancellation.event.is_set():
                        keys.unlink(missing_ok=True)
                progress('提交密钥')
                archive.save_config({'keys_file': str(keys), 'keys_error': ''})
                result = {'keys_saved': True, 'coverage': code, 'next_step': feedback}
            elif action in ('prepare', 'discover', 'update', 'update_all'):
                db_dir = config.get('decrypted_dir')
                manifest = {}
                if account['mode'] == 'encrypted':
                    if not config.get('keys_file'):
                        raise ValueError('请先点击提取密钥，或设置已有的本账号密钥文件')
                    media_needed = file_plan['media']
                    db_dir, manifest = encrypted_sync.sync(account['directory'], archive.directory,
                                                          config['keys_file'], [db_dir, config.get('message_snapshot'), config.get('media_snapshot')], progress,
                                                          contacts_only=False, media=media_needed)
                    generation = db_dir
                if action in ('prepare', 'discover'):
                    published = {}
                    if account['mode'] == 'encrypted':
                        published['keys_error'] = ''
                        snapshot = db_dir
                        if snapshot:
                            published['message_snapshot'] = str(snapshot)
                    count = archive.prepare_directory(db_dir, discover=action == 'discover', settings=published,
                                                      before_publish=lambda: progress('提交通讯录'), progress=progress)
                    result = {'prepared': True, 'databases': len(manifest),
                              'contacts': count, 'next_step': ('通讯录已准备，可选择会话并导入，或继续阅读已归档消息' if config.get('self_username') else
                              '查找并保存本人身份，再选择会话并导入；关注范围可选')}
                else:
                    if not config.get('self_username'):
                        raise ValueError('请先查找并确认自己的内部 username')
                    published = {'decrypted_dir': str(db_dir)}
                    if account['mode'] == 'encrypted':
                        published['keys_error'] = ''
                        published['message_snapshot'] = str(db_dir)
                        if media_needed:
                            published['media_snapshot'] = str(db_dir)
                    result = archive.update(db_dir, progress, settings=published,
                                            signatures=manifest if account['mode'] == 'encrypted' else None,
                                            usernames=usernames)
                    result['decrypted_reused'] = sum(x['reused'] for x in manifest.values())
            elif action == 'transcribe':
                if not usernames:
                    raise ValueError('请先选择聊天')
                voice_plan = archive.voice_plan(usernames, task.get('voice_targets'))
                if voice_plan['total'] == voice_plan['skipped']:
                    result = voice_service.transcribe(archive, config.get('decrypted_dir', ''), usernames, '', progress,
                        targets=task.get('voice_targets'), cancellation=cancellation)
                else:
                    if account['mode'] == 'encrypted':
                        db_dir, manifest = encrypted_sync.sync(account['directory'], archive.directory,
                            config['keys_file'], [config.get('decrypted_dir'), config.get('media_snapshot')], progress, contacts_only=True, media=True)
                        generation = db_dir
                        cancellation.publish(lambda: archive.save_config({'decrypted_dir': str(db_dir), 'media_snapshot': str(db_dir),
                                             'message_snapshot': config.get('message_snapshot') or config.get('decrypted_dir')}))
                        with self.lock:
                            task['publication'] = '快照已发布；语音逐条发布'
                        config['decrypted_dir'] = str(db_dir)
                    online = config.get('asr_provider', 'local') != 'local'
                    voice_service.validate_api(config)
                    progress('检查在线识别与音频工具' if online else '检查本地识别工具')
                    info = voice_service.probe(online=True, cancellation=cancellation) if online else voice_service.probe(cancellation=cancellation)
                    if not online and config.get('asr_model') and Path(config['asr_model']).is_file():
                        info['available'] = bool(info.get('whisper') and info.get('ffmpeg'))
                    if not info['available']:
                        raise voice_service.ToolUnavailable(info.get('reason', '本地识别工具不可用'))
                    model = config.get('asr_model') or next((m for m in info.get('models', []) if Path(m).name == 'small.pt'), '')
                    # Per-message failures are persisted; exports remain available.
                    def recognizer(media, chosen_model):
                        if not info['available']:
                            raise voice_service.ToolUnavailable(info.get('reason', '本地识别工具不可用'))
                        if media.startswith((b'\x02#!SILK_V3', b'#!SILK_V3')) and not info.get('silk'):
                            raise voice_service.ToolUnavailable('缺少 SILK 解码工具 pilk')
                        if online:
                            return voice_service.recognize_online(media, config, cancellation=cancellation, progress=activity)
                        result = voice_service.recognize(media, chosen_model, details=True, cancellation=cancellation)
                        return dict(result, provider='local', model=chosen_model)
                    def voice_published(counts):
                        with self.lock:
                            task.update(publication='逐条发布', result=dict(counts))
                    result = voice_service.transcribe(archive, config['decrypted_dir'], usernames, model, progress, recognizer,
                                                     targets=task.get('voice_targets'), force='voice_targets' in task, cancellation=cancellation,
                                                     on_publish=voice_published)
            else:
                raise ValueError('未知任务')
            if manifest:
                result['preparation'] = {
                    'snapshot_seconds': sum(x.get('snapshot_seconds', 0) for x in manifest.values()),
                    'prepare_seconds': sum(x.get('prepare_seconds', 0) for x in manifest.values()),
                    'snapshot_bytes': sum(x.get('snapshot_bytes', 0) for x in manifest.values()),
                    'materialized_bytes': sum(x.get('source_bytes', 0) for x in manifest.values() if not x['reused'])}
            with self.lock:
                task['publication'] = '已提交' if action != 'transcribe' else '逐条发布'
                if action == 'transcribe' and result['failed']:
                    outcome = 'failed'
                    final_stage = '转写结束，存在失败语音'
                    task.update(stage='正在清理任务资源', result=result,
                                reason=f"{result['failed']} 条语音转写失败，具体原因见下方汇总",
                                next_step='按错误汇总修复工具后重试；成功的转写会自动复用')
                else:
                    outcome = 'success'
                    final_stage = '部分密钥已保存' if task.get('diagnostic_code') == 'partial' else '完成'
                    task.update(stage='正在清理任务资源', result=result, next_step=result.get('next_step', '可查看列表或导出'))
        except TaskCancelled as exc:
            with self.lock:
                outcome = 'cancelled'
                task.update(stage='正在清理任务资源', cancellable=False,
                            next_step='可按原范围重新执行；已发布结果保留')
                if hasattr(exc, 'result'):
                    task.update(result=exc.result, publication='逐条发布')
        except BaseException as exc:
            logging.getLogger(__name__).exception('维护任务 %s 在 %s 失败', action, task['stage'])
            with self.lock:
                if isinstance(exc, CleanupError):
                    task['cleanup_resources'] = [exc.resource]
                if isinstance(exc, encrypted_sync.KeyPreparationError):
                    archive.save_config({'keys_error': str(exc)})
                    task.update(diagnostic_code='keys_invalid', next_step='返回数据设置，重新提取或选择密钥文件，再准备通讯录；旧档案仍可阅读')
                if (isinstance(exc, OSError) and (exc.errno == errno.ENOSPC or getattr(exc, 'winerror', None) == 112)) or (
                        isinstance(exc, sqlite3.Error) and (getattr(exc, 'sqlite_errorcode', None) == 13 or str(exc) == 'database or disk is full')):
                    task.update(diagnostic_code='disk_full', next_step=f'数据或临时文件所在卷空间已耗尽。请释放空间后按原范围重试；未提交档案与配置保持原值，已发布结果保留。数据目录：{archive.directory}')
                # Avoid printing raw SQL values or subprocess output.
                if isinstance(exc, CleanupError):
                    reason = f'任务资源清理失败：{exc}；请重试任务资源清理'
                elif isinstance(exc, OSError):
                    filename = Path(exc.filename2 or exc.filename).name if (exc.filename2 or exc.filename) else '未知文件'
                    code = getattr(exc, 'winerror', None) or exc.errno
                    reason = f'{type(exc).__name__}：文件 {filename} 操作失败（系统错误 {code}：{exc.strerror}）'
                else:
                    reason = f'{type(exc).__name__}：{exc}'
                frame = traceback.extract_tb(exc.__traceback__)[-1]
                task.update(failed_stage=task['stage'], reason=reason,
                            error_location=f'{Path(frame.filename).name}:{frame.lineno} · {frame.name}',
                            next_step=task.get('next_step') or '按失败原因调整目录、身份或工具后重新执行；此前可用档案仍保留')
        finally:
            try:
                config = archive.config()
                if generation and str(generation) not in {config.get('decrypted_dir'), config.get('message_snapshot'), config.get('media_snapshot')}:
                    shutil.rmtree(generation)
                if keys and str(keys) != config.get('keys_file'):
                    keys.unlink(missing_ok=True)
            except OSError as exc:
                outcome = 'failed'
                task.update(reason=f'任务资源清理失败：{exc}', next_step='检查数据目录访问权限后重试；已发布结果保留')
                if generation and Path(generation).exists():
                    task.setdefault('cleanup_resources', []).append({'path': str(generation), 'directory': True})
                if keys and keys.exists() and str(keys) != config.get('keys_file'):
                    task.setdefault('cleanup_resources', []).append({'path': str(keys), 'directory': False})
            with self.lock:
                task.update(status=outcome, finished=now(), cancellable=False)
                task['cancel_boundary'] = ''
                if outcome == 'cancelled':
                    task['stage'] = '已停止'
                elif final_stage:
                    task['stage'] = final_stage
                if task.get('cleanup_resources'):
                    self.cleanup = {'stage': '任务资源清理未完成，请重试', 'type': 'task_resources',
                                    'task_id': task.get('id'), 'account_id': task.get('account_id')}
                try:
                    atomic_json(task_path, task)
                except OSError:
                    logging.getLogger(__name__).exception('任务结束状态无法写入 %s；当前进程仍保留结果', task_path)
