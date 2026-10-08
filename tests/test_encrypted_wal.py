"""Encrypted synthetic pages/WAL; these are NOT real WeChat validation."""
from contextlib import closing
import ctypes
import hashlib
import hmac
from pathlib import Path
import sqlite3
import struct
import sys
import tempfile
import unittest
import json
from unittest.mock import patch

import encrypted_sync as sync
import wcdb_key_tool_windows as cipher


def encrypt(key, iv, data):
    from ctypes import wintypes as wt
    bcrypt = cipher._bcrypt
    bcrypt.BCryptEncrypt.argtypes = bcrypt.BCryptDecrypt.argtypes
    algorithm, handle = wt.HANDLE(), wt.HANDLE()
    assert bcrypt.BCryptOpenAlgorithmProvider(ctypes.byref(algorithm), 'AES', None, 0) == 0
    try:
        mode = 'ChainingModeCBC\x00'.encode('utf-16-le')
        assert bcrypt.BCryptSetProperty(algorithm, 'ChainingMode', mode, len(mode), 0) == 0
        assert bcrypt.BCryptGenerateSymmetricKey(algorithm, ctypes.byref(handle), None, 0, key, len(key), 0) == 0
        try:
            output, iv_buffer, size = ctypes.create_string_buffer(len(data)), ctypes.create_string_buffer(iv, len(iv)), wt.ULONG()
            assert bcrypt.BCryptEncrypt(handle, data, len(data), None, iv_buffer, len(iv), output, len(data), ctypes.byref(size), 0) == 0
            return output.raw[:size.value]
        finally:
            bcrypt.BCryptDestroyKey(handle)
    finally:
        bcrypt.BCryptCloseAlgorithmProvider(algorithm, 0)


def reserved_pages(path, value):
    """Relocate tiny SQLite leaf cells to make 80 bytes of reserve per page."""
    with closing(sqlite3.connect(path)) as c, c:
        c.execute('PRAGMA page_size=4096')
        c.execute('CREATE TABLE sample(value TEXT)')
        c.execute('INSERT INTO sample VALUES (?)', (value,))
    data = path.read_bytes()
    pages = []
    for index in range(len(data) // 4096):
        page = bytearray(data[index * 4096:(index + 1) * 4096])
        offset = 100 if index == 0 else 0
        assert page[offset] == 13
        count = struct.unpack('>H', page[offset + 3:offset + 5])[0]
        start = struct.unpack('>H', page[offset + 5:offset + 7])[0]
        page[start - 80:4016] = page[start:4096]
        page[4016:] = b'\x00' * 80
        page[offset + 5:offset + 7] = struct.pack('>H', start - 80)
        for i in range(count):
            position = offset + 8 + i * 2
            cell = struct.unpack('>H', page[position:position + 2])[0]
            page[position:position + 2] = struct.pack('>H', cell - 80)
        if index == 0:
            page[20] = 80
        pages.append(bytes(page))
    path.write_bytes(b''.join(pages))
    with closing(sqlite3.connect(path)) as c:
        assert c.execute('SELECT value FROM sample').fetchone()[0] == value
    return pages


def encrypted_page(page, pgno, key, salt):
    iv = bytes([pgno]) * 16
    offset = 16 if pgno == 1 else 0
    ciphertext = encrypt(key, iv, page[offset:4016])
    mac_key = hashlib.pbkdf2_hmac('sha512', key, bytes(x ^ 0x3a for x in salt), 2, dklen=32)
    mac = hmac.new(mac_key, ciphertext + iv + struct.pack('<I', pgno), hashlib.sha512).digest()
    return (salt if pgno == 1 else b'') + ciphertext + iv + mac


def wal_bytes(frames, endian='<'):
    salt = b'wal_salt'
    header = struct.pack('>IIII', 0x377f0682 if endian == '<' else 0x377f0683, 3007000, 4096, 0) + salt
    state = sync.checksum(header, endian)
    output = header + struct.pack('>II', *state)
    for pgno, size, page in frames:
        fh = struct.pack('>II', pgno, size)
        state = sync.checksum(fh + page, endian, state)
        output += fh + salt + struct.pack('>II', *state) + page
    return output


@unittest.skipUnless(sys.platform == 'win32', 'Uses Windows CNG AES for synthetic encryption')
class EncryptedWalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.key, self.salt = b'k' * 32, b's' * 16
        before = reserved_pages(self.root / 'before.db', 'before')
        after = reserved_pages(self.root / 'after.db', 'after')
        pending = reserved_pages(self.root / 'pending.db', 'uncommitted')
        self.source, self.target = self.root / 'encrypted.db', self.root / 'output.db'
        self.source.write_bytes(b''.join(encrypted_page(p, i + 1, self.key, self.salt) for i, p in enumerate(before)))
        self.after = [encrypted_page(p, i + 1, self.key, self.salt) for i, p in enumerate(after)]
        self.pending = encrypted_page(pending[1], 2, self.key, self.salt)
        self.wal = Path(str(self.source) + '-wal')

    def value(self):
        with closing(sqlite3.connect(self.target)) as c:
            self.assertEqual(c.execute('PRAGMA quick_check').fetchone()[0], 'ok')
            return c.execute('SELECT value FROM sample').fetchone()[0]

    def test_committed_encrypted_pages_merge_and_pending_pages_excluded(self):
        self.wal.write_bytes(wal_bytes([(1, 0, self.after[0]), (2, 2, self.after[1]), (2, 0, self.pending)]))
        result = sync.materialize(self.source, self.target, self.key)
        self.assertEqual(result['committed_frames'], 2)
        self.assertEqual(self.value(), 'after')
        self.assertFalse(Path(str(self.target) + '-wal').exists())
        self.assertEqual(result['real_wechat_validation'], 'not_performed')

    def test_without_commit_keeps_main_database(self):
        self.wal.write_bytes(wal_bytes([(2, 0, self.pending)]))
        sync.materialize(self.source, self.target, self.key)
        self.assertEqual(self.value(), 'before')

    def test_invalid_tail_checksum_salt_and_partial_frame_keep_last_commit(self):
        good = wal_bytes([(2, 2, self.after[1])])
        for kind in ('checksum', 'salt', 'partial'):
            data = bytearray(wal_bytes([(2, 2, self.after[1]), (2, 2, self.pending)]))
            if kind == 'checksum':
                data[-1] ^= 1
            elif kind == 'salt':
                data[len(good) + 8] ^= 1
            else:
                data = data[:-100]
            self.wal.write_bytes(data)
            sync.materialize(self.source, self.target, self.key)
            self.assertEqual(self.value(), 'after')

    def test_wal_page_hmac_failure_is_not_published_as_plaintext(self):
        corrupt = bytearray(self.after[1])
        corrupt[5] ^= 1
        # Recalculate WAL checksum so only the encrypted-page authentication catches it.
        self.wal.write_bytes(wal_bytes([(2, 2, bytes(corrupt))]))
        with self.assertRaisesRegex(ValueError, '认证失败'):
            sync.materialize(self.source, self.target, self.key)

    def test_header_checksum_wrong_key_and_big_endian(self):
        data = bytearray(wal_bytes([(2, 2, self.after[1])]))
        data[24] ^= 1
        self.wal.write_bytes(data)
        with self.assertRaisesRegex(ValueError, '头校验失败'):
            sync.materialize(self.source, self.target, self.key)
        self.wal.write_bytes(wal_bytes([(2, 2, self.after[1])], '>'))
        sync.materialize(self.source, self.target, self.key)
        self.assertEqual(self.value(), 'after')
        with self.assertRaisesRegex(ValueError, '认证失败'):
            sync.materialize(self.source, self.target, b'x' * 32)

    def test_sync_reuses_unchanged_database_and_failed_generation_preserves_previous(self):
        source = self.root / 'account_source'
        (source / 'contact').mkdir(parents=True)
        (source / 'message').mkdir()
        for relative in ('contact/contact.db', 'message/message_0.db'):
            (source / relative).write_bytes(self.source.read_bytes())
        keys = self.root / 'keys.json'
        keys.write_text(json.dumps({r: {'enc_key': self.key.hex()} for r in ('contact/contact.db', 'message/message_0.db')}))
        # Real WeChat also has these auxiliary files. They require no keys and
        # must never reach decryption/quick_check or count as message shards.
        for name in ('message_fts.db', 'message_resource.db'):
            (source / 'message' / name).write_bytes(b'auxiliary database')
        first, manifest = sync.sync(source, self.root / 'archive', keys)
        self.assertEqual(set(manifest), {'contact\\contact.db', 'message\\message_0.db'})
        from maintenance import inspect_directory
        self.assertEqual(inspect_directory(source)['message_shards'], 1)
        self.assertEqual(sum(r['reused'] for r in manifest.values()), 0)
        with patch.object(cipher, '_decrypt_page', side_effect=AssertionError('unchanged source must skip AES')):
            second, manifest = sync.sync(source, self.root / 'archive', keys, first)
        self.assertEqual(sum(r['reused'] for r in manifest.values()), 2)
        before = (second / 'message' / 'message_0.db').read_bytes()
        changed = bytearray((source / 'message' / 'message_0.db').read_bytes())
        changed[5000] ^= 1
        (source / 'message' / 'message_0.db').write_bytes(changed)
        with self.assertRaisesRegex(ValueError, '认证失败'):
            sync.sync(source, self.root / 'archive', keys, second)
        self.assertEqual((second / 'message' / 'message_0.db').read_bytes(), before)

    def test_windows_snapshot_rejects_open_writer_and_accepts_closed_files(self):
        with self.source.open('r+b'):
            with self.assertRaisesRegex(RuntimeError, '正在写入或 checkpoint'):
                with sync.stable_files([self.source]):
                    self.fail('must not read a database with an active write handle')
        with sync.stable_files([self.source]):
            self.assertEqual(self.source.read_bytes()[:16], self.salt)

    def test_live_sqlite_wal_snapshot_allows_idle_open_connection_and_blocks_mutations(self):
        path = self.root / 'live.db'
        writer = sqlite3.connect(path, timeout=0)
        self.addCleanup(writer.close)
        writer.execute('PRAGMA journal_mode=WAL')
        writer.execute('CREATE TABLE sample(value INTEGER)')
        writer.execute('INSERT INTO sample VALUES (1)')
        writer.commit()
        paths = [path, Path(str(path) + '-wal')]
        with sync.stable_files(paths):
            # Idle open handles must not fail, and a normal reader remains usable.
            with closing(sqlite3.connect(path, timeout=0)) as reader:
                self.assertEqual(reader.execute('SELECT count(*) FROM sample').fetchone()[0], 1)
            with self.assertRaises(sqlite3.OperationalError):
                writer.execute('INSERT INTO sample VALUES (2)')
            self.assertNotEqual(writer.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()[0], 0)
        writer.execute('INSERT INTO sample VALUES (2)')
        writer.commit()
        self.assertEqual(writer.execute('SELECT count(*) FROM sample').fetchone()[0], 2)

    def test_live_sqlite_active_transaction_is_reported_and_locks_are_released(self):
        path = self.root / 'busy.db'
        writer = sqlite3.connect(path, timeout=0)
        self.addCleanup(writer.close)
        writer.execute('PRAGMA journal_mode=WAL')
        writer.execute('CREATE TABLE sample(value INTEGER)')
        writer.commit()
        writer.execute('BEGIN IMMEDIATE')
        with self.assertRaisesRegex(RuntimeError, '正在写入或 checkpoint'):
            with sync.stable_files([path, Path(str(path) + '-wal')]):
                self.fail('must reject an active transaction')
        writer.rollback()
        with sync.stable_files([path, Path(str(path) + '-wal')]):
            pass


if __name__ == '__main__':
    unittest.main()
