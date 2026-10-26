"""Manual browser acceptance fixture: strictly synthetic data, isolated temporary state."""
import sys
from pathlib import Path
import tempfile
import os
import sqlite3
import io
import wave
from contextlib import closing

# web.app constructs its Manager at import time; isolate before importing it.
_bootstrap = tempfile.TemporaryDirectory(prefix='wechat_ui_bootstrap_')
os.environ['WECHAT_ARCHIVE_DATA_DIR'] = str(Path(_bootstrap.name) / 'state')

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
    texts = ['本周报销材料已提交', '合同第二版请核对', '报销发票需要补充抬头',
             '明天会议讨论项目进度', '报销审批已通过', '合同附件已归档',
             '请核对报销金额 1200 元', '[语音]']
    shard(source, 1, 'second@chatroom', [(i, 200+i, 34 if i == 8 else 1, 8,
          1700000000+i*86400, texts[i-1], 0) for i in range(1,9)])
    shard(source, 2, 'wxid_a', [(1, 301, 1, 8, 1700000000, 'before gap', 0),
                              (2, 302, 1, 8, 1700864000, 'after gap', 0)])

if __name__ == '__main__':
    with tempfile.TemporaryDirectory(prefix='wechat_ui_test_') as tmp:
        source = Path(tmp) / 'source'
        rich_seed(source)
        if '--voice-asr' in sys.argv:
            # Local mock API plus real audio conversion, with no external calls.
            from fastapi import Request
            @web.app.post('/synthetic/asr')
            async def synthetic_asr(request: Request):
                body = await request.body()
                if b'RIFF' not in body or b'name="file"' not in body:
                    raise ValueError('Expected decoded WAV multipart upload')
                return {'text': '合成在线识别：会议安排已更新'}
            with closing(sqlite3.connect(source / 'message/message_1.db')) as c, c:
                from test_maintenance import table
                c.executemany(f'INSERT INTO "{table("second@chatroom")}" VALUES (?,?,34,8,?,"voice",0)',
                              [(9, 209, 1700691201), (10, 210, 1700691202)])
            audio = io.BytesIO()
            with wave.open(audio, 'wb') as stream:
                stream.setnchannels(1)
                stream.setsampwidth(2)
                stream.setframerate(16000)
                stream.writeframes(b'\0\0' * 1600)
            with closing(sqlite3.connect(source / 'message/media_0.db')) as c, c:
                c.execute('CREATE TABLE Name2Id(user_name TEXT)')
                c.execute('INSERT INTO Name2Id(rowid,user_name) VALUES (99,"second@chatroom")')
                c.execute('CREATE TABLE VoiceInfo(chat_name_id INTEGER,local_id INTEGER,svr_id INTEGER,create_time INTEGER,voice_data BLOB,data_index TEXT)')
                c.executemany('INSERT INTO VoiceInfo VALUES (99,?,?,?,?,"synthetic")',
                              [(i, 200+i, 1700691200+i-8, audio.getvalue()) for i in (8, 9, 10)])
        web.manager = Manager(Path(tmp) / 'state')
        if '--prepared' in sys.argv:
            web.manager.select(str(source), 'synthetic_preview', True)
            web.manager.archive().save_config({'self_username': 'wxid_self'})
            web.manager.archive().update(source)
            web.manager.archive().save_transcript('second@chatroom', 'server:208', b'synthetic',
                                                 'success', text='语音确认：报销已经到账')
        # Avoid inspecting real machine directories in this synthetic browser fixture.
        web.suggest_directory = lambda: {'directory': '', 'decrypted_default': str(source)}
        for route in web.app.routes:
            if getattr(route, 'path', '') == '/api/suggest-directory':
                route.dependant.call = web.suggest_directory
        print('SYNTHETIC_SOURCE=' + str(source), flush=True)
        uvicorn.run(web.app, host='127.0.0.1', port=8767, access_log=False)
