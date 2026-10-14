"""Incremental import semantics and bounded processing on synthetic sources."""
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from archive_store import Archive
from test_maintenance import seed, shard, table
import wechat_export as we


class ImportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / 'source'
        self.user = seed(self.source)
        self.archive = Archive(self.root / 'archive')
        self.archive.save_config({'self_username': 'wxid_self'})
        self.path = self.source / 'message' / 'message_0.db'

    def execute(self, path, sql, params=()):
        with closing(sqlite3.connect(path)) as c, c:
            c.execute(sql, params)

    def test_unchanged_has_no_body_scan_or_location_write(self):
        self.archive.update(self.source)
        with patch.object(we, 'parse_message', side_effect=AssertionError('body parsed')):
            result = self.archive.update(self.source)
        p = result['performance']
        self.assertEqual((p['scanned_messages'], p['location_writes']), (0, 0))
        self.assertEqual(p['sqlite_changes_including_temp'], 1)
        self.assertEqual(result['reused'], 2)

    def test_changed_shard_conflicts_with_skipped_shard_and_retries(self):
        other = shard(self.source, 1, self.user, [(10, 101, 1, 7, 1700000000, 'hello', 0)])
        self.archive.update(self.source)
        before, baseline = self.archive.messages(self.user), self.archive.config()['import_baseline']
        self.execute(other, f'UPDATE "{table(self.user)}" SET message_content=?', ('conflict',))
        with self.assertRaisesRegex(RuntimeError, '冲突'):
            self.archive.update(self.source)
        self.assertEqual(self.archive.messages(self.user), before)
        self.assertEqual(self.archive.config()['import_baseline'], baseline)
        self.execute(other, f'UPDATE "{table(self.user)}" SET message_content=?', ('hello',))
        self.assertEqual(self.archive.update(self.source)['changed'], 0)

    def test_deleted_canonical_duplicate_elects_surviving_source(self):
        shard(self.source, 1, self.user, [(10, 101, 1, 9, 1700000000, 'hello', 0)])
        self.archive.update(self.source)
        self.execute(self.path, f'DELETE FROM "{table(self.user)}" WHERE local_id=1')
        result = self.archive.update(self.source)
        m = self.archive.messages(self.user)[0]
        self.assertEqual(m['source_shard'], 'message_1.db')
        self.assertEqual(m['sender'], '乙')
        self.assertEqual(result['performance']['overlap_rescanned_chats'], 1)
        self.assertEqual(len(self.archive.messages(self.user)), 2)

    def test_new_duplicate_earlier_than_skipped_winner(self):
        shard(self.source, 1, self.user, [(10, 999, 1, 9, 1700000000, 'same', 0)])
        self.archive.update(self.source)
        self.execute(self.path, f'INSERT INTO "{table(self.user)}" VALUES (0,999,1,8,1700000000,?,0)', ('same',))
        self.archive.update(self.source)
        m = next(m for m in self.archive.messages(self.user) if m['server_id'] == 999)
        self.assertEqual(m['source_shard'], 'message_0.db')
        self.assertEqual(m['sender'], '甲')

    def test_promotion_to_skipped_server_merges_transcript_and_locations(self):
        self.execute(self.path, f'UPDATE "{table(self.user)}" SET server_id=0 WHERE local_id=1')
        shard(self.source, 1, self.user, [(10, 777, 1, 7, 1700000000, 'hello', 0)])
        self.archive.update(self.source)
        self.execute(self.path, f'UPDATE "{table(self.user)}" SET server_id=777 WHERE local_id=1')
        self.archive.update(self.source)
        self.assertEqual(len(self.archive.messages(self.user)), 2)
        with closing(self.archive.connect()) as c:
            self.assertEqual(c.execute("SELECT count(*) FROM locations WHERE message_key LIKE 'local:%'").fetchone()[0], 0)

    def test_voice_promotion_preserves_successful_transcript(self):
        self.execute(self.path, f'INSERT INTO "{table(self.user)}" VALUES (3,0,34,8,1700000002,?,0)', ('synthetic voice',))
        self.archive.update(self.source)
        local = self.archive.messages(self.user)[-1]['message_id']
        self.archive.save_transcript(self.user, local, b'synthetic audio', 'success', text='synthetic transcript')
        self.execute(self.path, f'UPDATE "{table(self.user)}" SET server_id=777 WHERE local_id=3')
        with patch('voice_service.find_media', return_value=(b'synthetic audio', None)):
            self.archive.update(self.source)
        message = self.archive.messages(self.user)[-1]
        self.assertEqual(message['message_id'], 'server:777')
        self.assertEqual(message['content'], 'synthetic transcript')
        self.assertEqual(self.archive.contacts()[0]['preview'], 'synthetic transcript')

    def test_media_only_change_invalidates_preview_without_message_scan(self):
        self.execute(self.path, f'INSERT INTO "{table(self.user)}" VALUES (3,103,34,8,1700000002,?,0)', ('synthetic voice',))
        media = self.source / 'message' / 'media_0.db'
        with closing(sqlite3.connect(media)) as c, c:
            c.execute('CREATE TABLE Name2Id(user_name TEXT)')
            c.execute('INSERT INTO Name2Id VALUES (?)', (self.user,))
            c.execute('CREATE TABLE VoiceInfo(chat_name_id INTEGER,local_id INTEGER,svr_id INTEGER,create_time INTEGER,voice_data BLOB,data_index TEXT)')
            c.execute('INSERT INTO VoiceInfo VALUES (1,3,103,1700000002,?,?)', (b'synthetic audio', 'synthetic'))
        self.archive.update(self.source)
        self.archive.save_transcript(self.user, 'server:103', b'synthetic audio', 'success', text='synthetic transcript')
        with patch.object(we, 'parse_message', side_effect=AssertionError('body parsed')):
            self.assertEqual(self.archive.update(self.source)['performance']['scanned_messages'], 0)
        self.assertEqual(self.archive.contacts()[0]['preview'], 'synthetic transcript')
        self.execute(media, 'UPDATE VoiceInfo SET voice_data=?', (b'different synthetic audio',))
        with patch.object(we, 'parse_message', side_effect=AssertionError('body parsed')):
            result = self.archive.update(self.source)
        self.assertEqual(result['performance']['scanned_messages'], 0)
        self.assertEqual(self.archive.contacts()[0]['preview'], '[语音]')

    def test_names_identity_and_retained_history_refresh(self):
        self.archive.update(self.source)
        self.execute(self.path, f'DELETE FROM "{table(self.user)}" WHERE local_id=2')
        self.archive.update(self.source)
        self.execute(self.source / 'contact' / 'contact.db', 'UPDATE contact SET remark=? WHERE username=?', ('新显示名', 'wxid_a'))
        self.archive.update(self.source)
        self.assertEqual(self.archive.messages(self.user)[1]['sender'], '新显示名')
        self.archive.save_config({'self_username': 'wxid_a', 'my_name': '新自己'})
        self.archive.update(self.source)
        messages = self.archive.messages(self.user)
        self.assertEqual(messages[1]['sender'], '新自己')
        self.assertEqual(messages[1]['identity_basis'], 'username_mapping')
        self.assertEqual(messages[0]['sender'], '本人')  # contact display of wxid_self
        metadata = self.archive.contacts()[0]
        self.assertEqual(metadata['recorded_sender_count'], 2)

    def test_baseline_is_import_commit_not_prepare_and_failure_is_atomic(self):
        manifest = {'message/message_0.db': {'signature': ['generation-a', None]},
                    'contact/contact.db': {'signature': ['contact', None]}}
        self.archive.update(self.source, signatures=manifest)
        before = self.archive.config()
        self.execute(self.path, f'UPDATE "{table(self.user)}" SET status=1 WHERE local_id=1')
        newer = {**manifest, 'message/message_0.db': {'signature': ['generation-b', None]}}
        with patch.object(self.archive, 'metadata', side_effect=RuntimeError('failure')):
            with self.assertRaises(RuntimeError):
                self.archive.update(self.source, signatures=newer)
        self.assertEqual(self.archive.config(), before)
        result = self.archive.update(self.source, signatures=newer)
        self.assertEqual(result['changed'], 1)
        self.assertEqual(self.archive.config()['import_baseline']['signatures']['message/message_0.db'], ['generation-b', None])

    def test_failure_after_written_batch_preserves_published_reader_then_retry(self):
        self.archive.update(self.source)
        with closing(sqlite3.connect(self.path)) as c, c:
            c.executemany(f'INSERT INTO "{table(self.user)}" VALUES (?,?,1,8,?,?,0)',
                          ((i, i + 1000, 1700000010 + i, 'synthetic') for i in range(3, 800)))
        baseline = self.archive.config()['import_baseline']
        original = self.archive._import_batch
        calls = []
        def fail_after_first(*args):
            calls.append(1)
            if len(calls) == 2:
                self.assertEqual(len(self.archive.messages(self.user)), 2)
                raise RuntimeError('synthetic failure after writes')
            return original(*args)
        with patch.object(self.archive, '_import_batch', side_effect=fail_after_first):
            with self.assertRaises(RuntimeError):
                self.archive.update(self.source)
        self.assertEqual(self.archive.config()['import_baseline'], baseline)
        self.assertEqual(len(self.archive.messages(self.user)), 2)
        self.archive.update(self.source)
        self.assertEqual(len(self.archive.messages(self.user)), 799)

    def test_source_connections_and_sender_mapping_are_task_local(self):
        original = we._connect_ro
        calls = []
        def connect(path):
            calls.append(Path(path).name)
            return original(path)
        with patch.object(we, '_connect_ro', side_effect=connect):
            self.archive.update(self.source)
        self.assertEqual(calls.count('message_0.db'), 1)
        self.execute(self.path, 'UPDATE Name2Id SET user_name=? WHERE rowid=8', ('wxid_b',))
        self.archive.update(self.source)
        self.assertEqual(self.archive.messages(self.user)[1]['sender_username'], 'wxid_b')

    def test_plaintext_changed_during_import_does_not_publish_signature(self):
        self.execute(self.path, 'PRAGMA journal_mode=WAL')
        self.archive.update(self.source)
        baseline = self.archive.config()['import_baseline']
        original = self.archive.metadata
        def change_source(*args, **kwargs):
            original(*args, **kwargs)
            self.execute(self.path, f'UPDATE "{table(self.user)}" SET status=1 WHERE local_id=1')
        with patch.object(self.archive, 'metadata', side_effect=change_source):
            with self.assertRaisesRegex(RuntimeError, '导入期间发生变化'):
                self.archive.update(self.source)
        self.assertEqual(self.archive.config()['import_baseline'], baseline)
        self.assertEqual(self.archive.messages(self.user)[0]['status'], 0)
        self.archive.update(self.source)
        self.assertEqual(self.archive.messages(self.user)[0]['status'], 1)

    def test_removed_shard_re_elects_canonical_duplicate(self):
        shard(self.source, 1, self.user, [(10, 101, 1, 9, 1700000000, 'hello', 0)])
        self.archive.update(self.source)
        self.path.unlink()  # Independent synthetic source only.
        self.archive.update(self.source)
        self.assertEqual(self.archive.messages(self.user)[0]['source_shard'], 'message_1.db')
        self.assertEqual(len(self.archive.messages(self.user)), 2)

    def test_large_group_matches_sorted_reference_and_chunk_size(self):
        rows = 20000
        second = shard(self.source, 1, self.user, [])
        for parity, path in enumerate((self.path, second)):
            with closing(sqlite3.connect(path)) as c, c:
                c.execute(f'DELETE FROM "{table(self.user)}"')
                c.execute(f'ALTER TABLE "{table(self.user)}" ADD COLUMN sort_seq INTEGER')
                c.executemany(f'INSERT INTO "{table(self.user)}" VALUES (?,?,1,8,?,?,0,?)',
                              ((i, i + 1000, 1700000000 + i // 10, 'synthetic', i % 10) for i in reversed(range(parity, rows, 2))))
        original = self.archive._import_batch
        sizes = []
        def batch(*args):
            sizes.append(len(args[3]))
            return original(*args)
        with patch.object(self.archive, '_import_batch', side_effect=batch):
            self.archive.update(self.source)
        self.assertLessEqual(max(sizes), 256)
        messages = self.archive.messages(self.user)
        self.assertEqual([m['local_id'] for m in messages], list(range(rows)))
        self.assertEqual([m['message_id'] for m in messages], [f'server:{i + 1000}' for i in range(rows)])
        self.assertTrue(all(m['sender'] == '甲' and m['content'] == 'synthetic' for m in messages))
        self.assertEqual(self.archive.contacts()[0]['total'], rows)
        with closing(self.archive.connect()) as c:
            plan = c.execute("EXPLAIN QUERY PLAN SELECT data FROM messages WHERE username=? ORDER BY timestamp DESC,json_extract(data,'$.sort_seq') DESC,json_extract(data,'$.local_id') DESC,message_key DESC LIMIT 1", (self.user,)).fetchall()
        self.assertFalse(any('TEMP B-TREE' in r[3] for r in plan))


if __name__ == '__main__':
    unittest.main()
