"""Run the real pythonw desktop with isolated speech, a loopback API and window events.

Use a portable runtime/pythonw.exe, --app <package/app>, --output <empty directory>.
The worker shim only records real process starts before running the shipped worker.
No real WeChat data, keys or paid API is accessed. Results and fixtures are retained.
"""
import argparse
from contextlib import closing
import ctypes
from ctypes import wintypes
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import runpy
import sqlite3
import subprocess
import sys
import threading
import time
import traceback
import _winapi


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--app', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--base', action='store_true', help='Verify missing voice components in the base package')
    parser.add_argument('--smoke', action='store_true', help='Exercise the shipped --smoke-test instead of voice tasks')
    args = parser.parse_args()
    app, output = args.app.resolve(), args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    os.environ['WECHAT_ARCHIVE_DATA_DIR'] = str(output / 'state')
    os.environ['WECHAT_ARCHIVE_HOME'] = str(app.parent)
    sys.path.insert(0, str(app))
    sys.path.insert(1, str(Path(__file__).resolve().parent.parent / 'tests'))
    import voice_service
    from test_maintenance import seed, table
    from web import app as web
    import webview

    source = output / 'synthetic source'
    username = seed(source)
    with closing(sqlite3.connect(source / 'message/message_0.db')) as db, db:
        db.execute(f'INSERT INTO "{table(username)}" VALUES (3,103,34,8,1700000002,"voice",0)')
    web.manager.select(str(source), 'synthetic only', True)
    archive = web.manager.archive()
    archive.save_config({'self_username': 'wxid_self'})
    archive.update(source)
    # Prevent the setup suggestion endpoint from inspecting real machine directories.
    for route in web.app.routes:
        if getattr(route, 'path', '') == '/api/suggest-directory':
            route.dependant.call = lambda: {'directory': '', 'decrypted_default': str(source)}

    speech = output / 'synthetic speech.wav'
    if not args.base:
        escaped = str(speech).replace("'", "''")
        subprocess.run(['powershell.exe', '-NoProfile', '-Command',
            "Add-Type -AssemblyName System.Speech; $speaker = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
            f"$speaker.SetOutputToWaveFile('{escaped}'); $speaker.Speak('This is a synthetic speech test. The meeting starts tomorrow morning.'); $speaker.Dispose()"],
            capture_output=True, check=True, timeout=30, creationflags=subprocess.CREATE_NO_WINDOW)
        with closing(sqlite3.connect(source / 'message/media_0.db')) as db, db:
            db.execute('CREATE TABLE Name2Id(user_name TEXT)')
            db.execute('INSERT INTO Name2Id(rowid,user_name) VALUES (99,?)', (username,))
            db.execute('CREATE TABLE VoiceInfo(chat_name_id INTEGER,local_id INTEGER,svr_id INTEGER,create_time INTEGER,voice_data BLOB,data_index TEXT)')
            db.execute('INSERT INTO VoiceInfo VALUES (99,3,103,1700000002,?,"synthetic")', (speech.read_bytes(),))

    requests = []
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers['Content-Length']))
            requests.append({'wav_received': b'RIFF' in body, 'multipart': b'name="file"' in body})
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({'text': '合成本地接口识别成功'}).encode())

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    trace = output / 'processes.jsonl'
    shim = output / 'worker trace.py'
    shim.write_text(
        'import ctypes,json,os,runpy,subprocess,traceback,_winapi\n'
        f'trace={str(trace)!r}\n'
        'def record(value):\n'
        ' with open(trace,"a",encoding="utf-8") as out: out.write(json.dumps(value)+"\\n")\n'
        'native_create=_winapi.CreateProcess\n'
        'def create_process(*args):\n'
        ' result=native_create(*args)\n'
        ' record({"kind":"native", "pid":result[2],"command":args[1],"creationflags":args[5],"startup_flags":args[8].dwFlags,"show_window":args[8].wShowWindow})\n'
        ' return result\n'
        '_winapi.CreateProcess=create_process\n'
        'record({"kind":"worker", "pid":os.getpid(), "console":ctypes.windll.kernel32.GetConsoleWindow()})\n'
        'original=subprocess.Popen\n'
        'class TracedPopen(original):\n'
        ' def __init__(self,*args,**kwargs):\n'
        '  super().__init__(*args,**kwargs)\n'
        '  command=args[0] if args else kwargs["args"]\n'
        '  record({"kind":"child", "pid":self.pid,"command":command,"creationflags":kwargs.get("creationflags",0),"shell":kwargs.get("shell",False),"stack":traceback.format_stack(limit=8)})\n'
        'subprocess.Popen=TracedPopen\n'
        f'runpy.run_path({str(app / "local_asr.py")!r},run_name="__main__")\n', encoding='utf-8')
    voice_service.WORKER = shim
    native_create = _winapi.CreateProcess
    def create_process(*items):
        created = native_create(*items)
        with trace.open('a', encoding='utf-8') as out:
            out.write(json.dumps({'kind': 'native', 'pid': created[2], 'command': items[1],
                'creationflags': items[5], 'startup_flags': items[8].dwFlags,
                'show_window': items[8].wShowWindow, 'phase': phase[0]}) + '\n')
        return created
    _winapi.CreateProcess = create_process
    original_popen = subprocess.Popen
    class TracedPopen(original_popen):
        def __init__(self, *items, **kwargs):
            super().__init__(*items, **kwargs)
            with trace.open('a', encoding='utf-8') as out:
                out.write(json.dumps({'kind': 'host_child', 'pid': self.pid, 'command': items[0] if items else kwargs['args'],
                                      'creationflags': kwargs.get('creationflags', 0), 'shell': kwargs.get('shell', False)}) + '\n')
    subprocess.Popen = TracedPopen

    # Out-of-context WinEvent hooks catch even briefly shown console windows.
    user32 = ctypes.windll.user32
    events, foreground = [], []
    phase = ['startup']
    ready, stop = threading.Event(), threading.Event()
    callback_type = ctypes.WINFUNCTYPE(None, wintypes.HANDLE, wintypes.DWORD, wintypes.HWND,
                                     wintypes.LONG, wintypes.LONG, wintypes.DWORD, wintypes.DWORD)
    user32.SetWinEventHook.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.HMODULE,
                                      callback_type, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD]
    user32.SetWinEventHook.restype = wintypes.HANDLE
    user32.UnhookWinEvent.argtypes = [wintypes.HANDLE]
    def monitor():
        def event(hook, kind, hwnd, object_id, child_id, thread, timestamp):
            if not hwnd:
                return
            name = ctypes.create_unicode_buffer(256)
            user32.GetClassNameW(hwnd, name, 256)
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            item = {'phase': phase[0], 'event': kind, 'class': name.value, 'pid': pid.value,
                    'visible': bool(user32.IsWindowVisible(hwnd))}
            if kind == 3:
                foreground.append(item)
            if name.value in ('ConsoleWindowClass', 'CASCADIA_HOSTING_WINDOW_CLASS'):
                events.append(item)
        callback = callback_type(event)
        hooks = [user32.SetWinEventHook(a, b, None, callback, 0, 0, 0)
                 for a, b in ((3, 3), (0x8000, 0x8002))]
        ready.set()
        message = wintypes.MSG()
        while not stop.is_set():
            while user32.PeekMessageW(ctypes.byref(message), None, 0, 0, 1):
                user32.TranslateMessage(ctypes.byref(message))
                user32.DispatchMessageW(ctypes.byref(message))
            time.sleep(.005)
        for hook in hooks:
            if hook:
                user32.UnhookWinEvent(hook)
    monitor_thread = threading.Thread(target=monitor, daemon=True)
    monitor_thread.start()
    ready.wait(5)
    result = {'python': sys.executable, 'app': str(app), 'host_console': ctypes.windll.kernel32.GetConsoleWindow()}
    create_window = webview.create_window
    def create(*items, **kwargs):
        window = create_window(*items, **kwargs)
        def exercise():
            try:
                # Positive control verifies the observer detects a real new console.
                phase[0] = 'control'
                control = original_popen([str(app.parent / 'runtime/python.exe'), '-c', 'import time; time.sleep(1)'],
                                         creationflags=subprocess.CREATE_NEW_CONSOLE)
                control.wait(timeout=10)
                time.sleep(.2)
                phase[0] = 'tasks'
                base_url = items[1]
                def api(path, body=None):
                    import urllib.request
                    request = urllib.request.Request(base_url + '/api/' + path,
                        data=None if body is None else json.dumps(body).encode(),
                        headers={'Content-Type': 'application/json'})
                    with urllib.request.urlopen(request, timeout=120) as response:
                        return json.load(response)
                if args.smoke:
                    return
                result['probe'] = api('asr-status')
                def task(provider, broken=False):
                    config = {'asr_provider': provider}
                    if provider != 'local':
                        config.update(asr_api_url=f'http://127.0.0.1:{server.server_port}/transcribe', asr_api_model='synthetic')
                    api('settings', config)
                    if broken:
                        with closing(sqlite3.connect(source / 'message/media_0.db')) as db, db:
                            db.execute('UPDATE VoiceInfo SET voice_data=?', (b'invalid synthetic audio',))
                    api('tasks', {'action': 'transcribe', 'usernames': [username],
                                  'voice_targets': [{'username': username, 'message_id': 'server:103'}]})
                    deadline = time.monotonic() + 300
                    stages = []
                    while time.monotonic() < deadline:
                        state = api('data-status')['task']
                        if state['stage'] not in stages:
                            stages.append(state['stage'])
                        if state['status'] != 'running':
                            return {'task': state, 'stages': stages}
                        time.sleep(.1)
                    raise TimeoutError('Synthetic desktop voice task exceeded 300 seconds')
                if not args.base:
                    result['local'] = task('local')
                    result['online'] = task('openai')
                    result['decode_failure'] = task('openai', broken=True)
                    # Verify the visible task panel renders progress, result and errors.
                    window.evaluate_js("document.querySelector('.top-actions button').click()")
                    time.sleep(2)
                    text = window.evaluate_js('document.body.innerText')
                    result['error_visible'] = '在线识别前音频解码失败' in text and '退出码' in text
                    result['requests'] = requests
                else:
                    result['base_missing_components'] = not result['probe']['available']
            except BaseException:
                result['error'] = traceback.format_exc()
            finally:
                phase[0] = 'finish'
                stop.set()
                monitor_thread.join(5)
                result['console_events'] = events
                result['foreground_events'] = foreground
                result['processes'] = [json.loads(line) for line in trace.read_text(encoding='utf-8').splitlines()] if trace.exists() else []
                result['observer_control_detected'] = any(e['phase'] == 'control' for e in events)
                result['task_console_events'] = [e for e in events if e['phase'] == 'tasks' and e['visible']]
                result['task_foreground_events'] = [e for e in foreground if e['phase'] == 'tasks']
                native = [p for p in result['processes'] if p['kind'] == 'native' and
                          not p['creationflags'] & subprocess.CREATE_NEW_CONSOLE]
                result['native_background_starts_hidden'] = all(
                    p['creationflags'] & subprocess.CREATE_NO_WINDOW or
                    (p['startup_flags'] & subprocess.STARTF_USESHOWWINDOW and p['show_window'] == 0)
                    for p in native)
                write_json(output / 'background-probe.json', result)
                if not args.smoke:
                    window.destroy()
        window.events.loaded += lambda: threading.Thread(target=exercise, daemon=True).start()
        return window
    webview.create_window = create
    try:
        # Execute the shipped desktop entry, with its real server and EdgeChromium window.
        sys.argv = [str(app / 'desktop.py')] + (['--smoke-test'] if args.smoke else [])
        runpy.run_path(str(app / 'desktop.py'), run_name='__main__')
    finally:
        server.shutdown()
        server.server_close()
        web.manager.executor.shutdown()


if __name__ == '__main__':
    try:
        main()
    except BaseException as exc:
        if isinstance(exc, SystemExit) and not exc.code:
            raise
        if '--output' in sys.argv:
            failure = Path(sys.argv[sys.argv.index('--output') + 1])
            failure.mkdir(parents=True, exist_ok=True)
            (failure / 'probe-error.txt').write_text(traceback.format_exc(), encoding='utf-8')
        raise
