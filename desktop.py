"""Portable Windows desktop host; business operations stay on the loopback API."""
import argparse
import ctypes
import json
import logging
import os
from pathlib import Path
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import unquote

from runtime_paths import data_root, logs_root, package_root, python_executable


class InstanceLock:
    """The open file lock is released by Windows even after an interrupted process."""
    def __init__(self, path):
        self.path = Path(path)
        self.file = None

    def acquire(self):
        import msvcrt
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.file = self.path.open('a+b')
        if self.path.stat().st_size == 0:
            self.file.write(b'0')
            self.file.flush()
        self.file.seek(0)
        try:
            msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            self.file.close()
            self.file = None
            return False

    def close(self):
        if self.file:
            self.file.close()
            self.file = None


def wait_ready(url, thread, timeout=30):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not thread.is_alive():
            raise RuntimeError('本机服务启动失败，请检查 local_data/logs/desktop.log')
        try:
            with urllib.request.urlopen(url + '/api/data-status', timeout=1) as response:
                if response.status == 200:
                    return
        except (OSError, urllib.error.URLError):
            time.sleep(.1)
    raise RuntimeError('等待本机服务就绪超时，请检查 local_data/logs/desktop.log')


class NativeBridge:
    def __init__(self, base_url, busy):
        self._base_url = base_url
        self._busy = busy
        self._window = None
        self._last_saved = None
        self._saving = False

    def choose_directory(self):
        import webview
        selected = self._window.create_file_dialog(webview.FileDialog.FOLDER)
        return {'path': str(selected[0]) if selected else None}

    def save_export(self, request):
        """Download bytes from the same API used by the browser, then save natively."""
        import webview
        if self._saving:
            return {'saved': False, 'error': '正在保存导出文件'}
        self._saving = True
        try:
            export_format = request.get('format', 'zip')
            extension = export_format if export_format in ('txt', 'json') else 'zip'
            body = dict(request)
            endpoint = '/api/export-search' if body.pop('mode', 'all') == 'search' else '/api/export'
            payload = json.dumps(body).encode('utf-8')
            req = urllib.request.Request(self._base_url + endpoint, data=payload,
                headers={'Content-Type': 'application/json'}, method='POST')
            with urllib.request.urlopen(req, timeout=300) as response:
                content = response.read()
                disposition = response.headers.get('Content-Disposition', '')
            filename = '微信聊天记录.' + extension
            if "filename*=UTF-8''" in disposition:
                filename = unquote(disposition.split("filename*=UTF-8''", 1)[1].split(';', 1)[0])
            selected = self._window.create_file_dialog(webview.FileDialog.SAVE, save_filename=Path(filename).name)
            if not selected:
                return {'saved': False}
            path = Path(selected[0] if isinstance(selected, (tuple, list)) else selected)
            path.write_bytes(content)
            self._last_saved = path.resolve()
            return {'saved': True, 'path': str(self._last_saved)}
        except urllib.error.HTTPError as exc:
            try:
                reason = json.loads(exc.read()).get('detail', str(exc))
            except (ValueError, OSError):
                reason = str(exc)
            return {'saved': False, 'error': reason}
        except (OSError, ValueError) as exc:
            return {'saved': False, 'error': str(exc)}
        finally:
            self._saving = False

    def open_path(self, path):
        target = Path(path).resolve()
        if self._last_saved is None or target != self._last_saved:
            return {'opened': False, 'error': '请先保存导出文件'}
        try:
            os.startfile(str(target))
            return {'opened': True}
        except OSError as exc:
            return {'opened': False, 'error': str(exc)}

    def minimize(self):
        self._window.minimize()
        return True

    def _can_close(self):
        if self._busy() or self._saving:
            self._window.create_confirmation_dialog('任务仍在运行', '请等待任务完成后退出；可以最小化窗口继续处理。')
            return False
        return True


def check_webview2():
    """Explicitly check EdgeChromium; never silently select the legacy engine."""
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r'SOFTWARE\Microsoft\NET Framework Setup\NDP\v4\Full') as key:
            release = winreg.QueryValueEx(key, 'Release')[0]
        if release < 394802:
            raise OSError('requires .NET Framework 4.6.2')
    except OSError as exc:
        raise RuntimeError('需要 .NET Framework 4.6.2 或更新版本，请查看 prerequisites/说明.txt') from exc
    client = r'SOFTWARE\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}'
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        for view in (winreg.KEY_WOW64_32KEY, winreg.KEY_WOW64_64KEY):
            try:
                with winreg.OpenKey(hive, client, 0, winreg.KEY_READ | view) as key:
                    version = winreg.QueryValueEx(key, 'pv')[0]
                if version and version != '0.0.0.0':
                    return
            except OSError:
                pass
    raise RuntimeError('未检测到 Microsoft Edge WebView2 Runtime。请安装 prerequisites 中的离线安装程序后重试。')


def main(argv=None):
    parser = argparse.ArgumentParser(description='微信聊天档案桌面工作台')
    parser.add_argument('--smoke-test', action='store_true', help='窗口就绪后自动关闭，仅用于发布验证')
    args = parser.parse_args(argv)
    logs_root().mkdir(parents=True, exist_ok=True)
    logging.basicConfig(filename=logs_root() / 'desktop.log', level=logging.INFO,
                        encoding='utf-8', format='%(asctime)s %(levelname)s %(message)s')
    lock = InstanceLock(data_root() / 'desktop.lock')
    if not lock.acquire():
        ctypes.windll.user32.MessageBoxW(None, '微信聊天档案已经运行，请切换到已有窗口。', '微信聊天档案', 0x40)
        return 0
    server = None
    thread = None
    try:
        import webview
        import uvicorn
        from web.app import app, manager
        check_webview2()
        # Reserve a free loopback socket before starting the service.
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=port,
                                             log_config=None, access_log=False))
        thread = threading.Thread(target=server.run, kwargs={'sockets': [sock]}, daemon=True)
        thread.start()
        url = f'http://127.0.0.1:{port}'
        wait_ready(url, thread)
        bridge = NativeBridge(url, manager.busy)
        window = webview.create_window('微信聊天档案', url, js_api=bridge, width=1366, height=860,
                                       min_size=(900, 600))
        bridge._window = window
        window.events.closing += bridge._can_close
        if args.smoke_test:
            def smoke():
                result = subprocess.run([str(python_executable()), '-c',
                    'import sys,sqlite3,zstandard; print(sys.executable)'],
                    capture_output=True, text=True, timeout=20,
                    creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                (logs_root() / 'smoke-test.json').write_text(json.dumps({
                    'url': url, 'subprocess_returncode': result.returncode,
                    'subprocess_stdout': result.stdout, 'subprocess_stderr': result.stderr,
                    'package_root': str(package_root()), 'data_root': str(data_root())}, ensure_ascii=False), encoding='utf-8')
                time.sleep(2)
                window.destroy()
            window.events.loaded += lambda: threading.Thread(target=smoke, daemon=True).start()
        webview.start(gui='edgechromium', debug=False, private_mode=True)
        return 0
    except Exception as exc:
        logging.exception('桌面启动失败')
        ctypes.windll.user32.MessageBoxW(None, str(exc), '微信聊天档案启动失败', 0x10)
        return 1
    finally:
        if server:
            server.should_exit = True
        if thread:
            thread.join(timeout=10)
        lock.close()


if __name__ == '__main__':
    raise SystemExit(main())
