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

from runtime_paths import data_root, logs_root, package_root, python_executable, check_writable


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
            self.file.seek(1)
            self.file.truncate()
            self.file.write(json.dumps({'pid': os.getpid(), 'program': str(Path(__file__).resolve())}).encode('utf-8'))
            self.file.flush()
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
            raise RuntimeError(f'本机服务启动失败，请检查 {logs_root() / "desktop.log"}')
        try:
            with urllib.request.urlopen(url + '/api/data-status', timeout=1) as response:
                if response.status == 200:
                    return
        except (OSError, urllib.error.URLError):
            time.sleep(.1)
    raise RuntimeError(f'等待本机服务就绪超时，请检查 {logs_root() / "desktop.log"}')


class NativeBridge:
    def __init__(self, base_url, busy, manager=None):
        self._base_url = base_url
        self._busy = busy
        self._window = None
        self._last_saved = None
        self._saving = False
        self._state_lock = threading.RLock()
        self._closing = False
        self._manager = manager
        self._exit_waiting = False
        self._save_stage = ''

    def choose_directory(self):
        import webview
        selected = self._window.create_file_dialog(webview.FileDialog.FOLDER)
        return {'path': str(selected[0]) if selected else None}

    def save_export(self, request):
        """Download bytes from the same API used by the browser, then save natively."""
        import webview
        with self._state_lock:
            if self._saving or self._closing:
                return {'saved': False, 'error': '正在保存导出文件或退出'}
            self._saving = True
            self._save_stage = '正在生成导出文件'
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
            self._save_stage = '等待选择保存位置'
            selected = self._window.create_file_dialog(webview.FileDialog.SAVE, save_filename=Path(filename).name)
            if not selected:
                return {'saved': False}
            path = Path(selected[0] if isinstance(selected, (tuple, list)) else selected)
            self._save_stage = '正在写入导出文件'
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
            with self._state_lock:
                self._saving = False
                self._save_stage = ''

    def native_state(self):
        with self._state_lock:
            return {'saving': self._saving, 'save_stage': self._save_stage, 'exit_waiting': self._exit_waiting}

    def exit_action(self, action):
        if action == 'minimize':
            return self.minimize()
        if action != 'stop':
            return False
        with self._state_lock:
            if self._saving or not self._manager or getattr(self._manager, 'cleanup', None) or self._exit_waiting:
                return False
            with self._manager.lock:
                task = self._manager.task
                if task:
                    self._manager.cancel(task['id'], task['account_id'])
            self._exit_waiting = True
        def wait_and_close():
            while True:
                with self._state_lock:
                    if not self._busy() and not self._saving:
                        self._closing = True
                        self._exit_waiting = False
                        break
                time.sleep(.1)
            self._window.destroy()
        threading.Thread(target=wait_and_close, daemon=True).start()
        return True

    def open_path(self, path):
        target = Path(path).resolve()
        if self._last_saved is None or target != self._last_saved:
            return {'opened': False, 'error': '请先保存导出文件'}
        try:
            os.startfile(str(target))
            return {'opened': True}
        except OSError as exc:
            return {'opened': False, 'error': str(exc)}

    def open_resource(self, kind):
        targets = {'data': data_root(), 'logs': logs_root()}
        if kind not in targets:
            return {'opened': False, 'error': '未知资源目录'}
        try:
            os.startfile(str(targets[kind]))
            return {'opened': True}
        except OSError as exc:
            return {'opened': False, 'error': f'{targets[kind]}：{exc}'}

    def minimize(self):
        self._window.minimize()
        return True

    def _can_close(self):
        with self._state_lock:
            return self._can_close_locked()

    def _can_close_locked(self):
        if self._busy() or self._saving:
            # Closing is called on the native UI thread. JS evaluation waits for
            # that thread, so dispatch only after returning from the callback.
            threading.Thread(target=self._window.evaluate_js,
                args=("window.dispatchEvent(new Event('archive-close-request'))",), daemon=True).start()
            return False
        self._closing = True
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


def activate_instance(path):
    """Only enumerate windows owned by the process holding this data lock."""
    try:
        with Path(path).open('rb') as stream:
            stream.seek(1)
            owner = json.loads(stream.read().decode('utf-8'))
    except (OSError, ValueError):
        owner = {}
    pid = owner.get('pid')
    windows = []
    if pid:
        user32 = ctypes.windll.user32
        @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
        def visit(hwnd, _):
            process = ctypes.c_ulong()
            user32.GetWindowThreadProcessId(ctypes.c_void_p(hwnd), ctypes.byref(process))
            if process.value == pid and user32.IsWindowVisible(ctypes.c_void_p(hwnd)):
                windows.append(hwnd)
            return True
        user32.EnumWindows(visit, 0)
        if windows:
            handle = ctypes.c_void_p(windows[0])
            user32.ShowWindow(handle, 9)
            if user32.SetForegroundWindow(handle):
                return True
    ctypes.windll.user32.MessageBoxW(None,
        f'此数据目录已有实例：{data_root()}\n所属进程 PID：{pid or "未取得"}\n程序：{owner.get("program", "未取得")}\n'
        '未能激活窗口。若正在启动请稍候；持续无窗时，核对没有维护或导出任务后，可在任务管理器按此 PID 结束该宿主并重启。\n'
        f'日志：{logs_root()}\n可使用包内“诊断启动.cmd”查看启动输出；锁文件存在本身无需删除。', '微信聊天档案', 0x40)
    return False


def main(argv=None):
    parser = argparse.ArgumentParser(description='微信聊天档案桌面工作台')
    parser.add_argument('--smoke-test', action='store_true', help='窗口就绪后自动关闭，仅用于发布验证')
    args = parser.parse_args(argv)
    lock = InstanceLock(data_root() / 'desktop.lock')
    server = None
    thread = None
    stage, target = '初始化数据目录', data_root()
    try:
        check_writable(target)
        stage, target = '初始化日志目录', logs_root()
        check_writable(target)
        stage, target = '创建启动日志', logs_root() / 'desktop.log'
        logging.basicConfig(filename=target, level=logging.INFO,
                            encoding='utf-8', format='%(asctime)s %(levelname)s %(message)s')
        stage, target = '获取实例锁', lock.path
        if not lock.acquire():
            activate_instance(lock.path)
            return 0
        stage, target = '加载桌面运行库', package_root()
        import webview
        import uvicorn
        stage, target = '打开账号与档案', data_root()
        from web.app import app, manager
        stage, target = '检查桌面运行库', package_root() / 'prerequisites'
        check_webview2()
        # Reserve a free loopback socket before starting the service.
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=port,
                                             log_config=None, access_log=False))
        thread = threading.Thread(target=server.run, kwargs={'sockets': [sock]}, daemon=True)
        stage, target = '启动本机服务', logs_root() / 'desktop.log'
        thread.start()
        url = f'http://127.0.0.1:{port}'
        wait_ready(url, thread)
        bridge = NativeBridge(url, manager.busy, manager)
        stage, target = '创建桌面窗口', package_root()
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
        reason = f'系统错误 {getattr(exc, "winerror", None) or exc.errno}：{exc.strerror}' if isinstance(exc, OSError) else f'{type(exc).__name__}：{exc}'
        recovery = ('请释放空间或设置 WECHAT_ARCHIVE_DATA_DIR 指向可写的已有数据目录，再重试。不要删除原数据目录。'
                    if stage in ('初始化数据目录', '初始化日志目录', '创建启动日志', '获取实例锁', '打开账号与档案') else
                    '按上述原因补齐 prerequisites 中的运行库；可用“诊断启动.cmd”查看输出后重试。')
        ctypes.windll.user32.MessageBoxW(None,
            f'{stage}\n位置：{target}\n{reason}\n\n{recovery}', '微信聊天档案启动失败', 0x10)
        return 1
    finally:
        if server:
            server.should_exit = True
        if thread:
            thread.join(timeout=10)
        lock.close()


if __name__ == '__main__':
    raise SystemExit(main())
