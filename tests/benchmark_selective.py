"""Four-shard mixed synthetic workload. Timing and heavy diagnostics are separate.

Run against a frozen working-tree --code-dir and current code with identical flags.
All sources and archives live in TemporaryDirectory and are removed on exit.
"""
import argparse
from contextlib import closing
import ctypes
import hashlib
import inspect
import json
from pathlib import Path
import platform
import sqlite3
import sys
import tempfile
import threading
import time
import tracemalloc

parser = argparse.ArgumentParser()
parser.add_argument('--code-dir', default=str(Path(__file__).resolve().parents[1]))
parser.add_argument('--rows', type=int, default=100000)
parser.add_argument('--small-rows', type=int, default=100)
parser.add_argument('--scope', choices=['small', 'large', 'all'], default='all')
parser.add_argument('--diagnostics', action='store_true')
parser.add_argument('--verify-records', action='store_true')
parser.add_argument('--scenarios', default='full,unchanged,append,historical')
parser.add_argument('--cpu', type=int, default=0, help='Bind benchmark process to this logical CPU')
parser.add_argument('--output', required=True)
args = parser.parse_args()
sys.path.insert(0, args.code_dir)
from archive_store import Archive
import wechat_export as we
import encrypted_sync
import zstandard


def table(u):
    return 'Msg_' + hashlib.md5(u.encode()).hexdigest()


class Memory(ctypes.Structure):
    _fields_ = [('cb', ctypes.c_ulong), ('faults', ctypes.c_ulong),
                ('peak', ctypes.c_size_t), ('working', ctypes.c_size_t),
                ('paged_peak', ctypes.c_size_t), ('paged', ctypes.c_size_t),
                ('nonpaged_peak', ctypes.c_size_t), ('nonpaged', ctypes.c_size_t),
                ('pagefile', ctypes.c_size_t), ('pagefile_peak', ctypes.c_size_t)]


kernel = ctypes.WinDLL('kernel32')
kernel.GetCurrentProcess.restype = ctypes.c_void_p
kernel.SetProcessAffinityMask.argtypes = [ctypes.c_void_p, ctypes.c_size_t]
if not kernel.SetProcessAffinityMask(kernel.GetCurrentProcess(), 1 << args.cpu):
    raise ctypes.WinError()
psapi = ctypes.WinDLL('psapi')
psapi.GetProcessMemoryInfo.argtypes = [ctypes.c_void_p, ctypes.POINTER(Memory), ctypes.c_ulong]


with tempfile.TemporaryDirectory(prefix='wechat-mixed-benchmark-') as tmp:
    root = Path(tmp)
    source = root / 'source'
    (source / 'contact').mkdir(parents=True)
    (source / 'message').mkdir()
    small, large = 'small@chatroom', 'million@chatroom'
    with closing(sqlite3.connect(source / 'contact' / 'contact.db')) as c, c:
        c.execute('CREATE TABLE contact(username TEXT,local_type INTEGER,remark TEXT,nick_name TEXT)')
        c.executemany('INSERT INTO contact VALUES (?,1,?,?)', [(u, u, '') for u in (small, large, 'self', 'other')])
    compressed = zstandard.ZstdCompressor().compress(('synthetic compressed ' + 'x'*200).encode())
    samples = [(1, 'synthetic text ' + 'x'*200), (49, '<msg><title>synthetic link</title><des>synthetic description</des></msg>'),
               (1, compressed), (3, b'synthetic image'), (34, b'synthetic voice')]
    paths = []
    for shard in range(4):
        path = source / 'message' / f'message_{shard}.db'
        paths.append(path)
        with closing(sqlite3.connect(path)) as c, c:
            c.execute('CREATE TABLE Name2Id(user_name TEXT)')
            c.executemany('INSERT INTO Name2Id VALUES (?)', [(u,) for u in ('self', 'other', small, large)])
            for u, size in ((small, args.small_rows), (large, args.rows)):
                c.execute(f'CREATE TABLE "{table(u)}"(local_id INTEGER,server_id INTEGER,local_type INTEGER,real_sender_id INTEGER,create_time INTEGER,message_content BLOB,status INTEGER,sort_seq INTEGER)')
                c.executemany(f'INSERT INTO "{table(u)}" VALUES (?,?,?,?,?,?,0,?)',
                    ((i+1, i+1, samples[i%5][0], 1+i%2, 1700000000+i//10, samples[i%5][1], i%10)
                     for i in range(shard, size, 4)))
    archive = Archive(root / 'archive')
    archive.save_config({'self_username': 'self'})
    selected = [small] if args.scope == 'small' else [large] if args.scope == 'large' else [small, large]
    selective = 'usernames' in inspect.signature(archive.update).parameters
    stats = {'sql_statements': 0, 'parsed': 0, 'source_body_queries': 0, 'signature_bytes': 0}
    original_connect, original_parse, original_ro = archive.connect, we.parse_message, we._connect_ro
    if args.diagnostics:
        original_digest = encrypted_sync.file_digest
        def digest(path):
            stats['signature_bytes'] += path.stat().st_size
            return original_digest(path)
        encrypted_sync.file_digest = digest
        def connect():
            c = original_connect()
            c.set_trace_callback(lambda sql: stats.__setitem__('sql_statements', stats['sql_statements']+1))
            return c
        def parse(*a):
            stats['parsed'] += 1
            return original_parse(*a)
        def source_connect(path):
            c = original_ro(path)
            def trace(sql):
                if sql.startswith('SELECT * FROM "Msg_'):
                    stats['source_body_queries'] += 1
            c.set_trace_callback(trace)
            return c
        archive.connect, we.parse_message, we._connect_ro = connect, parse, source_connect
    results = {'environment': {'python': platform.python_version(), 'sqlite': sqlite3.sqlite_version,
               'platform': platform.platform(), 'diagnostics': args.diagnostics,
               'logical_cpu': args.cpu,
               'rows': args.rows, 'small_rows': args.small_rows, 'scope': args.scope,
               'selective_supported': selective, 'source_bytes': sum(p.stat().st_size for p in paths)}, 'phases': []}
    def measure(label, action):
        for k in stats:
            stats[k] = 0
        peak, stop = [0], threading.Event()
        def sample():
            while not stop.is_set():
                m = Memory(); m.cb = ctypes.sizeof(m)
                psapi.GetProcessMemoryInfo(kernel.GetCurrentProcess(), ctypes.byref(m), m.cb)
                peak[0] = max(peak[0], m.working)
                stop.wait(.02)
        if args.diagnostics:
            monitor = threading.Thread(target=sample)
            monitor.start(); tracemalloc.start()
        started = time.perf_counter()
        cpu_started = time.process_time()
        value = action()
        record = {'phase': label, 'seconds': time.perf_counter()-started,
                  'process_cpu_seconds': time.process_time()-cpu_started}
        if args.diagnostics:
            record.update(stats, python_peak_mib=tracemalloc.get_traced_memory()[1]/1048576,
                          process_peak_mib=peak[0]/1048576)
            tracemalloc.stop(); stop.set(); monitor.join()
        if isinstance(value, dict):
            record['result'] = value
        results['phases'].append(record)
        Path(args.output).write_text(json.dumps(results, indent=2), encoding='utf-8')
        print(json.dumps(record), flush=True)
        return value
    # Baseline had no catalog API: compare the same contact-only operation directly.
    measure('contacts', lambda: we.load_contacts(str(source), include_official=True, only_with_msg=False))
    def discover():
        sessions = set()
        for path in paths:
            with closing(we._connect_ro(str(path))) as c:
                tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                for (u,) in c.execute('SELECT user_name FROM Name2Id'):
                    if table(u) in tables:
                        sessions.add(u)
        return {'sessions': len(sessions)}
    measure('discovery', discover)
    def update():
        return archive.update(source, **({'usernames': selected} if selective else {}))
    appended = False
    for scenario in args.scenarios.split(','):
        if scenario == 'append':
            appended = True
            with closing(sqlite3.connect(paths[0])) as c, c:
                for u, size in ((small, args.small_rows), (large, args.rows)):
                    c.execute(f'INSERT INTO "{table(u)}" VALUES (?,?,1,2,?,?,0,0)', (size+1, size+1, 1800000000, 'synthetic new'))
        if scenario == 'historical':
            with closing(sqlite3.connect(paths[1])) as c, c:
                for u in (small, large):
                    c.execute(f'UPDATE "{table(u)}" SET status=1 WHERE local_id=2')
        measure(scenario, update)
        with closing(archive.connect()) as c:
            for u in (selected if selective else [small, large]):
                expected = (args.small_rows if u == small else args.rows) + int(appended)
                assert c.execute('SELECT count(*) FROM messages WHERE username=?', (u,)).fetchone()[0] == expected
            if selective and args.scope == 'small':
                assert c.execute('SELECT count(*) FROM messages WHERE username=?', (large,)).fetchone()[0] == 0
        if selective and scenario == 'unchanged':
            assert results['phases'][-1]['result']['performance']['parsed_messages'] == 0
            assert results['phases'][-1]['result']['performance']['location_writes'] == 0
    def export():
        u = selected[0]
        messages = archive.messages(u)
        contact = next(c for c in archive.contacts() if c['username'] == u)
        return {'txt_bytes': len(we.render_txt(contact['name'], messages).encode()),
                'json_bytes': len(we.render_json(contact, messages).encode())}
    measure('export_txt_json', export)
    if args.verify_records:
        expected_samples = [original_parse(t, raw) for t, raw in samples]
        verified = 0
        with closing(archive.connect()) as c:
            for u in selected:
                size = args.small_rows if u == small else args.rows
                cursor = c.execute("SELECT message_key,data FROM messages WHERE username=? ORDER BY timestamp,json_extract(data,'$.sort_seq'),json_extract(data,'$.local_id'),message_key", (u,))
                for i, (key, data) in enumerate(cursor):
                    m = json.loads(data)
                    assert key == f'server:{i+1}' and m['local_id'] == i+1
                    if i < size:
                        expected_type, content = expected_samples[i%5]
                        assert m['type_name'] == expected_type and m['content'] == content
                        assert m['sender_username'] == ('self' if i%2 == 0 else 'other')
                        assert m['status'] == int(i == 1 and 'historical' in args.scenarios.split(','))
                        assert m['source_shard'] == f'message_{i%4}.db'
                    else:
                        assert m['content'] == 'synthetic new'
                    verified += 1
        results['verified_records'] = verified
        Path(args.output).write_text(json.dumps(results, indent=2), encoding='utf-8')
