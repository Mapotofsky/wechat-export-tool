"""Reusable U2 source; contains only generated contacts and messages."""
from contextlib import closing
from pathlib import Path
import sqlite3
from test_maintenance import seed, table
from maintenance import Manager


def prepare(root):
    root = Path(root)
    source = root / '合成 源库'
    if not source.exists():
        seed(source)
        with closing(sqlite3.connect(source / 'contact/contact.db')) as c, c:
            c.executemany('INSERT INTO contact VALUES (?,?,1,?,?)',
                          [(username(i), '', f'合成{"群" if i % 2 == 0 else "好友"} {i:03}', '') for i in range(320)])
        with closing(sqlite3.connect(source / 'message/message_0.db')) as c, c:
            for i in range(320):
                c.execute(f'CREATE TABLE "{table(username(i))}"(local_id INTEGER,server_id INTEGER,local_type INTEGER,'
                          'real_sender_id INTEGER,create_time INTEGER,message_content TEXT,status INTEGER)')
                c.executemany(f'INSERT INTO "{table(username(i))}" VALUES (?,?,?,8,?,?,0)',
                              [(j, 10000+i*10+j, 34 if j == 1 else 1, 1700000000+i*86400+j,
                                '[语音]' if j == 1 else f'合成合同 报销 {i} {j}') for j in range(1, i % 4+2)])
    manager = Manager(root / 'state')
    manager.select(str(source), 'U2 合成账号 A', True)
    archive = manager.archive()
    if not archive.contacts():
        archive.save_config({'self_username': 'wxid_self', 'followed_usernames': [username(0),username(159),username(200)],
                             'session_groups': [{'id':'u2-project','name':'合成项目','usernames':[username(0),username(159),username(200),username(319)]}]})
        archive.update(source, usernames=[username(i) for i in range(160)])
        archive.save_transcript(username(0), 'server:10001', b'synthetic', 'success', text='合成成功转写')
        archive.save_transcript(username(1), 'server:10011', b'synthetic', 'failed', reason='合成失败')
    return manager, source


def username(i):
    return f'u2_{i:03}' + ('@chatroom' if i % 2 == 0 else '')
