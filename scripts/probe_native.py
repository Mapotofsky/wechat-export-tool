"""Exercise the real EdgeChromium bridge and native save dialog with synthetic bytes.

Run using a built package's runtime/python.exe. Only this process's Save dialog
is automated; output is written under the supplied temporary evidence directory.
"""
import argparse
import ctypes
from ctypes import wintypes
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import sys
import threading
import time

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--app', type=Path, required=True)
parser.add_argument('--output', type=Path, required=True)
args = parser.parse_args()
sys.path.insert(0, str(args.app.resolve()))
from desktop import NativeBridge, check_webview2
import webview

args.output.mkdir(parents=True, exist_ok=True)
target = (args.output / '中文 原生保存.json').resolve()
payload = json.dumps({'text': '报销', 'source': 'synthetic'}, ensure_ascii=False).encode('utf-8')
requests = []


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        requests.append({'path': self.path, 'body': json.loads(self.rfile.read(int(self.headers['Content-Length'])))})
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Disposition', "attachment; filename*=UTF-8''probe.json")
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):
        pass


server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
threading.Thread(target=server.serve_forever, daemon=True).start()
bridge = NativeBridge(f'http://127.0.0.1:{server.server_port}', lambda: False)
window = webview.create_window('原生保存验证（合成数据）', html='<p>正在验证原生保存与桥接</p>', js_api=bridge)
bridge._window = window


def automate_dialog(kind='save'):
    deadline = time.monotonic() + 25
    found = []
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def inspect(hwnd, _):
        pid = wintypes.DWORD()
        ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        name = ctypes.create_unicode_buffer(128)
        ctypes.windll.user32.GetClassNameW(hwnd, name, 128)
        if pid.value == os.getpid() and name.value == '#32770' and ctypes.windll.user32.IsWindowVisible(hwnd):
            found.append(hwnd)
        return True

    callback = callback_type(inspect)
    while time.monotonic() < deadline:
        ctypes.windll.user32.EnumWindows(callback, 0)
        if found:
            children = []
            def child(hwnd, _):
                name = ctypes.create_unicode_buffer(128)
                text = ctypes.create_unicode_buffer(1024)
                ctypes.windll.user32.GetClassNameW(hwnd, name, 128)
                ctypes.windll.user32.GetWindowTextW(hwnd, text, 1024)
                children.append((hwnd, name.value, text.value, ctypes.windll.user32.GetDlgCtrlID(hwnd)))
                return True
            child_callback = callback_type(child)
            ctypes.windll.user32.EnumChildWindows(found[0], child_callback, 0)
            (args.output / (kind + '-dialog-controls.json')).write_text(json.dumps(children, ensure_ascii=False), encoding='utf-8')
            edits = [h for h, name, _, _ in children if name == 'Edit']
            buttons = [h for h, name, _, control_id in children if name == 'Button' and control_id in ((6, 1) if kind == 'busy' else (1,))]
            send = ctypes.windll.user32.SendMessageW
            send.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
            if kind == 'busy' and buttons:
                send(buttons[0], 0x00F5, 0, 0)
            elif edits and buttons:
                text = ctypes.create_unicode_buffer(str(args.output.resolve() if kind == 'folder' else target))
                send(edits[0], 0x000C, 0, ctypes.cast(text, ctypes.c_void_p).value)
                time.sleep(.3)
                send(buttons[0], 0x00F5, 0, 0)
            return
        time.sleep(.2)


def test():
    # loaded fires after pywebview injects the API.
    time.sleep(.5)
    threading.Thread(target=automate_dialog, daemon=True).start()
    window.evaluate_js("window.probeResult=null; pywebview.api.save_export({mode:'search',q:'报销',format:'json',usernames:['synthetic']}).then(r=>window.probeResult=r)")
    deadline = time.monotonic() + 35
    result = None
    while time.monotonic() < deadline:
        result = window.evaluate_js('window.probeResult')
        if result is not None:
            break
        time.sleep(.25)
    evidence = {'bridge_result': result, 'requests': requests,
                'bytes_equal': target.is_file() and target.read_bytes() == payload,
                'api_names': window.evaluate_js('Object.keys(pywebview.api)')}
    threading.Thread(target=automate_dialog, args=('folder',), daemon=True).start()
    window.evaluate_js('window.folderResult=null; pywebview.api.choose_directory().then(r=>window.folderResult=r)')
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        folder = window.evaluate_js('window.folderResult')
        if folder is not None:
            evidence['folder_result'] = folder
            evidence['folder_equal'] = Path(folder['path']).resolve() == args.output.resolve() if folder.get('path') else False
            break
        time.sleep(.25)
    bridge._busy = lambda: True
    window.events.closing += bridge._can_close
    threading.Thread(target=automate_dialog, args=('busy',), daemon=True).start()
    window.destroy()
    evidence['busy_window_still_open'] = window.evaluate_js('document.readyState') == 'complete'
    bridge._busy = lambda: False
    if evidence['bytes_equal']:
        evidence['open_result'] = bridge.open_path(str(target))
    (args.output / 'native-probe.json').write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding='utf-8')
    window.destroy()


check_webview2()
window.events.loaded += lambda: threading.Thread(target=test, daemon=True).start()
webview.start(gui='edgechromium')
server.shutdown()
