"""Account registry and one background maintenance task; reads stay available."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import json
import logging
from pathlib import Path
import subprocess
import threading
import time
import traceback
import uuid

from archive_store import Archive, now
import encrypted_sync
import voice_service
import wechat_export as we
from runtime_paths import python_executable, resource_root


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
            'message_shards': len(messages),
            'wal_files': sum(Path(str(p) + '-wal').exists() for p in [contact] + messages)}


def identity_candidates(directory, query):
    path = Path(directory) / 'contact' / 'contact.db'
    if not path.is_file():
        raise ValueError('请先解密准备数据，再查找与确认身份')
    with closing(we._connect_ro(str(path))) as c:
        columns = {r[1] for r in c.execute('PRAGMA table_info(contact)')}
        alias = 'alias' if 'alias' in columns else "'' AS alias"
        rows = c.execute(f'SELECT username,{alias},remark,nick_name FROM contact WHERE username=?' +
                         (' OR alias=?' if 'alias' in columns else ''),
                         (query, query) if 'alias' in columns else (query,)).fetchall()
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
        for account in self.registry['accounts']:
            task_file = self.root / account['id'] / 'task.json'
            if task_file.exists():
                task = json.loads(task_file.read_text(encoding='utf-8'))
                if task['status'] == 'running':
                    task.update(status='interrupted', stage='上次任务已中断', reason='服务已重启；之前可用数据仍保留',
                                next_step='重新执行对应操作', finished=now())
                    atomic_json(task_file, task)

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
        return self.task is not None and self.task['status'] == 'running'

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
            self.registry['active'] = account['id']
            atomic_json(self.registry_path, self.registry)
            archive = self.archive()
            if info['mode'] == 'plaintext':
                archive.save_config({'decrypted_dir': info['directory']})
                archive.prepare_directory(info['directory'])
            return account

    def status(self):
        account = self.account()
        config, task = {}, None
        if account:
            archive = self.archive()
            config = archive.config()
        with self.lock:
            if self.task and account and self.task['account_id'] == account['id']:
                task = dict(self.task)
            elif account:
                path = archive.directory / 'task.json'
                if path.exists():
                    task = json.loads(path.read_text(encoding='utf-8'))
        return {'account': account, 'accounts': self.registry['accounts'], 'config': config,
                'task': task, 'wal_validation': '尚未执行真实微信加密 WAL 端到端验证'}

    def submit(self, action, usernames=None, group_id=None):
        with self.lock:
            if self.busy():
                raise ValueError('已有维护任务正在执行，可继续查看列表和导出')
            account = dict(self.account() or {})
            archive = self.archive(account)
            if group_id is not None:
                if action not in ('update', 'transcribe'):
                    raise ValueError('此任务不支持项目范围')
                usernames = [r['username'] for r in archive.group_scope(group_id)['members']]
            if action == 'update' and usernames is None:
                usernames = archive.config().get('followed_usernames', [])
            usernames = list(dict.fromkeys(usernames or []))
            if action in ('update', 'transcribe'):
                # Hidden followed members keep their membership but never enter maintenance.
                blocked = set(archive.config().get('hidden_usernames', []))
                usernames = [u for u in usernames if u not in blocked]
            if action in ('update', 'transcribe') and not usernames:
                raise ValueError('处理范围为空；请先保存关注会话或明确选择会话')
            if action == 'transcribe' and set(usernames) - {r['username'] for r in archive.contacts()}:
                raise ValueError('所选会话尚未导入；请先导入所选会话')
            task = {'id': uuid.uuid4().hex, 'account_id': account['id'], 'action': action,
                    'status': 'running', 'stage': '准备', 'done': 0, 'total': 0, 'started': now(),
                    'next_step': '', 'reason': '', 'selected_usernames': usernames, 'selected_count': len(usernames),
                    'scope': 'all' if action == 'update_all' else 'selected'}
            self.task = task
            atomic_json(archive.directory / 'task.json', task)
            self.executor.submit(self.perform, account, archive, task, action, usernames)
            return dict(task)

    def perform(self, account, archive, task, action, usernames):
        task_path = archive.directory / 'task.json'
        last_publication = 0.
        def progress(stage, done=0, total=0):
            nonlocal last_publication
            with self.lock:
                if action in ('update', 'update_all', 'transcribe'):
                    stage += ' · 全部会话' if action == 'update_all' else f' · 已选 {len(usernames)} 个会话'
                task.update(stage=stage, done=done, total=total)
                if time.monotonic() - last_publication >= .5:
                    atomic_json(task_path, task)
                    last_publication = time.monotonic()
        try:
            config = archive.config()
            manifest = {}
            if action == 'extract':
                if account['mode'] != 'encrypted':
                    raise ValueError('此目录已是明文数据库，无需提取密钥')
                progress('读取微信进程并验证候选密钥')
                # The existing tool can print identity/key details: capture and do not expose its logs.
                keys = archive.directory / ('keys_' + uuid.uuid4().hex + '.json')
                result = subprocess.run([str(python_executable()), str(resource_root() / 'wcdb_key_tool_windows.py'),
                                         'extract', '--db-dir', account['directory'], '--output', str(keys)],
                                        capture_output=True, timeout=600)
                if result.returncode or not keys.exists():
                    raise RuntimeError('密钥提取失败。请确认微信已登录且当前进程有读取权限，必要时以管理员身份启动后重试')
                archive.save_config({'keys_file': str(keys)})
                result = {'keys_saved': True}
            elif action in ('prepare', 'discover', 'update', 'update_all'):
                db_dir = config.get('decrypted_dir')
                manifest = {}
                if account['mode'] == 'encrypted':
                    if not config.get('keys_file'):
                        raise ValueError('请先点击提取密钥，或设置已有的本账号密钥文件')
                    with closing(archive.connect()) as c:
                        media_needed = action in ('update', 'update_all') and bool(c.execute(
                            "SELECT 1 FROM transcripts WHERE status='success' AND (? OR username IN (SELECT value FROM json_each(?))) LIMIT 1",
                            (action == 'update_all', json.dumps(usernames))).fetchone())
                    db_dir, manifest = encrypted_sync.sync(account['directory'], archive.directory,
                                                          config['keys_file'], [db_dir, config.get('message_snapshot'), config.get('media_snapshot')], progress,
                                                          contacts_only=action == 'prepare', media=media_needed)
                if action in ('prepare', 'discover'):
                    archive.save_config({'decrypted_dir': str(db_dir)})
                    if account['mode'] == 'encrypted':
                        snapshot = db_dir if action == 'discover' else config.get('message_snapshot') or config.get('decrypted_dir')
                        if snapshot:
                            archive.save_config({'message_snapshot': str(snapshot)})
                    count = archive.prepare_directory(db_dir, discover=action == 'discover')
                    result = {'prepared': True, 'databases': len(manifest),
                              'contacts': count, 'next_step': '选择并保存关注会话，确认身份后更新关注'}
                else:
                    if not config.get('self_username'):
                        raise ValueError('请先查找并确认自己的内部 username')
                    published = {'decrypted_dir': str(db_dir)}
                    if account['mode'] == 'encrypted':
                        published['message_snapshot'] = str(db_dir)
                        if media_needed:
                            published['media_snapshot'] = str(db_dir)
                    result = archive.update(db_dir, progress, settings=published,
                                            signatures=manifest if account['mode'] == 'encrypted' else None,
                                            usernames=None if action == 'update_all' else usernames)
                    result['decrypted_reused'] = sum(x['reused'] for x in manifest.values())
            elif action == 'transcribe':
                if not usernames:
                    raise ValueError('请先选择聊天')
                if account['mode'] == 'encrypted':
                    db_dir, manifest = encrypted_sync.sync(account['directory'], archive.directory,
                        config['keys_file'], [config.get('decrypted_dir'), config.get('media_snapshot')], progress, contacts_only=True, media=True)
                    archive.save_config({'decrypted_dir': str(db_dir), 'media_snapshot': str(db_dir),
                                         'message_snapshot': config.get('message_snapshot') or config.get('decrypted_dir')})
                    config['decrypted_dir'] = str(db_dir)
                progress('检查本地识别工具')
                info = voice_service.probe()
                if not info['available']:
                    raise voice_service.ToolUnavailable(info.get('reason', '本地识别工具不可用'))
                model = config.get('asr_model') or next((m for m in info.get('models', []) if Path(m).name == 'small.pt'), '')
                # Per-message failures are persisted; exports remain available.
                def recognizer(media, chosen_model):
                    if not info['available']:
                        raise voice_service.ToolUnavailable(info.get('reason', '本地识别工具不可用'))
                    if media.startswith((b'\x02#!SILK_V3', b'#!SILK_V3')) and not info.get('silk'):
                        raise voice_service.ToolUnavailable('缺少 SILK 解码工具 pilk')
                    return voice_service.recognize(media, chosen_model)
                result = voice_service.transcribe(archive, config['decrypted_dir'], usernames, model, progress, recognizer)
            else:
                raise ValueError('未知任务')
            if manifest:
                result['preparation'] = {
                    'snapshot_seconds': sum(x.get('snapshot_seconds', 0) for x in manifest.values()),
                    'prepare_seconds': sum(x.get('prepare_seconds', 0) for x in manifest.values()),
                    'snapshot_bytes': sum(x.get('snapshot_bytes', 0) for x in manifest.values()),
                    'materialized_bytes': sum(x.get('source_bytes', 0) for x in manifest.values() if not x['reused'])}
            with self.lock:
                if action == 'transcribe' and result['failed']:
                    task.update(status='failed', stage='转写结束，存在失败语音', result=result,
                                reason=f"{result['failed']} 条语音转写失败，具体原因见下方汇总", finished=now(),
                                next_step='按错误汇总修复工具后重试；成功的转写会自动复用')
                else:
                    task.update(status='success', stage='完成', result=result, finished=now(), next_step=result.get('next_step', '可查看列表或导出'))
        except BaseException as exc:
            logging.getLogger(__name__).exception('维护任务 %s 在 %s 失败', action, task['stage'])
            with self.lock:
                # Avoid printing raw SQL values or subprocess output.
                if isinstance(exc, OSError):
                    filename = Path(exc.filename2 or exc.filename).name if (exc.filename2 or exc.filename) else '未知文件'
                    code = getattr(exc, 'winerror', None) or exc.errno
                    reason = f'{type(exc).__name__}：文件 {filename} 操作失败（系统错误 {code}：{exc.strerror}）'
                else:
                    reason = f'{type(exc).__name__}：{exc}'
                frame = traceback.extract_tb(exc.__traceback__)[-1]
                task.update(status='failed', failed_stage=task['stage'], reason=reason, finished=now(),
                            error_location=f'{Path(frame.filename).name}:{frame.lineno} · {frame.name}',
                            next_step='按失败原因调整目录、身份或工具后重新执行；此前可用档案仍保留')
        finally:
            with self.lock:
                atomic_json(task_path, task)
