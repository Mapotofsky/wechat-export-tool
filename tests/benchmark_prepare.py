"""Synthetic encrypted file preparation, separate from message-import timing."""
import argparse
import ctypes
import inspect
import json
from pathlib import Path
import sys
import tempfile
import time

parser = argparse.ArgumentParser()
parser.add_argument('--code-dir', default=str(Path(__file__).resolve().parents[1]))
parser.add_argument('--output', required=True)
parser.add_argument('--all-files', action='store_true')
parser.add_argument('--cpu', type=int, default=0)
args = parser.parse_args()
kernel = ctypes.WinDLL('kernel32')
kernel.GetCurrentProcess.restype = ctypes.c_void_p
kernel.SetProcessAffinityMask.argtypes = [ctypes.c_void_p, ctypes.c_size_t]
if not kernel.SetProcessAffinityMask(kernel.GetCurrentProcess(), 1 << args.cpu):
    raise ctypes.WinError()
sys.path.insert(0, args.code_dir)
import encrypted_sync
from test_encrypted_wal import encrypted_page, reserved_pages

with tempfile.TemporaryDirectory(prefix='wechat-prepare-benchmark-') as tmp:
    root = Path(tmp)
    source = root / 'source'
    (source / 'contact').mkdir(parents=True)
    (source / 'message').mkdir()
    pages = reserved_pages(root / 'plain.db', 'synthetic')
    key, salt = b'k'*32, b's'*16
    content = b''.join(encrypted_page(p, i+1, key, salt) for i, p in enumerate(pages))
    # Two valid live pages followed by authenticated empty free pages. SQLite's
    # declared database size remains two pages; quick_check ignores the tail.
    # This isolates page processing cost, not a realistic WeChat size distribution.
    from test_encrypted_wal import encrypt
    import hashlib
    import hmac
    import struct
    def empty_page(pgno):
        iv = b'i'*16
        ciphertext = encrypt(key, iv, b'\0'*4016)
        mac_key = hashlib.pbkdf2_hmac('sha512', key, bytes(x^0x3a for x in salt), 2, dklen=32)
        mac = hmac.new(mac_key, ciphertext+iv+struct.pack('<I', pgno), hashlib.sha512).digest()
        return ciphertext+iv+mac
    large = content + b''.join(empty_page(i) for i in range(3, 259))
    relative = ['contact/contact.db'] + [f'message/message_{i}.db' for i in range(4)] + ['message/media_0.db']
    for rel in relative:
        (source / rel).write_bytes(content if rel.startswith('contact') else large)
    keys = root / 'synthetic-keys.json'
    keys.write_text(json.dumps({r: {'enc_key': key.hex()} for r in relative}))
    selective = 'contacts_only' in inspect.signature(encrypted_sync.sync).parameters
    results = {'synthetic_only': True, 'source_bytes': sum((source/r).stat().st_size for r in relative),
               'contacts_only_supported': selective, 'logical_cpu': args.cpu, 'all_files': args.all_files, 'phases': []}
    previous = None
    for phase in ('prepare_contacts', 'prepare_messages', 'reuse_messages', 'prepare_media'):
        kwargs = {} if not selective else ({'media': True} if args.all_files else
                 {'contacts_only': phase in ('prepare_contacts', 'prepare_media'), 'media': phase == 'prepare_media'})
        start, cpu = time.perf_counter(), time.process_time()
        directory, manifest = encrypted_sync.sync(source, root/'staging', keys, previous, **kwargs)
        count = sum(not x['reused'] for x in manifest.values())
        processed = sum((source/rel).stat().st_size for rel in manifest)
        record = {'phase': phase, 'seconds': time.perf_counter()-start, 'process_cpu_seconds': time.process_time()-cpu,
                  'prepared_files': len(manifest), 'materialized_files': count, 'source_bytes': processed,
                  'snapshot_seconds': sum(x.get('snapshot_seconds', 0) for x in manifest.values()) if selective else None,
                  'prepare_seconds': sum(x.get('prepare_seconds', 0) for x in manifest.values()) if selective else None}
        results['phases'].append(record)
        previous = directory
        Path(args.output).write_text(json.dumps(results, indent=2), encoding='utf-8')
        print(json.dumps(record), flush=True)
