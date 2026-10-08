"""Transactional, account-local message archive. Missing source rows are retained."""
import base64
import datetime
import json
from pathlib import Path
import sqlite3
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
                CREATE TABLE IF NOT EXISTS locations(
                    username TEXT NOT NULL,shard TEXT NOT NULL,local_id INTEGER NOT NULL,
                    timestamp INTEGER NOT NULL,message_key TEXT NOT NULL,
                    PRIMARY KEY(username,shard,local_id,timestamp));
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

    @staticmethod
    def key(message):
        # Server ids identify the same message across shards; unsent/local rows stay shard scoped.
        if message.get('server_id'):
            return 'server:' + str(message['server_id'])
        return f"local:{message['source_shard']}:{message['local_id']}:{message['timestamp']}"

    def messages(self, username, connection=None, limit=None):
        if connection is None:
            with closing(self.connect()) as c:
                return self.messages(username, c, limit)
        result = []
        for key, data, status, text, reason, source in connection.execute('''
            SELECT m.message_key,m.data,t.status,t.text,t.reason,t.source FROM messages m
            LEFT JOIN transcripts t ON t.username=m.username AND t.message_key=m.message_key
            WHERE m.username=? ORDER BY m.timestamp DESC,json_extract(m.data,'$.sort_seq') DESC,
            json_extract(m.data,'$.local_id') DESC,m.message_key DESC''' +
            (' LIMIT ?' if limit is not None else ''), (username, limit) if limit is not None else (username,)):
            message = json.loads(data)
            message['message_id'] = key
            if (message['type'] & 0xffffffff) == 34:
                message['transcription'] = {'status': status or 'not_requested', 'reason': reason or '',
                                            'media_source': json.loads(source) if source else None}
                if status == 'success':
                    message['content'] = text
            result.append(message)
        return list(reversed(result))

    def metadata(self, c, username, contact):
        messages = self.messages(username, c)
        senders = {}
        if contact['category'] == 'group':
            for m in messages:
                key = m.get('sender_username') or f"{m['source_shard']}:{m['sender_id']}"
                senders[key] = {'username': m.get('sender_username'), 'name': m['sender']}
        contact = dict(contact)
        contact.update(conversation_id=username, total=len(messages),
                       first_timestamp=messages[0]['timestamp'] if messages else None,
                       last_timestamp=messages[-1]['timestamp'] if messages else None,
                       last_type=messages[-1]['type_name'] if messages else '',
                       preview=(messages[-1]['content'] or f"[{messages[-1]['type_name']}]")[:160] if messages else '',
                       recorded_senders=list(senders.values()), recorded_sender_count=len(senders))
        c.execute('INSERT OR REPLACE INTO chats VALUES (?,?)', (username, json.dumps(contact, ensure_ascii=False)))

    def update(self, db_dir, progress=lambda *args: None, settings=None):
        config = self.config()
        contacts = we.load_contacts(str(db_dir), include_official=True)
        # Include sessions present only in Name2Id (deleted contacts and historical groups).
        known = {x['username'] for x in contacts}
        hashes = we.collect_msg_tables(str(db_dir))
        for shard in sorted((Path(db_dir) / 'message').glob('message_*.db')):
            if not we.is_message_shard(shard.name):
                continue
            with closing(we._connect_ro(str(shard))) as c:
                if c.execute("SELECT 1 FROM sqlite_master WHERE name='Name2Id'").fetchone():
                    import hashlib
                    for (username,) in c.execute('SELECT user_name FROM Name2Id'):
                        if username and username not in known and hashlib.md5(username.encode()).hexdigest() in hashes:
                            known.add(username)
                            contacts.append({'username': username, 'name': username,
                                             'category': we.classify_username(username)})
        counts = {'added': 0, 'changed': 0, 'reused': 0}
        with closing(self.connect()) as c, c:
            for index, contact in enumerate(contacts):
                username = contact['username']
                progress('导入消息', index, len(contacts))
                messages = we.get_messages(str(db_dir), username, config.get('my_name', '我'),
                                           contact['name'], config.get('self_username'))
                seen = {}
                for m in messages:
                    key = self.key(m)
                    location = (username, m['source_shard'], m['local_id'], m['timestamp'])
                    existing_location = c.execute('SELECT message_key FROM locations WHERE username=? AND shard=? AND local_id=? AND timestamp=?', location).fetchone()
                    if existing_location and existing_location[0] != key:
                        old_key = existing_location[0]
                        if not m.get('server_id') and old_key.startswith('server:'):
                            key = old_key
                        elif old_key.startswith('local:') and key.startswith('server:'):
                            if not c.execute('SELECT 1 FROM messages WHERE username=? AND message_key=?', (username, key)).fetchone():
                                c.execute('UPDATE messages SET message_key=? WHERE username=? AND message_key=?', (key, username, old_key))
                                c.execute('UPDATE transcripts SET message_key=? WHERE username=? AND message_key=?', (key, username, old_key))
                            else:
                                c.execute('DELETE FROM messages WHERE username=? AND message_key=?', (username, old_key))
                                c.execute('DELETE FROM transcripts WHERE username=? AND message_key=?', (username, old_key))
                            c.execute('UPDATE locations SET message_key=? WHERE username=? AND message_key=?', (key, username, old_key))
                    c.execute('INSERT OR REPLACE INTO locations VALUES (?,?,?,?,?)', (*location, key))
                    raw = m.pop('_raw_content', None)
                    source_input = json.dumps([encoded(raw), m['type'], m.get('status')], ensure_ascii=False)
                    if key in seen:
                        if seen[key] != source_input:
                            raise RuntimeError('同一会话 server_id 在多个分片中存在冲突内容，未发布本次导入')
                        counts['reused'] += 1
                        continue
                    seen[key] = source_input
                    data = json.dumps(m, ensure_ascii=False, sort_keys=True)
                    old = c.execute('SELECT data,input FROM messages WHERE username=? AND message_key=?',
                                    (username, key)).fetchone()
                    transcript = c.execute('SELECT input,status FROM transcripts WHERE username=? AND message_key=?', (username, key)).fetchone()
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
                    c.execute('INSERT OR REPLACE INTO messages VALUES (?,?,?,?,?)',
                              (username, key, m['timestamp'], data, source_input))
                    if old and old[1] != source_input and ((m['type'] & 0xffffffff) != 34 or media is None):
                        c.execute('DELETE FROM transcripts WHERE username=? AND message_key=?', (username, key))
                self.metadata(c, username, contact)
            c.execute('INSERT OR REPLACE INTO settings VALUES (?,?)', ('last_success', json.dumps(now())))
            for key, value in (settings or {}).items():
                c.execute('INSERT OR REPLACE INTO settings VALUES (?,?)', (key, json.dumps(value)))
        progress('完成', len(contacts), len(contacts))
        return counts

    def save_transcript(self, username, key, media, status, text='', reason='', source=None):
        with closing(self.connect()) as c, c:
            c.execute('INSERT OR REPLACE INTO transcripts VALUES (?,?,?,?,?,?,?)',
                      (username, key, media, status, text, reason, json.dumps(source, ensure_ascii=False)))
            row = c.execute('SELECT data FROM chats WHERE username=?', (username,)).fetchone()
            if row:
                self.metadata(c, username, json.loads(row[0]))

    def transcript(self, username, key):
        with closing(self.connect()) as c:
            return c.execute('SELECT input,status,text FROM transcripts WHERE username=? AND message_key=?',
                             (username, key)).fetchone()
