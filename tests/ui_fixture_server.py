"""Manual browser acceptance fixture: strictly synthetic data, isolated temporary state."""
import sys
from pathlib import Path
import tempfile
import sqlite3
from contextlib import closing

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from test_maintenance import seed, shard
from maintenance import Manager
from web import app as web
import uvicorn


def rich_seed(source):
    seed(source)
    with closing(sqlite3.connect(source / 'contact/contact.db')) as c, c:
        c.execute("UPDATE contact SET remark='项目备注',nick_name='独立昵称',alias='public_alice' WHERE username='wxid_a'")
        c.execute("UPDATE contact SET remark='项目备注',nick_name='另一个人',alias='public_bob' WHERE username='wxid_b'")
        c.executemany('INSERT INTO contact VALUES (?,?,1,?,?)', [
            ('second@chatroom', '', '第二群', ''), ('pending', 'pending_alias', '未导入联系人', '')])
    shard(source, 1, 'second@chatroom', [(i, 200+i, 1, 8, 1700000000+i*86400, f'day {i}', 0) for i in range(1,9)])
    shard(source, 2, 'wxid_a', [(1, 301, 1, 8, 1700000000, 'before gap', 0),
                              (2, 302, 1, 8, 1700864000, 'after gap', 0)])

if __name__ == '__main__':
    with tempfile.TemporaryDirectory(prefix='wechat_ui_test_') as tmp:
        source = Path(tmp) / 'source'
        rich_seed(source)
        web.manager = Manager(Path(tmp) / 'state')
        if '--prepared' in sys.argv:
            web.manager.select(str(source), 'synthetic_preview', True)
            web.manager.archive().save_config({'self_username': 'wxid_self'})
            web.manager.archive().update(source)
        # Avoid inspecting real machine directories in this synthetic browser fixture.
        web.suggest_directory = lambda: {'directory': '', 'decrypted_default': str(source)}
        for route in web.app.routes:
            if getattr(route, 'path', '') == '/api/suggest-directory':
                route.dependant.call = web.suggest_directory
        print('SYNTHETIC_SOURCE=' + str(source), flush=True)
        uvicorn.run(web.app, host='127.0.0.1', port=8767, access_log=False)
