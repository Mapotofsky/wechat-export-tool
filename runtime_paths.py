"""Application resources and writable portable data, independent of the CWD."""
import os
from pathlib import Path
import shutil
import sys


def resource_root():
    return Path(__file__).resolve().parent


def package_root():
    if os.environ.get('WECHAT_ARCHIVE_HOME'):
        return Path(os.environ['WECHAT_ARCHIVE_HOME']).expanduser().resolve()
    root = resource_root()
    return root.parent if root.name == 'app' and (root.parent / 'runtime').is_dir() else root


def data_root():
    return Path(os.environ.get('WECHAT_ARCHIVE_DATA_DIR', package_root() / 'local_data')).expanduser().resolve()


data_dir = data_root


def models_root():
    return package_root() / 'models'


def logs_root():
    return data_root() / 'logs'


def python_executable():
    candidate = package_root() / 'runtime' / 'python.exe'
    if candidate.is_file():
        return candidate
    if getattr(sys, 'frozen', False):
        raise RuntimeError('缺少随包 runtime/python.exe，请重新解压完整程序包')
    return Path(sys.executable)


def voice_python_executable():
    override = os.environ.get('WECHAT_ARCHIVE_VOICE_PYTHON')
    if override:
        return Path(override).expanduser().resolve()
    candidate = package_root() / 'voice-runtime' / 'python.exe'
    if candidate.is_file():
        return candidate
    # A portable base package must not borrow a developer's ASR environment.
    if (package_root() / 'runtime' / 'python.exe').is_file():
        return candidate
    return Path(sys.executable)


def ffmpeg_executable():
    candidate = package_root() / 'tools' / 'ffmpeg.exe'
    return str(candidate) if candidate.is_file() else shutil.which('ffmpeg')
