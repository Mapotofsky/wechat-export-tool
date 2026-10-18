# -*- coding: utf-8 -*-
"""微信 4.x 聊天记录解析与导出核心模块。

依赖：zstandard（用于解压 zstd 压缩的消息内容）
安装：pip install zstandard

数据来源：wcdb-key-tool 解密后的数据库（decrypted/ 目录）。
"""
import os
import re
import json
import html
import sqlite3
import hashlib
import datetime
from contextlib import closing, nullcontext, ExitStack
import heapq
from pathlib import Path
from collections import Counter

try:
    import zstandard
    _ZSTD = zstandard.ZstdDecompressor()
    HAS_ZSTD = True
except ImportError:
    HAS_ZSTD = False

ZSTD_MAGIC = b'\x28\xb5\x2f\xfd'


def is_message_shard(name):
    """消息实体分片；message_fts/resource 是辅助库。"""
    return re.fullmatch(r'message_[0-9]+\.db', name) is not None


def _connect_ro(path):
    """以只读模式打开 SQLite，同时读取现有 WAL。"""
    return sqlite3.connect(f'file:{path}?mode=ro', uri=True)

# 消息类型映射（base = local_type & 0xFFFFFFFF）
TYPE_NAMES = {
    1: '文本',
    3: '图片',
    34: '语音',
    37: '好友验证',
    42: '名片',
    43: '视频',
    47: '表情',
    48: '位置',
    49: '链接/文件',
    10000: '系统消息',
}


def normalize_system_sender(message):
    """系统通知没有说话人；保留原始 sender_id 以便回查来源。"""
    if (message.get('type', 0) & 0xffffffff) == 10000:
        message.update(sender='系统消息', sender_username=None, identity_basis='system')
    return message


def decompress(content):
    """zstd 解压（若内容以 zstd 魔数开头）"""
    if isinstance(content, bytes) and content[:4] == ZSTD_MAGIC and HAS_ZSTD:
        try:
            return _ZSTD.decompress(content)
        except Exception:
            return content
    return content


def _xml_tag(xml_text, tag):
    """从 XML 文本里提取指定标签的内容（容错，用正则）"""
    m = re.search(r'<%s[^>]*>(.*?)</%s>' % (tag, tag), xml_text, re.S)
    return m.group(1).strip() if m else ''


def _clean(text):
    """清理提取出的文本：反转义 HTML、去 XML 标签、过滤占位符、限长"""
    if not text:
        return ''
    text = html.unescape(text)
    if '<' in text or '>' in text:
        text = re.sub(r'<[^>]+>', '', text)
    text = text.strip()
    if text.lower() == 'null':
        return ''
    if len(text) > 200:
        text = text[:200] + '…'
    return text


_REF_TYPE = {'3': '图片', '34': '语音', '43': '视频', '47': '表情', '49': '链接', '48': '位置'}


def _parse_refermsg(xml_text):
    """解析引用消息，返回人类可读的引用描述"""
    m = re.search(r'<refermsg>(.*?)</refermsg>', xml_text, re.S)
    if not m:
        return ''
    ref = m.group(1)
    ref_type = _xml_tag(ref, 'type')
    ref_name = _clean(_xml_tag(ref, 'displayname'))
    name = f'「{ref_name}」' if ref_name else ''
    if ref_type == '1':
        c = _clean(_xml_tag(ref, 'content'))
        if c and not c.startswith('<'):
            return f'引用{name}: {c}'
        return f'引用{name}[消息]'
    return f'引用{name}[{_REF_TYPE.get(ref_type, "消息")}]'


def parse_message(local_type, message_content):
    """解析单条消息，返回 (type_name, text)。

    text 为空字符串表示该类型无法文本化（如图片/语音/表情）。
    """
    base = local_type & 0xFFFFFFFF
    tname = TYPE_NAMES.get(base, f'未知({local_type})')

    if base == 1:
        # 文本：str 直接用；bytes(zstd) 解压后是 UTF-8 文本
        if isinstance(message_content, str):
            return tname, message_content.strip()
        d = decompress(message_content)
        if isinstance(d, bytes):
            return tname, d.decode('utf-8', errors='replace').strip()
        return tname, ''

    if base in (3, 34, 43, 47, 37, 42):
        # 图片/语音/视频/表情/名片/验证：无文本，仅标注类型
        return tname, ''

    if base == 10000:
        # 系统消息：解压后可能是文本
        d = decompress(message_content)
        if isinstance(d, (bytes, str)):
            s = d.decode('utf-8', errors='replace') if isinstance(d, bytes) else d
            if '撤回' in s or '加入' in s or '邀请' in s:
                return tname, s.strip()
        return tname, ''

    # 链接/文件(49) 及位置(48)：解压后是 XML，提取关键字段
    d = decompress(message_content)
    if isinstance(d, (bytes, str)):
        xml_text = d.decode('utf-8', errors='replace') if isinstance(d, bytes) else d
        if base == 48:  # 位置
            label = _clean(_xml_tag(xml_text, 'label'))
            x = _xml_tag(xml_text, 'x')
            y = _xml_tag(xml_text, 'y')
            parts = [p for p in [label, (f'({x},{y})' if x and y else '')] if p]
            return tname, ' '.join(parts)
        if base == 49:  # 链接/文件/引用
            title = _clean(_xml_tag(xml_text, 'title'))
            des = _clean(_xml_tag(xml_text, 'des'))
            fileext = _xml_tag(xml_text, 'fileext')
            parts = []
            if title:
                parts.append(title)
            if fileext:
                parts.append(f'[{fileext}]')
            if des and des != title:
                parts.append(des)
            ref = _parse_refermsg(xml_text)
            if ref:
                parts.append(ref)
            return tname, ' '.join(parts).strip()

    # 兜底：尝试解压后提取可读文本
    d = decompress(message_content)
    if isinstance(d, bytes):
        s = d.decode('utf-8', errors='replace')
        frags = re.findall(r'[\u4e00-\u9fffA-Za-z0-9，。！？、：；""''（）…—.-]{2,}', s)
        return tname, ' '.join(frags[:10]).strip()
    return tname, ''


_SYSTEM_USERS = {
    'filehelper', 'weixin', 'notifymessage', 'floatbottle', 'medianote',
    'fmessage', 'qqmail', 'tmessage', 'qmessage', 'officialaccounts',
}


def classify_username(username):
    """按 username 特征分类联系人"""
    if username.endswith('@chatroom'):
        return 'group'
    if username.endswith('@openim'):
        return 'openim'
    if username.startswith('gh_'):
        return 'official'
    if username in _SYSTEM_USERS:
        return 'system'
    return 'friend'


_SELF_ID_CACHE = {}


def detect_self_sender_id(db_path, self_username=None, with_basis=False, connection=None):
    """确定某个 message 分片里"自己"的 real_sender_id。

    优先使用 filehelper 中唯一的非系统消息发送者；然后查询该分片
    Name2Id 中用户确认的内部 username；最后保留存在编号 4 则取 4、否则 1
    的启发式回退。识别依据可通过 with_basis 获取，每次读取重新判断。
    """
    self_id = None
    basis = 'heuristic_1'
    with closing(_connect_ro(db_path)) if connection is None else nullcontext(connection) as conn:
        cur = conn.cursor()
        # 锚点1：filehelper 表，消息全部是"自己"发的
        fh = 'Msg_' + hashlib.md5(b'filehelper').hexdigest()
        try:
            r = cur.execute(
                f'SELECT DISTINCT real_sender_id FROM "{fh}" WHERE real_sender_id IS NOT NULL '
                'AND (local_type & 4294967295) != 10000 LIMIT 2'
            ).fetchall()
            if len(r) == 1:
                self_id = r[0][0]
                basis = 'filehelper'
        except Exception:
            pass
        if self_id is None and self_username:
            try:
                r = cur.execute('SELECT rowid FROM Name2Id WHERE user_name=?', (self_username,)).fetchone()
                if r:
                    self_id, basis = r[0], 'username_mapping'
            except sqlite3.OperationalError:
                pass
        # 锚点2：新格式标志 —— 分片内是否存在 real_sender_id == 4
        if self_id is None:
            try:
                tables = [r[0] for r in cur.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'Msg_%'"
                )]
                for name in tables:
                    if cur.execute(
                        f'SELECT 1 FROM "{name}" WHERE real_sender_id = 4 LIMIT 1'
                    ).fetchone():
                        self_id = 4
                        basis = 'heuristic_4'
                        break
            except Exception:
                pass
    if self_id is None:
        self_id = 1  # 旧格式默认：自己 = 1
    return (self_id, basis) if with_basis else self_id


def collect_msg_tables(db_dir):
    """收集所有有消息表的会话 md5 hash 集合"""
    msg_dir = os.path.join(db_dir, 'message')
    hashes = set()
    if not os.path.isdir(msg_dir):
        return hashes
    for fname in os.listdir(msg_dir):
        if not is_message_shard(fname):
            continue
        db_path = os.path.join(msg_dir, fname)
        try:
            with closing(_connect_ro(db_path)) as conn:
                rows = conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'Msg_%'"
                ).fetchall()
        except sqlite3.Error as e:
            raise RuntimeError(f'读取消息数据库失败: {db_path}') from e
        for (name,) in rows:
            hashes.add(name[4:])  # 去掉 "Msg_" 前缀
    return hashes


def load_contacts(db_dir, include_official=False, only_with_msg=True):
    """读取联系人列表，返回 [{username, category, remark, nick_name, name}]

    默认过滤公众号(official)和系统(system)账号；
    only_with_msg=True 时只返回有聊天记录的联系人。
    """
    contact_db = os.path.join(db_dir, 'contact', 'contact.db')
    if not os.path.exists(contact_db):
        return []
    msg_hashes = collect_msg_tables(db_dir) if only_with_msg else None

    conn = _connect_ro(contact_db)
    conn.row_factory = sqlite3.Row
    columns = {r[1] for r in conn.execute('PRAGMA table_info(contact)')}
    alias = 'alias' if 'alias' in columns else "'' AS alias"
    rows = conn.execute(
        f'SELECT username, local_type, remark, nick_name, {alias} FROM contact'
    ).fetchall()
    conn.close()

    contacts = []
    seen = set()
    for r in rows:
        username = r['username']
        if not username or username in seen:
            continue
        seen.add(username)
        category = classify_username(username)
        if category in ('official', 'system') and not include_official:
            continue
        if msg_hashes is not None:
            h = hashlib.md5(username.encode('utf-8')).hexdigest()
            if h not in msg_hashes:
                continue
        name = r['remark'] or r['nick_name'] or username
        contacts.append({
            'username': username,
            'category': category,
            'local_type': r['local_type'],
            'remark': r['remark'],
            'nick_name': r['nick_name'],
            'alias': r['alias'],
            'name': name,
        })
    order = {'friend': 0, 'group': 1, 'openim': 2, 'official': 3}
    contacts.sort(key=lambda c: (order.get(c['category'], 9), c['name']))
    return contacts


def find_contact(contacts, keyword):
    """按备注/昵称/username 模糊查找联系人"""
    kw = keyword.strip()
    for c in contacts:
        if c['username'] == kw or c['remark'] == kw or c['nick_name'] == kw:
            return c
    for c in contacts:
        if kw in c['remark'] or kw in c['nick_name']:
            return c
    return None


class MessageReader:
    """One import's read transactions and identity/contact caches; bounded row iteration."""
    def __init__(self, db_dir, self_username=None):
        self.db_dir = Path(db_dir)
        self.self_username = self_username
        self.stack = ExitStack()
        self.shards = {}
        self.names = {}

    def __enter__(self):
        try:
            contact = self.db_dir / 'contact' / 'contact.db'
            if contact.exists():
                c = self.stack.enter_context(closing(_connect_ro(str(contact))))
                c.execute('BEGIN')
                self.names = {u: r or n or u for u, r, n in c.execute('SELECT username,remark,nick_name FROM contact')}
            for path in sorted((self.db_dir / 'message').glob('message_*.db')):
                if not is_message_shard(path.name):
                    continue
                c = self.stack.enter_context(closing(_connect_ro(str(path))))
                c.row_factory = sqlite3.Row
                c.execute('PRAGMA temp_store=FILE')
                c.execute('BEGIN')
                tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                users = {r[0]: r[1] for r in c.execute('SELECT rowid,user_name FROM Name2Id')} if 'Name2Id' in tables else {}
                identity = detect_self_sender_id(str(path), self.self_username, True, c)
                self.shards[path.name] = (c, tables, users, identity)
            return self
        except BaseException:
            self.stack.close()
            raise

    def __exit__(self, *exc):
        return self.stack.__exit__(*exc)

    def iter_messages(self, username, my_name='我', other_name=None, shards=None, parse=True):
        table = 'Msg_' + hashlib.md5(username.encode()).hexdigest()
        def rows(fname, state):
            c, tables, users, (self_id, basis) = state
            if table not in tables:
                return
            columns = {r[1] for r in c.execute(f'PRAGMA table_info("{table}")')}
            if not {'local_id', 'local_type', 'real_sender_id', 'create_time', 'message_content'}.issubset(columns):
                raise sqlite3.OperationalError('消息表缺少必要字段')
            seq = 'COALESCE(sort_seq,0)' if 'sort_seq' in columns else '0+0'
            cursor = c.execute(f'SELECT * FROM "{table}" ORDER BY create_time,{seq},local_id')
            while batch := cursor.fetchmany(512):
                for r in batch:
                    sender_id = r['real_sender_id']
                    user = self.self_username if sender_id == self_id and self.self_username else users.get(sender_id)
                    sender = my_name if sender_id == self_id else (
                        self.names.get(user) or user or f'未知发送者#{sender_id}'
                        if username.endswith('@chatroom') else other_name or '对方')
                    tname, text = parse_message(r['local_type'], r['message_content']) if parse else (None, None)
                    yield normalize_system_sender({'local_id': r['local_id'], 'source_shard': fname,
                           'server_id': r['server_id'] if 'server_id' in columns else 0,
                           'status': r['status'] if 'status' in columns else None,
                           'sort_seq': r['sort_seq'] if 'sort_seq' in columns else 0,
                           'sender_username': user, 'identity_basis': basis,
                           'time': datetime.datetime.fromtimestamp(r['create_time']).strftime('%Y-%m-%d %H:%M:%S'),
                           'timestamp': r['create_time'], 'sender': sender, 'sender_id': sender_id,
                           'type': r['local_type'], 'type_name': tname, 'content': text,
                           '_raw_content': r['message_content']})
        streams = [rows(fname, state) for fname, state in self.shards.items() if shards is None or fname in shards]
        return heapq.merge(*streams, key=lambda m: (m['timestamp'], m['sort_seq'] or 0, m['local_id'], m['source_shard']))


def get_messages(db_dir, username, my_name='我', other_name=None, self_username=None):
    with MessageReader(db_dir, self_username) as reader:
        return list(reader.iter_messages(username, my_name, other_name))




def _safe_name(name):
    """生成安全的文件名（去除非法字符）"""
    return re.sub(r'[\\/:*?"<>|\s]+', '_', name).strip('_') or '联系人'


def export_basename(contact):
    """用显示名和唯一用户名组成文件名，避免同名联系人覆盖。"""
    return f"{_safe_name(contact['name'])}_{_safe_name(contact['username'])}"


def render_txt(display, messages):
    """渲染 TXT 文本"""
    out = [f'与「{display}」的聊天记录', f'共 {len(messages)} 条消息']
    if messages:
        out.append(f'时间范围：{messages[0]["time"]} ~ {messages[-1]["time"]}')
    out.append('=' * 60)
    out.append('')
    for m in messages:
        if m['type_name'] == '文本':
            out.append(f'[{m["time"]}] {m["sender"]}: {m["content"]}')
        else:
            extra = f' {m["content"]}' if m['content'] else ''
            out.append(f'[{m["time"]}] {m["sender"]}: [{m["type_name"]}]{extra}')
    return '\n'.join(out) + '\n'


def render_json(contact, messages):
    """渲染 JSON 文本"""
    return json.dumps({
        'contact': contact,
        'total': len(messages),
        'time_range': [messages[0]['time'], messages[-1]['time']] if messages else [],
        'messages': [{k: v for k, v in m.items() if not k.startswith('_')} for m in messages],
    }, ensure_ascii=False, indent=2)


def export_contact(db_dir, contact, out_dir, my_name='我'):
    """导出单个联系人的聊天记录为 JSON + TXT"""
    username = contact['username']
    display = contact['remark'] or contact['nick_name'] or username
    other_name = contact['remark'] or contact['nick_name'] or username
    messages = get_messages(db_dir, username, my_name=my_name, other_name=other_name)

    os.makedirs(out_dir, exist_ok=True)
    safe = export_basename({**contact, 'name': display})
    json_path = os.path.join(out_dir, f'{safe}_聊天记录.json')
    txt_path = os.path.join(out_dir, f'{safe}_聊天记录.txt')

    with open(json_path, 'w', encoding='utf-8') as f:
        f.write(render_json(contact, messages))
    with open(txt_path, 'w', encoding='utf-8') as f:
        f.write(render_txt(display, messages))

    stat = Counter(m['type_name'] for m in messages)
    return {'json': json_path, 'txt': txt_path, 'total': len(messages), 'stat': dict(stat)}
