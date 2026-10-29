"""Transactional, account-local message archive. Missing source rows are retained."""
import base64
import datetime
import json
from pathlib import Path
import sqlite3
import time
import itertools
import hashlib
from contextlib import closing

import wechat_export as we


def now():
    return datetime.datetime.now().astimezone().isoformat(timespec='seconds')


def encoded(value):
    if isinstance(value, bytes):
        return {'bytes': base64.b64encode(value).decode('ascii')}
    return value


class Archive:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.path = self.directory / 'archive.sqlite'
        with closing(self.connect()) as c:
            c.executescript('''
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS chats(username TEXT PRIMARY KEY,data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS messages(
                    username TEXT NOT NULL, message_key TEXT NOT NULL, timestamp INTEGER NOT NULL,
                    data TEXT NOT NULL, input TEXT NOT NULL,
                    PRIMARY KEY(username,message_key));
                CREATE INDEX IF NOT EXISTS message_time ON messages(username,timestamp,message_key);
                CREATE TABLE IF NOT EXISTS transcripts(
                    username TEXT NOT NULL,message_key TEXT NOT NULL,input BLOB,
                    status TEXT NOT NULL,text TEXT NOT NULL,reason TEXT NOT NULL,source TEXT,
                    PRIMARY KEY(username,message_key));
                CREATE TABLE IF NOT EXISTS voice_skips(
                    username TEXT NOT NULL,message_key TEXT NOT NULL,
                    PRIMARY KEY(username,message_key));
                CREATE TABLE IF NOT EXISTS locations(
                    username TEXT NOT NULL,shard TEXT NOT NULL,local_id INTEGER NOT NULL,
                    timestamp INTEGER NOT NULL,message_key TEXT NOT NULL,
                    PRIMARY KEY(username,shard,local_id,timestamp));
                CREATE TABLE IF NOT EXISTS current_sources(
                    username TEXT NOT NULL,shard TEXT NOT NULL,local_id INTEGER NOT NULL,
                    timestamp INTEGER NOT NULL,message_key TEXT NOT NULL,
                    PRIMARY KEY(username,shard,local_id,timestamp));
                CREATE INDEX IF NOT EXISTS current_source_key ON current_sources(username,message_key,shard);
                CREATE INDEX IF NOT EXISTS message_order ON messages(username,timestamp,
                    json_extract(data,'$.sort_seq'),json_extract(data,'$.local_id'),message_key);
            ''')

    def connect(self):
        return sqlite3.connect(self.path, timeout=30)

    def config(self):
        with closing(self.connect()) as c:
            return {k: json.loads(v) for k, v in c.execute('SELECT key,value FROM settings')}

    def save_config(self, values):
        with closing(self.connect()) as c, c:
            c.executemany('INSERT OR REPLACE INTO settings VALUES (?,?)',
                          [(k, json.dumps(v, ensure_ascii=False)) for k, v in values.items()])

    def contacts(self, connection=None):
        if connection is None:
            with closing(self.connect()) as c:
                return self.contacts(c)
        return [json.loads(r[0]) for r in connection.execute('SELECT data FROM chats')]

    def directory_contacts(self, include_hidden=False, connection=None):
        if connection is None:
            with closing(self.connect()) as c:
                c.execute('BEGIN')
                return self.directory_contacts(include_hidden, c)
        config = {k: json.loads(v) for k, v in connection.execute('SELECT key,value FROM settings')}
        source = config.get('message_snapshot') or config.get('decrypted_dir')
        sessions = set(self.source_sessions(source)) if source else set()
        if include_hidden:
            sessions.update(config.get('hidden_usernames', []))
        rows = {r['username']: dict(r, total=0, first_timestamp=None, last_timestamp=None,
                preview='', recorded_senders=[], recorded_sender_count=0, imported=False)
                for r in config.get('contact_directory', []) if r['username'] in sessions}
        for u in sessions:
            rows.setdefault(u, dict(username=u, name=u, category=we.classify_username(u),
                total=0, first_timestamp=None, last_timestamp=None, preview='',
                recorded_senders=[], recorded_sender_count=0, imported=False))
        for r in self.contacts(connection):
            rows[r['username']] = dict(rows.get(r['username'], {}), **r, imported=True)
        followed = set(config.get('followed_usernames', []))
        hidden = set(config.get('hidden_usernames', []))
        return [dict(r, followed=u in followed, hidden=u in hidden) for u, r in rows.items()
                if include_hidden or u not in hidden]

    def require_visible(self, usernames):
        if set(usernames) & set(self.config().get('hidden_usernames', [])):
            raise ValueError('范围包含隐藏会话；请先在隐藏会话管理中恢复')

    def maintenance_contacts(self):
        """Preview and execution use the same visible conversation catalog."""
        return self.directory_contacts()

    @staticmethod
    def source_sessions(db_dir, progress=lambda *args: None):
        users = set()
        hashes = set()
        for path in sorted((Path(db_dir) / 'message').glob('message_*.db')):
            progress('发现历史会话', len(users), 0)
            if not we.is_message_shard(path.name):
                continue
            with closing(we._connect_ro(str(path))) as c:
                tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                hashes.update(t[4:] for t in tables if t.startswith('Msg_'))
                if 'Name2Id' in tables:
                    users.update(u for (u,) in c.execute('SELECT user_name FROM Name2Id')
                                 if u and 'Msg_' + hashlib.md5(u.encode()).hexdigest() in tables)
        users.update(r['username'] for r in we.load_contacts(str(db_dir), include_official=True, only_with_msg=False)
                     if hashlib.md5(r['username'].encode()).hexdigest() in hashes)
        return sorted(users)

    def groups(self):
        config = self.config()
        return [{'id': 'followed', 'name': '关注会话', 'usernames': config.get('followed_usernames', [])},
                *config.get('session_groups', [])]

    def group_scope(self, group_id):
        group = next((g for g in self.groups() if g['id'] == group_id), None)
        if group is None:
            raise ValueError('项目不存在，请重新选择')
        available = {r['username']: r for r in self.directory_contacts()}
        members = [available[u] for u in dict.fromkeys(group['usernames']) if u in available]
        return {'id': group_id, 'name': group['name'], 'members': members,
                'imported': [r for r in members if r['imported']],
                'pending': [r for r in members if not r['imported']]}

    def hidden_preview(self, usernames, purge=False):
        with closing(self.connect()) as c:
            c.execute('BEGIN')
            known = {r['username']: r for r in self.directory_contacts(True, c)}
            users = list(dict.fromkeys(usernames))
            if not users or set(users) - known.keys():
                raise ValueError('请选择有效会话')
            counts = {'messages': 0, 'transcripts': 0, 'successful_transcripts': 0}
            for u in users:
                counts['messages'] += c.execute('SELECT count(*) FROM messages WHERE username=?', (u,)).fetchone()[0]
                total, success = c.execute("SELECT count(*),coalesce(sum(status='success'),0) FROM transcripts WHERE username=?", (u,)).fetchone()
                counts['transcripts'] += total
                counts['successful_transcripts'] += success
            return dict(usernames=users, members=[known[u] for u in users], purge=purge, counts=counts)

    def reclaim_space(self):
        with closing(self.connect()) as c:
            try:
                c.execute('VACUUM')
                reclaimed = c.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()[0] == 0
                result = dict(space_reclaimed=reclaimed, warning='' if reclaimed else '档案已清理，空间回收未完成；请稍后重试空间回收。')
            except sqlite3.Error as exc:
                result = dict(space_reclaimed=False, warning=f'档案已清理，空间回收未完成：{exc}；可稍后重试空间回收。')
        self.save_config({'space_reclaim_pending': not result['space_reclaimed']})
        return result

    def set_hidden(self, usernames, hidden=True, purge=False, progress=None):
        config = self.config()
        known = {r['username']: r for r in self.directory_contacts(include_hidden=True)}
        if not usernames or set(usernames) - known.keys():
            raise ValueError('请选择有效会话')
        blocked = set(config.get('hidden_usernames', []))
        blocked = blocked | set(usernames) if hidden else blocked - set(usernames)
        values = {'hidden_usernames': sorted(blocked)}
        # Keep names and membership when an archive-only historical chat is purged.
        directory = {r['username']: r for r in config.get('contact_directory', [])}
        for u in usernames:
            directory.setdefault(u, {k: known[u].get(k, '') for k in
                                     ('username', 'name', 'category', 'alias', 'remark', 'nick_name')})
        values['contact_directory'] = list(directory.values())
        if config.get('view_state', {}).get('active') in blocked:
            values['view_state'] = {}
        deleted = 0
        if progress:
            progress('正在提交档案清理' if purge else '正在保存隐藏状态')
        with closing(self.connect()) as c:
            with c:
                if hidden and purge:
                    for index, u in enumerate(usernames):
                        if progress:
                            progress(f'正在清理会话 {index + 1} / {len(usernames)}（事务尚未提交）')
                        for table in ('messages', 'transcripts', 'voice_skips', 'locations', 'current_sources', 'chats'):
                            count = c.execute(f'DELETE FROM {table} WHERE username=?', (u,)).rowcount
                            if table == 'messages':
                                deleted += count
                    values['conversation_baselines'] = {u: b for u, b in config.get('conversation_baselines', {}).items()
                                                        if u not in usernames}
                c.executemany('INSERT OR REPLACE INTO settings VALUES (?,?)',
                              [(k, json.dumps(v, ensure_ascii=False)) for k, v in values.items()])
        result = {'saved': True, 'deleted_messages': deleted, 'space_reclaimed': False}
        if hidden and purge:
            if progress:
                progress('档案已清理，正在回收空间')
            result.update(self.reclaim_space())
        return result

    def prepare_directory(self, db_dir, discover=False, settings=None, before_publish=lambda: None, progress=lambda *args: None):
        rows = {r['username']: r for r in self.config().get('contact_directory', [])}
        rows.update({r['username']: r for r in we.load_contacts(str(db_dir), include_official=True, only_with_msg=False)})
        if discover:
            for u in self.source_sessions(db_dir, progress):
                rows.setdefault(u, {'username': u, 'name': u, 'category': we.classify_username(u)})
        values = dict(settings or {})
        values.update({'contact_directory': list(rows.values()), 'decrypted_dir': str(db_dir)})
        before_publish()
        self.save_config(values)
        return len(rows)

    @staticmethod
    def key(message):
        # Server ids identify the same message across shards; unsent/local rows stay shard scoped.
        if message.get('server_id'):
            return 'server:' + str(message['server_id'])
        return f"local:{message['source_shard']}:{message['local_id']}:{message['timestamp']}"

    def messages(self, username, connection=None, limit=None, start=None, end=None, offset=0):
        if connection is None:
            with closing(self.connect()) as c:
                return self.messages(username, c, limit, start, end, offset)
        result = []
        for key, data, status, text, reason, source in connection.execute('''
            SELECT m.message_key,m.data,CASE WHEN EXISTS(SELECT 1 FROM voice_skips s WHERE s.username=m.username AND s.message_key=m.message_key) THEN 'skipped' ELSE t.status END,t.text,t.reason,t.source FROM messages m
            LEFT JOIN transcripts t ON t.username=m.username AND t.message_key=m.message_key
            WHERE m.username=? AND (? IS NULL OR m.timestamp>=?) AND (? IS NULL OR m.timestamp<=?)
            ORDER BY m.timestamp DESC,json_extract(m.data,'$.sort_seq') DESC,
            json_extract(m.data,'$.local_id') DESC,m.message_key DESC''' +
            (' LIMIT ? OFFSET ?' if limit is not None else ''),
            (username, start, start, end, end) + ((limit, offset) if limit is not None else ())):
            message = we.normalize_system_sender(json.loads(data))
            message['message_id'] = key
            if (message['type'] & 0xffffffff) == 34:
                message['transcription'] = {'status': status or 'not_requested', 'reason': reason or '',
                                            'media_source': json.loads(source) if source else None}
                if status == 'success':
                    message['content'] = text
            result.append(message)
        return list(reversed(result))

    def message_count(self, username, start=None, end=None, connection=None):
        if connection is None:
            with closing(self.connect()) as c:
                return self.message_count(username, start, end, c)
        return connection.execute('''SELECT count(*) FROM messages WHERE username=?
            AND (? IS NULL OR timestamp>=?) AND (? IS NULL OR timestamp<=?)''',
            (username, start, start, end, end)).fetchone()[0]

    @staticmethod
    def search_condition(username, q='', start=None, end=None, day=None, include_transcripts=True):
        if start is not None and end is not None and start > end:
            raise ValueError('开始日期不能晚于结束日期')
        clauses = ['m.username=?']
        params = [username]
        for bound, operator in ((start, '>='), (end, '<=')):
            if bound is not None:
                clauses.append('m.timestamp' + operator + '?')
                params.append(bound)
        if day:
            try:
                parsed = datetime.date.fromisoformat(day)
            except ValueError:
                raise ValueError('日期必须为 YYYY-MM-DD')
            if parsed.isoformat() != day:
                raise ValueError('日期必须为 YYYY-MM-DD')
            clauses.append("date(m.timestamp,'unixepoch','localtime')=?")
            params.append(day)
        if q:
            body = "instr(lower(coalesce(json_extract(m.data,'$.content'),'')),lower(?))>0"
            params.append(q)
            if include_transcripts:
                body += " OR (t.status='success' AND (json_extract(m.data,'$.type') & 4294967295)=34 AND instr(lower(t.text),lower(?))>0)"
                params.append(q)
            clauses.append('(' + body + ')')
        return ' AND '.join(clauses), params

    def search(self, username, q='', start=None, end=None, day=None, include_transcripts=True,
               offset=0, limit=50, connection=None):
        if offset < 0 or (limit is not None and not 1 <= limit <= 500):
            raise ValueError('分页位置不能小于零，每页数量须为 1 至 500')
        if connection is None:
            with closing(self.connect()) as c:
                c.execute('BEGIN')
                return self.search(username, q, start, end, day, include_transcripts, offset, limit, c)
        condition, params = self.search_condition(username, q, start, end, day, include_transcripts)
        source = ''' FROM messages m LEFT JOIN transcripts t
            ON t.username=m.username AND t.message_key=m.message_key WHERE ''' + condition
        daily = [{'day': d, 'count': n} for d, n in connection.execute(
            "SELECT date(m.timestamp,'unixepoch','localtime') AS day,count(*)" + source + ' GROUP BY day ORDER BY day', params)]
        total = sum(bucket['count'] for bucket in daily)
        rows = connection.execute("SELECT m.message_key,m.data,CASE WHEN EXISTS(SELECT 1 FROM voice_skips s WHERE s.username=m.username AND s.message_key=m.message_key) THEN 'skipped' ELSE t.status END,t.text,t.reason,t.source" + source + '''
            ORDER BY m.timestamp DESC,json_extract(m.data,'$.sort_seq') DESC,
            json_extract(m.data,'$.local_id') DESC,m.message_key DESC''' +
            (' LIMIT ? OFFSET ?' if limit is not None else ''), params + ([limit, offset] if limit is not None else []))
        messages = []
        for key, data, status, text, reason, media_source in rows:
            message = we.normalize_system_sender(json.loads(data))
            message['message_id'] = key
            original = message.get('content') or ''
            transcript = text if status == 'success' and (message['type'] & 0xffffffff) == 34 else ''
            if (message['type'] & 0xffffffff) == 34:
                message['transcription'] = {'status': status or 'not_requested', 'reason': reason or '',
                    'media_source': json.loads(media_source) if media_source else None}
                if status == 'success':
                    message['content'] = text
            needle = q.lower()
            sources = []
            if q and needle in original.lower():
                sources.append('content')
            if q and include_transcripts and needle in transcript.lower():
                sources.append('transcript')
            snippet_text = transcript if 'transcript' in sources else original
            position = max(0, snippet_text.lower().find(needle) - 40) if q else 0
            message.update(search_sources=sources, snippet=snippet_text[position:position + 160])
            messages.append(message)
        return {'messages': list(reversed(messages)), 'total': total, 'daily': daily,
                'offset': offset, 'limit': limit,
                'query': dict(username=username, q=q, start=start, end=end, day=day,
                              include_transcripts=include_transcripts)}

    def context(self, username, message_id, before=10, after=10):
        if not 0 <= before <= 100 or not 0 <= after <= 100:
            raise ValueError('上下文数量须为 0 至 100')
        with closing(self.connect()) as c:
            c.execute('BEGIN')
            rows = c.execute('''SELECT message_key FROM messages WHERE username=? ORDER BY timestamp,
                json_extract(data,'$.sort_seq'),json_extract(data,'$.local_id'),message_key''', (username,)).fetchall()
            keys = [r[0] for r in rows]
            if message_id not in keys:
                raise ValueError('消息不存在于当前会话')
            target = keys.index(message_id)
            first, last = max(0, target - before), min(len(keys), target + after + 1)
            messages = self.messages(username, c, limit=last-first, offset=len(keys)-last)
            for message in messages:
                message['is_target'] = message['message_id'] == message_id
            return {'messages': messages, 'target_id': message_id, 'target_index': target-first}

    def metadata(self, c, username, contact, reuse=False):
        if reuse:
            row = c.execute('SELECT data FROM chats WHERE username=?', (username,)).fetchone()
            if row:
                previous = json.loads(row[0])
                if all(previous.get(k) == v for k, v in contact.items()):
                    return
                previous.update(contact)
                data = json.dumps(previous, ensure_ascii=False)
                c.execute('UPDATE chats SET data=? WHERE username=? AND data!=?', (data, username, data))
                return
        total, first, last = c.execute('SELECT count(*),min(timestamp),max(timestamp) FROM messages WHERE username=?', (username,)).fetchone()
        latest = self.messages(username, c, limit=1)
        senders = {}
        if contact['category'] == 'group':
            # Project only sender fields; retain first occurrence order and latest display name.
            for user, shard, sender_id, name in c.execute('''
                SELECT json_extract(data,'$.sender_username'),json_extract(data,'$.source_shard'),
                    json_extract(data,'$.sender_id'),json_extract(data,'$.sender')
                FROM messages WHERE username=? AND (json_extract(data,'$.type') & 4294967295) != 10000
                ORDER BY timestamp,json_extract(data,'$.sort_seq'),
                    json_extract(data,'$.local_id'),message_key''', (username,)):
                key = user or f'{shard}:{sender_id}'
                senders[key] = {'username': user, 'name': name}
        contact = dict(contact)
        contact.update(conversation_id=username, total=total,
                       first_timestamp=first, last_timestamp=last,
                       last_type=latest[-1]['type_name'] if latest else '',
                       preview=(latest[-1]['content'] or f"[{latest[-1]['type_name']}]")[:160] if latest else '',
                       recorded_senders=list(senders.values()), recorded_sender_count=len(senders))
        data = json.dumps(contact, ensure_ascii=False)
        c.execute('INSERT INTO chats VALUES (?,?) ON CONFLICT(username) DO UPDATE SET data=excluded.data WHERE chats.data!=excluded.data', (username, data))

    def update(self, db_dir, progress=lambda *args: None, settings=None, signatures=None, usernames=None):
        started = time.perf_counter()
        config = self.config()
        metrics = {'read_parse_seconds': 0., 'compare_write_seconds': 0., 'metadata_seconds': 0.,
                   'commit_seconds': 0., 'scanned_messages': 0, 'parsed_messages': 0, 'location_writes': 0,
                   'overlap_rescanned_chats': 0, 'temp_changes': 0,
                   'source_state_seconds': 0., 'display_refresh_seconds': 0., 'media_seconds': 0.}
        if usernames is not None:
            usernames = frozenset(usernames)
            if not usernames:
                metrics.update(signature_seconds=0., total_seconds=time.perf_counter()-started,
                               selected_chats=0, scanned_shards=0, skipped_shards=0,
                               sqlite_changes_including_temp=0, durable_changes=0)
                progress('完成（范围为空）', 0, 0)
                return dict(added=0, changed=0, reused=0, performance=metrics)
        plaintext = signatures is None
        include_media = False
        if plaintext and any((Path(db_dir) / 'message').glob('media_*.db')):
            with closing(self.connect()) as c:
                include_media = bool(c.execute(
                    "SELECT 1 FROM transcripts WHERE status='success' AND (? OR username IN (SELECT value FROM json_each(?))) LIMIT 1",
                    (usernames is None, json.dumps(sorted(usernames or ())))).fetchone())
        progress('核对导入签名', 0, 0)
        phase = time.perf_counter()
        if plaintext:
            signatures = self._source_signatures(db_dir, include_media, progress)
        metrics['signature_seconds'] = time.perf_counter() - phase
        with we.MessageReader(db_dir, config.get('self_username')) as reader:
            return self._update(reader, config, progress, settings, signatures, metrics, started, plaintext,
                                usernames, include_media)

    @staticmethod
    def _source_signatures(db_dir, include_media=True, progress=lambda *args: None):
        from encrypted_sync import file_digest, stable_files
        db_dir = Path(db_dir)
        paths = [db_dir / 'contact' / 'contact.db'] + sorted((db_dir / 'message').glob('*.db'))
        paths = [p for p in paths if p.name == 'contact.db' or we.is_message_shard(p.name) or (include_media and p.name.startswith('media_'))]
        files = [p for db in paths for p in (db, Path(str(db) + '-wal')) if p.exists()]
        # Pin only while fingerprinting, rather than blocking a live plaintext writer
        # for the whole import. A changed source before commit causes rollback.
        with stable_files(files):
            return {p.relative_to(db_dir).as_posix(): {'signature': [file_digest(p, progress),
                file_digest(Path(str(p) + '-wal'), progress) if Path(str(p) + '-wal').exists() and Path(str(p) + '-wal').stat().st_size else None]}
                for p in paths}

    def _update(self, reader, config, progress, settings, signatures, metrics, started, plaintext, selected, include_media):
        db_dir = reader.db_dir
        # Encrypted signatures are supplied only by sync(), never inferred from a prepare generation.
        signatures = {k.replace('\\', '/'): v['signature'] for k, v in signatures.items()}
        context = {'my_name': config.get('my_name', '我'), 'self_username': config.get('self_username'),
                   'names': reader.names}
        contacts = we.load_contacts(str(db_dir), include_official=True, only_with_msg=False)
        contact_details = {x['username']: x for x in contacts}
        # Include sessions present only in Name2Id (deleted contacts and historical groups).
        hashes = {name[4:] for _, tables, _, _ in reader.shards.values() for name in tables if name.startswith('Msg_')}
        contacts = [x for x in contacts if hashlib.md5(x['username'].encode()).hexdigest() in hashes]
        known = {x['username'] for x in contacts}
        for _, _, users, _ in reader.shards.values():
            for username in users.values():
                if username and username not in known and hashlib.md5(username.encode()).hexdigest() in hashes:
                    known.add(username)
                    contacts.append({'username': username, 'name': reader.names.get(username, username),
                                     'category': we.classify_username(username)})
        for contact in self.contacts():
            contact_details.setdefault(contact['username'], contact)
            if contact['username'] not in known:
                contacts.append(contact_details.get(contact['username'], contact))
                known.add(contact['username'])
        if selected is not None:
            contacts = [contact_details.get(u, {'username': u, 'name': reader.names.get(u, u),
                        'category': we.classify_username(u)}) for u in sorted(selected)]
        hidden = set(config.get('hidden_usernames', []))
        contacts = [r for r in contacts if r['username'] not in hidden]
        baselines = dict(config.get('conversation_baselines', {}))
        metrics['selected_chats'] = len(contacts)
        scanned_shards, skipped_shards = set(), set()
        counts = {'added': 0, 'changed': 0, 'reused': 0}
        with closing(self.connect()) as c, c:
            c.execute('BEGIN')
            changes_before = c.total_changes
            c.execute('PRAGMA temp_store=FILE')
            c.execute('CREATE TEMP TABLE import_sources(shard TEXT,local_id INTEGER,timestamp INTEGER,message_key TEXT,PRIMARY KEY(shard,local_id,timestamp))')
            c.execute('CREATE INDEX temp.import_source_key ON import_sources(message_key)')
            for index, contact in enumerate(contacts):
                username = contact['username']
                message_changes_before = counts['added'] + counts['changed']
                previous = baselines.get(username, config.get('import_baseline', {}))
                # A legacy full baseline is reliable only for an already archived conversation.
                if username not in baselines and not c.execute('SELECT 1 FROM chats WHERE username=?', (username,)).fetchone():
                    previous = {}
                old_context = previous.get('context', {})
                refresh = any(old_context.get(k) != context.get(k) for k in ('my_name', 'self_username'))
                changed_names = {u for u in old_context.get('names', {})
                                 if old_context.get('names', {}).get(u) != reader.names.get(u)}
                sender_mappings = {s: dict(users) for s, users in previous.get('sender_mappings', {}).items()}
                mapping_changes = {f'{s}:{sender_id}' for s, users in sender_mappings.items() if s in reader.shards
                                   for sender_id, user in users.items()
                                   if reader.shards[s][2].get(None if sender_id == 'None' else int(sender_id)) != user}
                name_refresh = username in changed_names or bool(changed_names and c.execute(
                    "SELECT 1 FROM messages WHERE username=? AND json_extract(data,'$.sender_username') IN (SELECT value FROM json_each(?)) LIMIT 1",
                    (username, json.dumps(sorted(changed_names)))).fetchone())
                identity_changed = any(previous.get('identities', {}).get(name) != list(state[3]) for name, state in reader.shards.items())
                changed = {name for name, state in reader.shards.items()
                           if previous.get('signatures', {}).get('message/' + name) != signatures.get('message/' + name)
                           or previous.get('identities', {}).get(name) != list(state[3])}
                skipped = set(reader.shards) - changed
                scanned_shards.update(changed)
                skipped_shards.update(skipped)
                removed = set(previous.get('identities', {})) - set(reader.shards)
                media_changed = {k: v for k, v in signatures.items() if k.startswith('message/media_')} != {
                    k: v for k, v in previous.get('signatures', {}).items() if k.startswith('message/media_')}
                progress('导入消息', index, len(contacts))
                metrics['temp_changes'] += c.execute('DELETE FROM import_sources').rowcount
                chat_changed, chat_skipped = changed, skipped
                if (changed or removed) and skipped:
                    params = ','.join('?' for _ in changed | removed)
                    # Re-elect the canonical source when its shard changes and another shard
                    # still carries that message, including deletion of the old winner.
                    overlap = c.execute(f'''SELECT 1 FROM messages m JOIN current_sources s
                        ON s.username=m.username AND s.message_key=m.message_key
                        WHERE m.username=? AND json_extract(m.data,'$.source_shard') IN ({params})
                        AND s.shard!=json_extract(m.data,'$.source_shard') LIMIT 1''', (username, *(changed | removed))).fetchone()
                    if overlap:
                        chat_changed, chat_skipped = set(reader.shards), set()
                        metrics['overlap_rescanned_chats'] += 1
                scanned_shards.update(chat_changed)
                if chat_skipped:
                    params = ','.join('?' for _ in chat_skipped)
                    counts['reused'] += c.execute(f'SELECT count(*) FROM current_sources WHERE username=? AND shard IN ({params})', (username, *chat_skipped)).fetchone()[0]
                messages = reader.iter_messages(username, config.get('my_name', '我'), contact['name'], chat_changed, parse=False)
                processed, last_progress = 0, time.monotonic()
                iterator = iter(messages)
                while True:
                    progress(f'读取消息批次（会话 {index + 1}/{len(contacts)}）', index, len(contacts))
                    phase = time.perf_counter()
                    batch = list(itertools.islice(iterator, 256))
                    metrics['read_parse_seconds'] += time.perf_counter() - phase
                    if not batch:
                        break
                    for shard, sender_id in {(m['source_shard'], m['sender_id']) for m in batch}:
                        sender_mappings.setdefault(shard, {})[str(sender_id)] = reader.shards[shard][2].get(sender_id)
                    phase = time.perf_counter()
                    self._import_batch(c, db_dir, username, batch, counts, metrics, chat_skipped)
                    metrics['compare_write_seconds'] += time.perf_counter() - phase
                    processed += len(batch)
                    metrics['scanned_messages'] += len(batch)
                    if time.monotonic() - last_progress >= .5:
                        progress(f'导入消息（会话 {index + 1}/{len(contacts)}，已处理 {processed:,} 条；复用 {counts["reused"]:,}，新增 {counts["added"]:,}，变化 {counts["changed"]:,}）', index, len(contacts))
                        last_progress = time.monotonic()
                phase = time.perf_counter()
                for shard in chat_changed | removed:
                    c.execute('''DELETE FROM current_sources WHERE username=? AND shard=? AND NOT EXISTS (
                        SELECT 1 FROM import_sources s WHERE s.shard=current_sources.shard
                        AND s.local_id=current_sources.local_id AND s.timestamp=current_sources.timestamp)''', (username, shard))
                c.execute('''INSERT INTO current_sources SELECT ?,shard,local_id,timestamp,message_key FROM import_sources WHERE true
                    ON CONFLICT(username,shard,local_id,timestamp) DO UPDATE SET message_key=excluded.message_key
                    WHERE current_sources.message_key!=excluded.message_key''', (username,))
                metrics['source_state_seconds'] += time.perf_counter() - phase
                phase = time.perf_counter()
                legacy_mapping_refresh = bool(previous and changed and not previous.get('sender_mappings'))
                if config.get('last_success') and (refresh or identity_changed or name_refresh or mapping_changes or legacy_mapping_refresh):
                    restrict = not (refresh or identity_changed or legacy_mapping_refresh or
                                    (username in changed_names and contact['category'] != 'group'))
                    self._refresh_senders(c, username, contact, reader, config, counts,
                                          changed_names if restrict else None, mapping_changes, sender_mappings)
                metrics['display_refresh_seconds'] += time.perf_counter() - phase
                phase = time.perf_counter()
                if media_changed:
                    from voice_service import find_media
                    for key, data, old_media in c.execute('SELECT m.message_key,m.data,t.input FROM messages m JOIN transcripts t ON t.username=m.username AND t.message_key=m.message_key WHERE m.username=? AND t.status=?', (username, 'success')):
                        m = json.loads(data)
                        if (m['type'] & 0xffffffff) == 34:
                            media, _ = find_media(db_dir, username, m)
                            if media is not None and media != old_media:
                                c.execute('DELETE FROM transcripts WHERE username=? AND message_key=?', (username, key))
                metrics['media_seconds'] += time.perf_counter() - phase
                phase = time.perf_counter()
                progress('计算会话元数据', index, len(contacts))
                affected = counts['added'] + counts['changed'] != message_changes_before
                self.metadata(c, username, contact, reuse=not affected and not media_changed)
                metrics['metadata_seconds'] += time.perf_counter() - phase
                metadata = json.loads(c.execute('SELECT data FROM chats WHERE username=?', (username,)).fetchone()[0])
                relevant_names = {username, config.get('self_username')} | {
                    s.get('username') for s in metadata.get('recorded_senders', [])}
                chat_context = {**context, 'names': {u: reader.names.get(u) for u in sorted(u for u in relevant_names if u)}}
                baselines[username] = {'signatures': signatures, 'context': chat_context,
                                      'sender_mappings': sender_mappings,
                                      'identities': {name: list(state[3]) for name, state in reader.shards.items()}}
            if plaintext:
                phase = time.perf_counter()
                progress('复核明文源库签名', len(contacts), len(contacts))
                final_signatures = {k: v['signature'] for k, v in self._source_signatures(db_dir, include_media, progress).items()}
                metrics['signature_seconds'] += time.perf_counter() - phase
                if final_signatures != signatures:
                    raise RuntimeError('明文源库在导入期间发生变化，未发布本次导入；请稳定源库后重试')
            baseline = {'signatures': signatures, 'context': context,
                        'identities': {name: list(state[3]) for name, state in reader.shards.items()}}
            if selected is None:
                c.execute('INSERT INTO settings VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value WHERE settings.value!=excluded.value', ('import_baseline', json.dumps(baseline, ensure_ascii=False)))
            c.execute('INSERT INTO settings VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value WHERE settings.value!=excluded.value', ('conversation_baselines', json.dumps(baselines, ensure_ascii=False)))
            c.execute('INSERT OR REPLACE INTO settings VALUES (?,?)', ('last_success', json.dumps(now())))
            for key, value in (settings or {}).items():
                c.execute('INSERT OR REPLACE INTO settings VALUES (?,?)', (key, json.dumps(value)))
            metrics['sqlite_changes_including_temp'] = c.total_changes - changes_before
            metrics['durable_changes'] = metrics['sqlite_changes_including_temp'] - metrics['temp_changes']
            progress('提交消息档案', len(contacts), len(contacts))
            phase = time.perf_counter()
        metrics['commit_seconds'] = time.perf_counter() - phase
        metrics['total_seconds'] = time.perf_counter() - started
        metrics['scanned_shards'] = len(scanned_shards)
        metrics['skipped_shards'] = len(skipped_shards - scanned_shards)
        progress('完成', len(contacts), len(contacts))
        return dict(counts, performance=metrics)

    def _refresh_senders(self, c, username, contact, reader, config, counts, changed_users=None, changed_ids=(), mappings=None):
        # Retained history also follows changed display names and explicit identity.
        where, parameters = '', (username,)
        if changed_users is not None:
            where = " AND (json_extract(data,'$.sender_username') IN (SELECT value FROM json_each(?)) OR json_extract(data,'$.source_shard') || ':' || json_extract(data,'$.sender_id') IN (SELECT value FROM json_each(?)))"
            parameters += (json.dumps(sorted(changed_users)), json.dumps(sorted(changed_ids)))
        cursor = c.execute('SELECT message_key,data FROM messages WHERE username=?' + where, parameters)
        while rows := cursor.fetchmany(256):
            writes = []
            for key, data in rows:
                m = json.loads(data)
                state = reader.shards.get(m['source_shard'])
                if state:
                    _, _, users, (self_id, basis) = state
                    if mappings is not None:
                        mappings.setdefault(m['source_shard'], {})[str(m['sender_id'])] = users.get(m['sender_id'])
                    user = config.get('self_username') if m['sender_id'] == self_id and config.get('self_username') else users.get(m['sender_id'])
                    own = m['sender_id'] == self_id
                    m['identity_basis'] = basis
                else:
                    user = m.get('sender_username')
                    own = bool(user and user == config.get('self_username'))
                m['sender_username'] = user
                m['sender'] = config.get('my_name', '我') if own else (
                    reader.names.get(user) or user or f"未知发送者#{m['sender_id']}"
                    if contact['category'] == 'group' else reader.names.get(username, contact['name']))
                we.normalize_system_sender(m)
                updated = json.dumps(m, ensure_ascii=False, sort_keys=True)
                if updated != data:
                    writes.append((updated, username, key))
            c.executemany('UPDATE messages SET data=? WHERE username=? AND message_key=?', writes)
            counts['changed'] += len(writes)

    def _import_batch(self, c, db_dir, username, batch, counts, metrics, skipped):
        keys = {self.key(m) for m in batch}
        values = ','.join('(?,?,?)' for _ in batch)
        locations = {(s, local, stamp): key for s, local, stamp, key in c.execute(
            f'WITH wanted(shard,local_id,timestamp) AS (VALUES {values}) SELECT l.shard,l.local_id,l.timestamp,l.message_key FROM locations l JOIN wanted w USING(shard,local_id,timestamp) WHERE l.username=?',
            tuple(v for m in batch for v in (m['source_shard'], m['local_id'], m['timestamp'])) + (username,))}
        keys.update(locations.values())
        placeholders = ','.join('?' for _ in keys)
        old_messages = {k: (data, raw) for k, data, raw in c.execute(f'SELECT message_key,data,input FROM messages WHERE username=? AND message_key IN ({placeholders})', (username, *keys))}
        transcripts = {k: (raw, status) for k, raw, status in c.execute(f'SELECT message_key,input,status FROM transcripts WHERE username=? AND message_key IN ({placeholders})', (username, *keys))}
        seen = dict(c.execute(f'SELECT DISTINCT m.message_key,m.input FROM import_sources s JOIN messages m ON m.message_key=s.message_key WHERE m.username=? AND s.message_key IN ({placeholders})', (username, *keys)))
        if skipped:
            shard_params = ','.join('?' for _ in skipped)
            seen.update(c.execute(f'''SELECT DISTINCT m.message_key,m.input FROM messages m JOIN current_sources s
                ON s.username=m.username AND s.message_key=m.message_key
                WHERE m.username=? AND m.message_key IN ({placeholders}) AND s.shard IN ({shard_params})''',
                (username, *keys, *skipped)))
        location_writes, source_writes, message_writes = [], [], []
        def flush():
            c.executemany('INSERT OR REPLACE INTO locations VALUES (?,?,?,?,?)', location_writes)
            before = c.total_changes
            c.executemany('INSERT OR REPLACE INTO import_sources VALUES (?,?,?,?)', source_writes)
            metrics['temp_changes'] += c.total_changes - before
            c.executemany('INSERT OR REPLACE INTO messages VALUES (?,?,?,?,?)', message_writes)
            location_writes.clear()
            source_writes.clear()
            message_writes.clear()
        for m in batch:
            key = self.key(m)
            location = (username, m['source_shard'], m['local_id'], m['timestamp'])
            existing_key = locations.get(location[1:])
            existing_location = (existing_key,) if existing_key else None
            if existing_location and existing_location[0] != key:
                old_key = existing_location[0]
                if not m.get('server_id') and old_key.startswith('server:'):
                    key = old_key
                elif old_key.startswith('local:') and key.startswith('server:'):
                    flush()
                    if key not in old_messages:
                        c.execute('UPDATE messages SET message_key=? WHERE username=? AND message_key=?', (key, username, old_key))
                        c.execute('UPDATE transcripts SET message_key=? WHERE username=? AND message_key=?', (key, username, old_key))
                        if old_key in old_messages:
                            old_messages[key] = old_messages.pop(old_key)
                        if old_key in transcripts:
                            transcripts[key] = transcripts.pop(old_key)
                    else:
                        c.execute('''INSERT OR IGNORE INTO transcripts
                            SELECT username,?,input,status,text,reason,source FROM transcripts
                            WHERE username=? AND message_key=? AND EXISTS(SELECT 1 FROM voice_skips s
                                WHERE s.username=transcripts.username AND s.message_key=transcripts.message_key)''',
                            (key, username, old_key))
                        c.execute('DELETE FROM messages WHERE username=? AND message_key=?', (username, old_key))
                        c.execute('DELETE FROM transcripts WHERE username=? AND message_key=?', (username, old_key))
                    c.execute('INSERT OR IGNORE INTO voice_skips SELECT username,? FROM voice_skips WHERE username=? AND message_key=?', (key, username, old_key))
                    c.execute('DELETE FROM voice_skips WHERE username=? AND message_key=?', (username, old_key))
                    c.execute("DELETE FROM voice_skips WHERE username=? AND message_key=? AND EXISTS(SELECT 1 FROM transcripts t WHERE t.username=voice_skips.username AND t.message_key=voice_skips.message_key AND t.status='success')", (username, key))
                    c.execute('UPDATE locations SET message_key=? WHERE username=? AND message_key=?', (key, username, old_key))
                    c.execute('UPDATE current_sources SET message_key=? WHERE username=? AND message_key=?', (key, username, old_key))
                    before = c.total_changes
                    c.execute('UPDATE import_sources SET message_key=? WHERE message_key=?', (key, old_key))
                    metrics['temp_changes'] += c.total_changes - before
            if existing_key != key:
                location_writes.append((*location, key))
                metrics['location_writes'] += 1
            locations[location[1:]] = key
            source_writes.append((*location[1:], key))
            raw = m.pop('_raw_content', None)
            source_input = json.dumps([encoded(raw), m['type'], m.get('status')], ensure_ascii=False)
            if key in seen:
                if seen[key] != source_input:
                    raise RuntimeError('同一会话 server_id 在多个分片中存在冲突内容，未发布本次导入')
                # A skipped shard may own the earlier canonical row. Preserve that winner.
                old = old_messages.get(key)
                canonical = json.loads(old[0]) if old else None
                if canonical and canonical['source_shard'] in skipped and (
                    canonical['timestamp'], canonical['sort_seq'] or 0, canonical['local_id'], canonical['source_shard']) > (
                    m['timestamp'], m['sort_seq'] or 0, m['local_id'], m['source_shard']):
                    pass
                else:
                    counts['reused'] += 1
                    continue
            seen[key] = source_input
            old = old_messages.get(key)
            if m['type_name'] is None:
                previous_data = json.loads(old[0]) if old else None
                if old and old[1] == source_input:
                    m['type_name'], m['content'] = previous_data['type_name'], previous_data['content']
                else:
                    m['type_name'], m['content'] = we.parse_message(m['type'], raw)
                    metrics['parsed_messages'] += 1
                if previous_data == m and old[1] == source_input:
                    counts['reused'] += 1
                    continue
            data = json.dumps(m, ensure_ascii=False, sort_keys=True)
            transcript = transcripts.get(key)
            media = None
            if transcript and transcript[1] == 'success' and (m['type'] & 0xffffffff) == 34:
                from voice_service import find_media
                media, _ = find_media(db_dir, username, m)
                if media is not None and media != transcript[0]:
                    c.execute('DELETE FROM transcripts WHERE username=? AND message_key=?', (username, key))
            if old and old[0] == data and old[1] == source_input:
                counts['reused'] += 1
                continue
            counts['changed' if old else 'added'] += 1
            message_writes.append((username, key, m['timestamp'], data, source_input))
            old_messages[key] = (data, source_input)
            if old and old[1] != source_input and ((m['type'] & 0xffffffff) != 34 or media is None):
                c.execute('''DELETE FROM transcripts WHERE username=? AND message_key=?
                    AND NOT EXISTS(SELECT 1 FROM voice_skips s WHERE s.username=transcripts.username
                                   AND s.message_key=transcripts.message_key)''', (username, key))
        flush()

    def save_transcript(self, username, key, media, status, text='', reason='', source=None):
        with closing(self.connect()) as c, c:
            if status == 'success':
                c.execute('DELETE FROM voice_skips WHERE username=? AND message_key=?', (username, key))
            c.execute('INSERT OR REPLACE INTO transcripts VALUES (?,?,?,?,?,?,?)',
                      (username, key, media, status, text, reason, json.dumps(source, ensure_ascii=False)))
            row = c.execute('SELECT data FROM chats WHERE username=?', (username,)).fetchone()
            if row:
                self.metadata(c, username, json.loads(row[0]))

    def transcript(self, username, key):
        with closing(self.connect()) as c:
            return c.execute('SELECT input,status,text FROM transcripts WHERE username=? AND message_key=?',
                             (username, key)).fetchone()

    def record_transcript_error(self, username, key, reason):
        with closing(self.connect()) as c, c:
            c.execute('UPDATE transcripts SET reason=? WHERE username=? AND message_key=?', (reason, username, key))

    def skipped_voices(self):
        with closing(self.connect()) as c:
            return set(c.execute('SELECT username,message_key FROM voice_skips'))

    def set_voice_skipped(self, targets, skipped):
        targets = [dict(username=u, message_id=k) for u, k in
                   dict.fromkeys((t['username'], t['message_id']) for t in targets)]
        users = list(dict.fromkeys(t['username'] for t in targets))
        self.voice_targets(users, targets)
        with closing(self.connect()) as c, c:
            # Validate the whole batch before writing; successful text is never replaced.
            if skipped:
                for t in targets:
                    row = c.execute('SELECT status FROM transcripts WHERE username=? AND message_key=?',
                                    (t['username'], t['message_id'])).fetchone()
                    if not row or row[0] not in ('failed', 'media_missing', 'tool_unavailable'):
                        raise ValueError('仅失败、媒体缺失或工具不可用的语音可标记无需转写；成功结果保留')
            for t in targets:
                values = (t['username'], t['message_id'])
                c.execute('INSERT OR IGNORE INTO voice_skips VALUES (?,?)' if skipped else
                          'DELETE FROM voice_skips WHERE username=? AND message_key=?', values)
        return {'saved': True, 'count': len(targets)}

    def voice_plan(self, usernames, targets=None):
        selected = self.voice_targets(usernames, targets)
        skipped = self.skipped_voices()
        counts = dict(total=len(selected), skipped=0, reusable=0, pending=0)
        with closing(self.connect()) as c:
            successful = set(c.execute("SELECT username,message_key FROM transcripts WHERE status='success' AND username IN (SELECT value FROM json_each(?))", (json.dumps(usernames),)))
            for u, m in selected:
                if (u, m['message_id']) in skipped:
                    counts['skipped'] += 1
                elif targets is None and (u, m['message_id']) in successful:
                    counts['reusable'] += 1
                else:
                    counts['pending'] += 1
        return counts

    def voice_targets(self, usernames, targets=None):
        self.require_visible(usernames)
        wanted = set((t['username'], t['message_id']) for t in targets) if targets is not None else None
        result = []
        with closing(self.connect()) as c:
            for username in usernames:
                for key, data in c.execute('''SELECT message_key,data FROM messages
                        WHERE username=? AND (json_extract(data,'$.type') & 4294967295)=34
                        ORDER BY timestamp,message_key''', (username,)):
                    if wanted is None or (username, key) in wanted:
                        result.append((username, dict(json.loads(data), message_id=key)))
        if wanted is not None and (not wanted or wanted != {(u, m['message_id']) for u, m in result}):
            raise ValueError('所选语音不存在或不在本次会话范围内，请刷新后重选')
        return result

    def voice_page(self, usernames, status='', q='', offset=0, limit=50):
        self.require_visible(usernames)
        if offset < 0 or not 1 <= limit <= 100:
            raise ValueError('语音分页参数无效')
        if status not in ('', 'success', 'not_requested', 'failed', 'media_missing', 'tool_unavailable', 'skipped'):
            raise ValueError('未知转写状态')
        if not usernames:
            return {'messages': [], 'total': 0}
        condition = f"m.username IN ({','.join('?' for _ in usernames)}) AND (json_extract(m.data,'$.type') & 4294967295)=34"
        params = list(usernames)
        if status:
            condition += " AND (CASE WHEN s.message_key IS NOT NULL THEN 'skipped' ELSE coalesce(t.status,'not_requested') END)=?"
            params.append(status)
        if q:
            condition += " AND (instr(lower(coalesce(t.text,'')),lower(?))>0 OR instr(lower(coalesce(json_extract(m.data,'$.sender'),'')),lower(?))>0)"
            params.extend([q, q])
        sql = ' FROM messages m LEFT JOIN transcripts t ON t.username=m.username AND t.message_key=m.message_key LEFT JOIN voice_skips s ON s.username=m.username AND s.message_key=m.message_key WHERE ' + condition
        with closing(self.connect()) as c:
            c.execute('BEGIN')
            total = c.execute('SELECT count(*)' + sql, params).fetchone()[0]
            rows = c.execute("SELECT m.username,m.message_key,m.data,CASE WHEN s.message_key IS NOT NULL THEN 'skipped' ELSE t.status END,t.text,t.reason,t.source" + sql +
                             ' ORDER BY m.timestamp DESC,m.username,m.message_key DESC LIMIT ? OFFSET ?', params + [limit, offset])
            messages = [dict(we.normalize_system_sender(json.loads(data)), username=u, message_id=key,
                        transcription={'status': state or 'not_requested', 'text': text or '', 'reason': reason or '',
                                       'media_source': json.loads(source) if source else None})
                        for u, key, data, state, text, reason, source in rows]
        return {'messages': messages, 'total': total}
