"""Independent synthetic import benchmark; never opens local_data or chat sources."""
import argparse
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
import tracemalloc
import threading
import ctypes

parser = argparse.ArgumentParser()
parser.add_argument('--code-dir', default=str(Path(__file__).resolve().parents[1]))
parser.add_argument('--rows', type=int, default=100000)
parser.add_argument('--scenarios', default='full,unchanged,append,historical')
parser.add_argument('--output')
args = parser.parse_args()
sys.path.insert(0, args.code_dir)
import archive_store
import wechat_export as we

with tempfile.TemporaryDirectory(prefix='wechat-import-bench-') as directory:
    root = Path(directory)
    source = root / 'source'
    (source / 'contact').mkdir(parents=True)
    (source / 'message').mkdir()
    username = 'synthetic@chatroom'
    table = 'Msg_' + hashlib.md5(username.encode()).hexdigest()
    with closing(sqlite3.connect(source / 'contact' / 'contact.db')) as c, c:
        c.execute('CREATE TABLE contact(username TEXT,local_type INTEGER,remark TEXT,nick_name TEXT)')
        c.executemany('INSERT INTO contact VALUES (?,1,?,?)',
                      [(username, 'Synthetic group', ''), ('self', 'Self', ''), ('other', 'Other', '')])
    paths = []
    for shard in range(2):
        path = source / 'message' / f'message_{shard}.db'
        paths.append(path)
        with closing(sqlite3.connect(path)) as c, c:
            c.execute('CREATE TABLE Name2Id(user_name TEXT)')
            c.executemany('INSERT INTO Name2Id VALUES (?)', [('self',), ('other',)])
            c.execute(f'CREATE TABLE "{table}"(local_id INTEGER,server_id INTEGER,local_type INTEGER,real_sender_id INTEGER,create_time INTEGER,message_content TEXT,status INTEGER,sort_seq INTEGER)')
            start, end = (0, args.rows * 3 // 4) if shard == 0 else (args.rows * 3 // 4, args.rows)
            c.executemany(f'INSERT INTO "{table}" VALUES (?,?,1,?,?,?,?,?)',
                          ((i + 1, i + 1, 1 + i % 2, 1700000000 + i // 10,
                            'synthetic text ' + 'x' * 180, 0, i % 10) for i in range(start, end)))
    archive = archive_store.Archive(root / 'archive')
    archive.save_config({'self_username': 'self'})
    original_parse = we.parse_message
    original_get = we.get_messages
    original_metadata = archive.metadata
    stats = {}
    class TimedConnection(sqlite3.Connection):
        def execute(self, *a, **kw):
            stats['sql_api_calls'] += 1
            return super().execute(*a, **kw)
        def executemany(self, *a, **kw):
            stats['sql_api_calls'] += 1
            return super().executemany(*a, **kw)
        def __exit__(self, *exc):
            started = time.perf_counter()
            try:
                return super().__exit__(*exc)
            finally:
                stats['commit_seconds'] += time.perf_counter() - started
    def connect():
        c = sqlite3.connect(archive.path, timeout=30, factory=TimedConnection)
        def trace(sql):
            stats['sql'] += 1
            if sql.startswith('INSERT OR REPLACE INTO locations'):
                stats['location_statements'] += 1
        c.set_trace_callback(trace)
        return c
    def parse(*a):
        started = time.perf_counter()
        result = original_parse(*a)
        stats['parse_seconds'] += time.perf_counter() - started
        stats['parsed'] += 1
        return result
    def get(*a, **kw):
        started = time.perf_counter()
        result = original_get(*a, **kw)
        stats['read_parse_seconds'] += time.perf_counter() - started
        return result
    def metadata(*a, **kw):
        started = time.perf_counter()
        result = original_metadata(*a, **kw)
        stats['metadata_seconds'] += time.perf_counter() - started
        return result
    class Memory(ctypes.Structure):
        _fields_ = [('cb', ctypes.c_ulong), ('faults', ctypes.c_ulong),
                    ('peak', ctypes.c_size_t), ('working', ctypes.c_size_t),
                    ('paged_peak', ctypes.c_size_t), ('paged', ctypes.c_size_t),
                    ('nonpaged_peak', ctypes.c_size_t), ('nonpaged', ctypes.c_size_t),
                    ('pagefile', ctypes.c_size_t), ('pagefile_peak', ctypes.c_size_t)]
    kernel = ctypes.WinDLL('kernel32')
    kernel.GetCurrentProcess.restype = ctypes.c_void_p
    psapi = ctypes.WinDLL('psapi')
    psapi.GetProcessMemoryInfo.argtypes = [ctypes.c_void_p, ctypes.POINTER(Memory), ctypes.c_ulong]
    def sample(stop, peak):
        while not stop.is_set():
            memory = Memory()
            memory.cb = ctypes.sizeof(memory)
            psapi.GetProcessMemoryInfo(kernel.GetCurrentProcess(), ctypes.byref(memory), memory.cb)
            peak[0] = max(peak[0], memory.working)
            stop.wait(.02)
    archive.connect = connect
    we.parse_message = parse
    we.get_messages = get
    archive.metadata = metadata
    results = []
    for scenario in args.scenarios.split(','):
        if scenario == 'append':
            with closing(sqlite3.connect(paths[1])) as c, c:
                c.execute(f'INSERT INTO "{table}" VALUES (?,?,1,2,?,?,0,0)',
                          (args.rows + 1, args.rows + 1, 1700000000 + args.rows, 'synthetic addition'))
        if scenario == 'historical':
            with closing(sqlite3.connect(paths[0])) as c, c:
                c.execute(f'UPDATE "{table}" SET status=1 WHERE local_id=1')
        stats = {'sql': 0, 'parse_seconds': 0, 'parsed': 0, 'location_statements': 0,
                 'read_parse_seconds': 0, 'metadata_seconds': 0, 'commit_seconds': 0, 'sql_api_calls': 0}
        stop, peak_rss = threading.Event(), [0]
        monitor = threading.Thread(target=sample, args=(stop, peak_rss))
        monitor.start()
        tracemalloc.start()
        started = time.perf_counter()
        updates = []
        def progress(stage, done=0, total=0):
            if '已处理' in stage:
                updates.append(time.monotonic())
        result = archive.update(source, progress)
        elapsed = time.perf_counter() - started
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        stop.set()
        monitor.join()
        expected = args.rows + int(scenario in ('append', 'historical') and 'append' in args.scenarios.split(','))
        assert archive.contacts()[0]['total'] == expected
        if updates:
            assert all(b - a >= .49 for a, b in zip(updates, updates[1:]))
        record = {'scenario': scenario, 'rows': args.rows, 'seconds': elapsed,
                          'python_peak_mib': peak / 1048576, 'process_peak_mib': peak_rss[0] / 1048576,
                          'inner_progress_updates': len(updates), **stats, 'result': result}
        results.append(record)
        if args.output:
            Path(args.output).write_text(json.dumps(results, indent=2), encoding='utf-8')
        print(json.dumps(record), flush=True)
