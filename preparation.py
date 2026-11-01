"""File selection and measured capacity for the maintenance preparation chain."""
from contextlib import closing
import json
import os
from pathlib import Path
import shutil
import tempfile

import encrypted_sync


def plan(account, archive, action, usernames, targets=None):
    media = False
    if action in ('update', 'update_all'):
        with closing(archive.connect()) as c:
            media = bool(c.execute("SELECT 1 FROM transcripts WHERE status='success' AND username IN (SELECT value FROM json_each(?)) LIMIT 1",
                                   (json.dumps(usernames),)).fetchone())
    if action == 'transcribe':
        voices = archive.voice_plan(usernames, targets)
        media = voices['total'] > voices['skipped']
    needed = action in ('prepare', 'discover', 'update', 'update_all') or (action == 'transcribe' and media)
    files = encrypted_sync.preparation_files(account['directory'], action == 'transcribe', media) if needed and account['mode'] == 'encrypted' else []
    return dict(files=files, media=media, transcribe=action == 'transcribe', sqlite_temp=action in ('update', 'update_all'))


def sqlite_temp_root():
    if os.name == 'nt':
        import ctypes
        buffer = ctypes.create_unicode_buffer(32768)
        if ctypes.windll.kernel32.GetTempPathW(len(buffer), buffer):
            return Path(buffer.value)
    return Path(tempfile.gettempdir())


def volume(path):
    path = Path(path).resolve()
    while not path.exists() and path != path.parent:
        path = path.parent
    # Windows mount points may share a drive letter; disk_usage uses the actual target.
    if os.name == 'nt':
        import ctypes
        buffer = ctypes.create_unicode_buffer(32768)
        if ctypes.windll.kernel32.GetVolumePathNameW(str(path), buffer, len(buffer)):
            return buffer.value
    return path.anchor


def capacity(preparation, archive):
    files = preparation['files']
    copied, output, unknown, names = 0, 0, [], []
    for db in files:
        names.append(str(db))
        try:
            size = db.stat().st_size
            copied += size
            output += size
            wal = Path(str(db) + '-wal')
            if wal.exists():
                size = wal.stat().st_size
                copied += size
                # WAL frame headers are excluded; pages can overwrite earlier pages.
                output += max(0, (size - 32) // (4096 + 24)) * 4096
                names.append(str(wal))
        except OSError as exc:
            unknown.append(f'{db}：{exc}')
    targets = [(archive.directory, copied, copied + output, '聊天记录与数据副本')]
    if preparation['media'] and preparation.get('transcribe'):
        targets.append((Path(tempfile.gettempdir()), 0, None, '语音临时文件'))
    if preparation.get('sqlite_temp'):
        targets.append((sqlite_temp_root(), 0, None, '导入时的临时文件'))
    rows = {}
    for path, necessary, estimate, purpose in targets:
        key = volume(path)
        if key not in rows:
            try:
                free = shutil.disk_usage(key).free
            except OSError:
                free = None
            rows[key] = dict(volume=key, free_bytes=free, necessary_bytes=0, estimated_bytes=0, unknown_growth=False, paths=[], purposes=[])
        row = rows[key]
        row['necessary_bytes'] += necessary
        row['unknown_growth'] = row['unknown_growth'] or estimate is None
        row['estimated_bytes'] += estimate or 0
        row['paths'].append(str(path))
        row['purposes'].append(purpose)
    retained = sum(p.stat().st_size for p in archive.directory.rglob('*') if p.is_file())
    volumes = list(rows.values())
    return dict(files=names, copy_bytes=copied, retained_bytes=retained, volumes=volumes,
                blocked=any(r['free_bytes'] is not None and r['necessary_bytes'] > r['free_bytes'] for r in volumes),
                unknown=unknown + ['尚未计入全部用量：聊天记录增长、数据库临时写入和语音转换。以前保留的数据副本不会自动删除。',
                                   '解密文件按数据库及 WAL 页大小估算，并计入它们与源副本同时存在时的占用。'])


def require_capacity(info):
    if info['blocked']:
        row = next(r for r in info['volumes'] if r['free_bytes'] is not None and r['necessary_bytes'] > r['free_bytes'])
        raise ValueError(f"{row['volume']} 空间不足：仅数据复制就需要 {row['necessary_bytes']} 字节，可用 {row['free_bytes']} 字节。请释放该磁盘空间后重试。数据目录可在设置中查看；如需更换位置，请先退出并保留原数据。")
