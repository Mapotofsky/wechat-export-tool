"""SQLCipher4 page authentication and SQLite WAL commit materialization.

Format evidence: https://sqlite.org/fileformat.html#the_write_ahead_log
This path is implemented and synthetically tested, not yet verified against a
user-triggered real WeChat encrypted WAL snapshot.
"""
from contextlib import contextmanager, closing
import ctypes
import hashlib
import hmac
import json
from pathlib import Path
import shutil
import sqlite3
import struct
import sys
import uuid
import time

import wcdb_key_tool_windows as cipher
from task_lifecycle import CleanupError
from wechat_export import is_message_shard


def checksum(data, endian, state=(0, 0)):
    a, b = state
    words = struct.unpack(endian + 'I' * (len(data) // 4), data)
    for i in range(0, len(words), 2):
        a = (a + words[i] + b) & 0xffffffff
        b = (b + words[i + 1] + a) & 0xffffffff
    return a, b


def committed_frames(path, progress=lambda *args: None):
    """Return authenticated WAL structure through the last valid commit only."""
    if not path.exists() or path.stat().st_size == 0:
        return [], None
    frames, end, size = [], 0, None
    with path.open('rb') as f:
        header = f.read(32)
        if len(header) != 32:
            raise ValueError('WAL 头不完整，请退出微信后重新更新')
        magic, version, page_size = struct.unpack('>III', header[:12])
        if magic not in (0x377f0682, 0x377f0683) or version != 3007000 or page_size != cipher.PAGE_SZ:
            raise ValueError('原始 WAL 格式不受支持（要求 SQLite WAL / 4096 字节页）')
        endian = '<' if magic == 0x377f0682 else '>'
        state = checksum(header[:24], endian)
        if state != struct.unpack('>II', header[24:]):
            raise ValueError('WAL 头校验失败')
        while True:
            if len(frames) % 256 == 0:
                progress('验证加密 WAL 提交边界', len(frames), 0)
            fh = f.read(24)
            if len(fh) != 24:
                break
            page = f.read(page_size)
            if len(page) != page_size or fh[8:16] != header[16:24]:
                break
            pgno, commit_size = struct.unpack('>II', fh[:8])
            if pgno == 0:
                break
            next_state = checksum(fh[:8] + page, endian, state)
            if next_state != struct.unpack('>II', fh[16:24]):
                break
            state = next_state
            frames.append((pgno, page))
            if commit_size:
                end, size = len(frames), commit_size
    return frames[:end], size


def authenticate(page, pgno, mac_key):
    if len(page) != cipher.PAGE_SZ:
        raise ValueError('加密数据库存在不完整页面')
    offset = cipher.SALT_SZ if pgno == 1 else 0
    data = page[offset:cipher.PAGE_SZ - cipher.RESERVE_SZ + cipher.IV_SZ]
    actual = hmac.new(mac_key, data + struct.pack('<I', pgno), hashlib.sha512).digest()
    if not hmac.compare_digest(actual, page[-cipher.HMAC_SZ:]):
        raise ValueError(f'加密页面认证失败（页 {pgno}）；请重新提取密钥或稳定源库后重试')


def materialize(source, target, key, progress=lambda *args: None):
    """Write only into an unpublished generation, never over an existing archive."""
    target.parent.mkdir(parents=True, exist_ok=True)
    with source.open('rb') as f:
        first = f.read(cipher.PAGE_SZ)
    mac_key = hashlib.pbkdf2_hmac('sha512', key, bytes(x ^ 0x3a for x in first[:16]), 2, dklen=32)
    with source.open('rb') as f, target.open('wb') as out:
        pgno = 1
        total_pages = source.stat().st_size // cipher.PAGE_SZ
        progress('解密主数据库页面', 0, total_pages)
        while page := f.read(cipher.PAGE_SZ):
            authenticate(page, pgno, mac_key)
            out.write(cipher._decrypt_page(key, page, pgno))
            pgno += 1
            if pgno % 256 == 0:
                progress('解密主数据库页面', pgno - 1, total_pages)
    progress('验证加密 WAL 提交边界', 0, 0)
    frames, size = committed_frames(Path(str(source) + '-wal'), progress)
    with target.open('r+b') as out:
        for index, (pgno, page) in enumerate(frames):
            if index % 256 == 0:
                progress('解密 WAL 页面', index, len(frames))
            authenticate(page, pgno, mac_key)
            if pgno <= size:
                out.seek((pgno - 1) * cipher.PAGE_SZ)
                out.write(cipher._decrypt_page(key, page, pgno))
        if size is not None:
            out.truncate(size * cipher.PAGE_SZ)
        # The standalone output has no WAL. Preserve the 80 reserved bytes per page.
        out.seek(18)
        out.write(b'\x01\x01')
    progress('检查明文数据库一致性', 0, 0)
    with closing(sqlite3.connect(f'{target.as_uri()}?mode=ro', uri=True)) as c:
        try:
            valid = all(row[0] == 'ok' for row in c.execute('PRAGMA quick_check'))
        except sqlite3.Error as exc:
            raise RuntimeError(f'{target.name} 一致性检查无法执行（{exc}）；本次数据未保存') from exc
        if not valid:
            raise ValueError(f'{target.name} 解密/WAL 合并后 SQLite 一致性检查失败，本次数据未保存')
    return {'committed_frames': len(frames), 'wal_present': Path(str(source) + '-wal').exists(),
            'real_wechat_validation': 'not_performed'}


@contextmanager
def stable_files(paths):
    """Pin offline files or take SQLite's Windows WAL transaction locks.

    Open writable handles are normal for an idle SQLite connection. They do not
    imply an active writer. Lock offsets follow SQLite os_win.c WIN_SHM_BASE=120
    and wal.c write/checkpoint/recovery slots 0..2, not the database encryption.
    """
    if sys.platform != 'win32':
        yield
        return
    from ctypes import wintypes as wt
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateFileW.argtypes = [wt.LPCWSTR, wt.DWORD, wt.DWORD, wt.LPVOID, wt.DWORD, wt.DWORD, wt.HANDLE]
    kernel.CreateFileW.restype = wt.HANDLE
    kernel.CloseHandle.argtypes = [wt.HANDLE]
    class Overlapped(ctypes.Structure):
        _fields_ = [('Internal', ctypes.c_size_t), ('InternalHigh', ctypes.c_size_t),
                    ('Offset', wt.DWORD), ('OffsetHigh', wt.DWORD), ('hEvent', wt.HANDLE)]
    kernel.LockFileEx.argtypes = [wt.HANDLE, wt.DWORD, wt.DWORD, wt.DWORD, wt.DWORD, ctypes.POINTER(Overlapped)]
    kernel.LockFileEx.restype = wt.BOOL
    kernel.UnlockFileEx.argtypes = [wt.HANDLE, wt.DWORD, wt.DWORD, wt.DWORD, ctypes.POINTER(Overlapped)]
    kernel.UnlockFileEx.restype = wt.BOOL
    handles = []
    locks = []
    def release():
        for handle, length, overlap in reversed(locks):
            kernel.UnlockFileEx(handle, 0, length, 0, ctypes.byref(overlap))
        locks.clear()
        for handle in reversed(handles):
            kernel.CloseHandle(handle)
        handles.clear()
    def open_file(path, sharing):
        handle = kernel.CreateFileW(str(path), 0x80000000, sharing, None, 3, 0, None)
        if handle == ctypes.c_void_p(-1).value:
            raise OSError(ctypes.get_last_error(), path.name)
        handles.append(handle)
        return handle
    def lock_file(handle, offset, length, exclusive):
        overlap = Overlapped()
        overlap.Offset, overlap.OffsetHigh = offset & 0xffffffff, offset >> 32
        if not kernel.LockFileEx(handle, 1 | (2 if exclusive else 0), 0, length, 0, ctypes.byref(overlap)):
            raise OSError(ctypes.get_last_error(), 'SQLite transaction lock')
        locks.append((handle, length, overlap))
    paths = [Path(p) for p in paths]
    try:
        try:
            for path in paths:
                open_file(path, 1)
        except OSError as exc:
            release()
            if exc.errno != 32:
                raise RuntimeError(f'快照文件无法读取（Windows 错误 {exc.errno}，文件 {exc.strerror}）；请检查目录与访问权限') from exc
            # A live WAL connection is protected by its actual transaction locks.
            databases = [p for p in paths if p.suffix == '.db']
            for attempt in range(20):
                try:
                    for db in databases:
                        shm = Path(str(db) + '-shm')
                        if not shm.exists():
                            # Offline databases within a mixed live/offline set still use deny-write.
                            open_file(db, 1)
                            wal = Path(str(db) + '-wal')
                            if wal.exists():
                                open_file(wal, 1)
                            continue
                        db_handle = open_file(db, 3)  # allow read/write handles, pin against deletion
                        lock_file(db_handle, 0x40000002, 510, False)  # SQLite SHARED range
                        shm_handle = open_file(shm, 3)
                        lock_file(shm_handle, 128, 1, False)  # shared deadman: prevent WAL-index reset
                        lock_file(shm_handle, 120, 3, True)  # writer, checkpoint, recovery
                        wal = Path(str(db) + '-wal')
                        if wal.exists():
                            open_file(wal, 3)
                    break
                except OSError as lock_error:
                    release()
                    if lock_error.errno not in (32, 33) or attempt == 19:
                        if lock_error.errno in (32, 33):
                            raise RuntimeError(f'微信正在写入数据库（Windows 错误 {lock_error.errno}）。请稍后点击重试；持续占用时再退出微信。') from lock_error
                        raise RuntimeError(f'无法读取或锁定快照文件（Windows 错误 {lock_error.errno}）；请检查访问权限与目录是否发生变化') from lock_error
                    time.sleep(.05)
        yield
    finally:
        release()


def file_digest(path, progress=lambda *args: None):
    digest = hashlib.sha256()
    with path.open('rb') as f:
        while block := f.read(1024 * 1024):
            progress('核对文件内容', 0, 0)
            digest.update(block)
    return digest.hexdigest()


class KeyPreparationError(ValueError):
    """The selected key file cannot prepare the requested databases."""


def preparation_files(source, contacts_only=False, media=False):
    source = Path(source)
    return sorted([source / 'contact' / 'contact.db'] +
                  ([] if contacts_only else [p for p in (source / 'message').glob('message_*.db') if is_message_shard(p.name)]) +
                  (list((source / 'message').glob('media_*.db')) if media else []))


def obsolete_generations(account_dir, config):
    """Only app-owned generations no longer referenced by published settings."""
    root = (Path(account_dir) / 'decrypted').resolve()
    retained = {Path(config[key]).resolve() for key in
                ('decrypted_dir', 'message_snapshot', 'media_snapshot') if config.get(key)}
    if not root.is_dir():
        return []
    return [path for path in root.iterdir()
            if len(path.name) == 32 and all(c in '0123456789abcdef' for c in path.name)
            and path.is_dir() and not path.is_symlink() and path.resolve().parent == root
            and path.resolve() not in retained]


def prune_generations(account_dir, config):
    for path in obsolete_generations(account_dir, config):
        try:
            shutil.rmtree(path)
        except OSError as exc:
            raise CleanupError(path, True, exc) from exc


def sync(source, account_dir, keys_file, previous=None, progress=lambda *args: None, contacts_only=False, media=False):
    generation = Path(account_dir) / 'decrypted' / uuid.uuid4().hex
    generation.mkdir(parents=True)
    try:
        return _sync(source, account_dir, keys_file, previous, progress, contacts_only, media, generation)
    except BaseException:
        try:
            shutil.rmtree(generation)
        except OSError as exc:
            raise CleanupError(generation, True, exc) from exc
        raise


def _sync(source, account_dir, keys_file, previous, progress, contacts_only, media, generation):
    source, account_dir = Path(source), Path(account_dir)
    try:
        raw_keys = json.loads(Path(keys_file).read_text(encoding='utf-8'))
        if not isinstance(raw_keys, dict):
            raise ValueError('expected object')
        keys = cipher._strip_key_metadata(raw_keys)
    except (ValueError, OSError):
        raise KeyPreparationError('密钥文件无法读取或不是有效 JSON，请重新选择密钥文件或提取密钥') from None
    files = preparation_files(source, contacts_only, media)
    if not files or not files[0].exists() or not list((source / 'message').glob('message_*.db')):
        raise ValueError('目录需要包含 contact/contact.db 与 message/message_*.db')
    manifest, reusable = {}, []
    previous_dirs = previous if isinstance(previous, (list, tuple)) else [previous]
    for directory in previous_dirs:
        if directory and (Path(directory) / 'sync.json').exists():
            reusable.append((Path(directory), json.loads((Path(directory) / 'sync.json').read_text(encoding='utf-8'))))
    paths = [p for db in files for p in (db, Path(str(db) + '-wal')) if p.exists()]
    snapshot_started = time.perf_counter()
    with stable_files(paths):
        snapshot_bytes = sum(p.stat().st_size for p in paths)
        # Copy main databases and WALs while all snapshot locks remain held.
        snapshot = generation / 'source_snapshot'
        for index, db in enumerate(files):
            progress('稳定快照', index, len(files))
            rel = db.relative_to(source)
            dst = snapshot / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            copy_chunks(db, dst, progress)
            wal = Path(str(db) + '-wal')
            if wal.exists():
                copy_chunks(wal, Path(str(dst) + '-wal'), progress)
    snapshot_seconds = time.perf_counter() - snapshot_started
    for index, db in enumerate(files):
        file_started = time.perf_counter()
        progress('解密与 WAL 合并', index, len(files))
        rel = db.relative_to(source)
        src, dst = snapshot / rel, generation / rel
        wal = Path(str(src) + '-wal')
        signature = [file_digest(src, progress), file_digest(wal, progress) if wal.exists() else None]
        key_info = cipher._get_key_info(keys, str(rel))
        if not key_info:
            raise KeyPreparationError(f'缺少 {rel} 的密钥，请重新提取密钥或选择包含该库的密钥文件')
        # Key authentication is done before reusing an existing decrypted snapshot.
        try:
            key = bytes.fromhex(key_info['enc_key'])
            if len(key) != 32:
                raise ValueError('invalid key length')
        except (ValueError, KeyError, TypeError):
            raise KeyPreparationError('密钥内容格式无效，请重新选择密钥文件或提取密钥') from None
        with src.open('rb') as f:
            if not cipher.verify_enc_key(key, f.read(cipher.PAGE_SZ)):
                raise KeyPreparationError('当前密钥未通过所选数据库认证，请核对目录并重新提取或选择密钥文件')
        cached = next(((directory, old[str(rel)]) for directory, old in reusable
                       if old.get(str(rel), {}).get('signature') == signature and (directory / rel).exists()), None)
        if cached:
            dst.parent.mkdir(parents=True, exist_ok=True)
            copy_chunks(cached[0] / rel, dst, progress)
            info = dict(cached[1], reused=True)
        else:
            info = dict(materialize(src, dst, key, progress), signature=signature, reused=False)
        manifest[str(rel)] = info
        info.update(prepare_seconds=time.perf_counter() - file_started,
                    source_bytes=src.stat().st_size + (wal.stat().st_size if wal.exists() else 0),
                    snapshot_seconds=snapshot_seconds if index == 0 else 0,
                    snapshot_bytes=snapshot_bytes if index == 0 else 0)
        # These are new, private staging copies, not user-owned source/decrypted files.
        src.unlink()
        if wal.exists():
            wal.unlink()
    (generation / 'sync.json').write_text(json.dumps(manifest, indent=2), encoding='utf-8')
    return generation, manifest


def copy_chunks(source, target, progress):
    total = source.stat().st_size
    done = 0
    with source.open('rb') as src, target.open('wb') as dst:
        while block := src.read(1024 * 1024):
            progress('复制快照', done, total)
            dst.write(block)
            done += len(block)
    progress('复制快照', done, total)
