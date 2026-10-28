"""Runtime-only maintenance cancellation signal."""
import threading


class TaskCancelled(Exception):
    pass


class CleanupError(OSError):
    def __init__(self, path, directory, error):
        super().__init__(f'{path}: {error}')
        self.resource = {'path': str(path), 'directory': directory}


class Cancellation:
    def __init__(self, lock=None):
        self.event = threading.Event()
        self.lock = lock or threading.RLock()

    def check(self):
        if self.event.is_set():
            raise TaskCancelled('已在安全边界停止')

    def publish(self, function):
        with self.lock:
            self.check()
            return function()
