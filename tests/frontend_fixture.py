"""Synthetic browser/desktop acceptance, real bundled decoder and loopback ASR only."""
import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import sys
import threading
import time
import wave

parser = argparse.ArgumentParser()
parser.add_argument('--root', required=True, type=Path)
parser.add_argument('--app', type=Path)
parser.add_argument('--desktop', action='store_true')
parser.add_argument('--exercise-exit', action='store_true')
parser.add_argument('--long-reading', action='store_true')
parser.add_argument('--port', type=int, default=8774)
args = parser.parse_args()
args.root = args.root.resolve()
args.root.mkdir(parents=True, exist_ok=True)
repo = Path(__file__).resolve().parent.parent
os.environ['WECHAT_ARCHIVE_DATA_DIR'] = str(args.root / 'bootstrap')
os.environ['WECHAT_ARCHIVE_VOICE_PYTHON'] = str(repo / 'dist/微信聊天档案 完整语音版/voice-runtime/python.exe')
sys.path[:0] = [str(repo), str(repo / 'tests')]
from u2_synthetic import prepare
manager, source = prepare(args.root)
if args.app:
    sys.path.insert(0, str(args.app.resolve()))
from web import app as web
import voice_service
import uvicorn
web.manager = manager
archive = manager.archive()
if args.long_reading:
    from contextlib import closing
    import sqlite3
    from test_maintenance import table
    with closing(sqlite3.connect(source / 'message/message_0.db')) as c, c:
        name = table('u2_159')
        if not c.execute(f'SELECT 1 FROM "{name}" WHERE local_id=5').fetchone():
            c.executemany(f'INSERT INTO "{name}" VALUES (?,?,1,8,?,?,0)',
                [(i, 20000+i, 1713759200+i, f'合成长消息 报销 {i} ' * 5) for i in range(5,125)])
    archive.update(source, usernames=['u2_159'])
audio = io.BytesIO()
with wave.open(audio, 'wb') as wav:
    wav.setnchannels(1)
    wav.setsampwidth(2)
    wav.setframerate(16000)
    wav.writeframes(b'\0\0' * 1600)
media = audio.getvalue()
voice_service.find_media = lambda *args: (media, {'database':'synthetic'})
archive.save_transcript('u2_000@chatroom', 'server:10001', media, 'success', text='合成成功转写')
control = {'delay':3, 'calls':0, 'fail_call':2, 'commit_delay':0, 'space_fail':False}
class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        self.rfile.read(int(self.headers['Content-Length']))
        control['calls'] += 1
        call = control['calls']
        time.sleep(control['delay'])
        self.send_response(503 if call == control['fail_call'] or call in control.get('fail_calls', []) else 200)
        self.end_headers()
        self.wfile.write(json.dumps({'error':'synthetic service failure'} if call == control['fail_call'] or call in control.get('fail_calls', []) else {'text':f'合成在线转写 合同 {call}'}).encode())
    def log_message(self, *args):
        pass
server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
threading.Thread(target=server.serve_forever, daemon=True).start()
archive.save_config({'asr_provider':'openai', 'asr_api_url':f'http://127.0.0.1:{server.server_port}/asr', 'asr_api_model':'synthetic'})
original_update = archive.update
def update(db_dir, progress, **kwargs):
    def report(stage, *counts):
        progress(stage, *counts)
        if stage == '提交消息档案':
            time.sleep(control['commit_delay'])
    return original_update(db_dir, report, **kwargs)
archive.update = update
original_reclaim = archive.reclaim_space
def reclaim():
    if control['space_fail']:
        archive.save_config({'space_reclaim_pending':True})
        return {'space_reclaimed':False,'warning':'合成空间回收失败，请重试。'}
    return original_reclaim()
archive.reclaim_space = reclaim
@web.app.post('/fixture/control')
def configure(values: dict):
    control.update(values)
    return control
@web.app.post('/fixture/cleanup-failure')
def cleanup_failure():
    from maintenance import atomic_json
    with manager.lock:
        task = manager.task or manager.status()['task']
        if manager.busy() or not task:
            raise ValueError('请先完成一个合成任务')
        manager.task = task
        resource = args.root / '模拟未发布资源'
        resource.mkdir(exist_ok=True)
        (resource / 'temporary.txt').write_text('synthetic', encoding='utf-8')
        manager.task.update(status='failed', reason='任务资源清理失败：合成错误；请重试任务资源清理',
            cleanup_resources=[{'path':str(resource), 'directory':True}])
        manager.cleanup = {'stage':'任务资源清理未完成，请重试', 'type':'task_resources',
            'task_id':manager.task['id'], 'account_id':manager.account()['id']}
        atomic_json(archive.directory / 'task.json', manager.task)
        return {'injected':True}
if args.desktop:
    import desktop
    if args.exercise_exit:
        import webview
        original_create = webview.create_window
        def create(*values, **kwargs):
            window = original_create(*values, **kwargs)
            bridge = kwargs['js_api']
            def exercise():
                evidence = {}
                def record():
                    (args.root / 'exit-evidence.json').write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding='utf-8')
                try:
                    time.sleep(1)
                    control.update(delay=8, fail_call=0)
                    task = manager.submit('transcribe', ['u2_159'], voice_targets=[{'username':'u2_159','message_id':'server:11591'}], account_id=manager.account()['id'])
                    deadline = time.monotonic() + 15
                    while '等待当前语音响应' not in manager.status()['task'].get('activity', ''):
                        if time.monotonic() > deadline:
                            raise TimeoutError('未到达模拟请求等待阶段')
                        time.sleep(.1)
                    evidence['before_close'] = manager.status()['task']
                    window.destroy()  # Real native closing event; must remain open while busy.
                    time.sleep(.5)
                    evidence['exit_panel'] = window.evaluate_js("document.querySelector('.drawer')?.innerText")
                    record()
                    window.evaluate_js("[...document.querySelectorAll('button')].find(b=>b.textContent==='继续使用 / 等待').click()")
                    time.sleep(.2)
                    evidence['wait_keeps_running'] = manager.status()['task']['status'] == 'running'
                    window.destroy()
                    time.sleep(.3)
                    window.evaluate_js("[...document.querySelectorAll('button')].find(b=>b.textContent==='最小化继续处理').click()")
                    time.sleep(.2)
                    evidence['minimized_task_status'] = manager.status()['task']['status']
                    window.restore()
                    time.sleep(.3)
                    evidence['restored_panel'] = window.evaluate_js("document.querySelector('.drawer')?.innerText")
                    window.evaluate_js("[...document.querySelectorAll('button')].find(b=>b.textContent==='停止后退出').click()")
                    time.sleep(.2)
                    evidence['stop_requested'] = manager.status()['task']['status']
                    evidence['waiting_panel'] = window.evaluate_js("document.querySelector('.drawer')?.innerText")
                    record()
                    if evidence['stop_requested'] != 'cancelling':
                        raise AssertionError('停止后退出没有提交取消申请')
                    deadline = time.monotonic() + 15
                    while manager.busy():
                        if time.monotonic() > deadline:
                            raise TimeoutError('停止后资源未释放')
                        time.sleep(.1)
                    evidence['finished'] = manager.status()['task']
                    record()
                except BaseException:
                    import traceback
                    evidence['error'] = traceback.format_exc()
                    record()
            window.events.loaded += lambda: threading.Thread(target=exercise, daemon=True).start()
            window.events.closed += lambda: (args.root / 'window-closed.txt').write_text('closed', encoding='utf-8')
            return window
        webview.create_window = create
    try:
        desktop.main([])
    finally:
        server.shutdown()
        server.server_close()
        manager.executor.shutdown()
else:
    try:
        uvicorn.run(web.app, host='127.0.0.1', port=args.port, access_log=False)
    finally:
        server.shutdown()
        server.server_close()
        manager.executor.shutdown()
