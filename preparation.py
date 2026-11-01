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
    targets = [(archive.directory, copied, copied + output, '数据与新快照')]
    if preparation['media'] and preparation.get('transcribe'):
        targets.append((Path(tempfile.gettempdir()), 0, None, '语音临时文件'))
    if preparation.get('sqlite_temp'):
        targets.append((sqlite_temp_root(), 0, None, 'SQLite 导入临时表'))
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
                unknown=unknown + ['档案增长、SQLite WAL／临时写入、语音解码工作空间随运行变化；旧代次继续保留。',
                                   '解密输出按数据库与 WAL 页估计；峰值展示快照和输出同时存在的保守估计。'])


def require_capacity(info):
    if info['blocked']:
        row = next(r for r in info['volumes'] if r['free_bytes'] is not None and r['necessary_bytes'] > r['free_bytes'])
        raise ValueError(f"{row['volume']} 空间不足：必要复制 {row['necessary_bytes']} 字节，可用 {row['free_bytes']} 字节。请释放该卷空间，或退出后用 WECHAT_ARCHIVE_DATA_DIR 指定已有数据所在的可写位置再重试。")
