"""Account-local selective import; exclusively synthetic sources."""
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch

from archive_store import Archive
from maintenance import Manager, identity_candidates
import wechat_export as we
import encrypted_sync
from test_maintenance import seed, shard, table


class SelectiveTests(unittest.TestCase):
    def test_encrypted_contacts_discovery_scoped_update_and_media_cache_end_to_end(self):
        from test_encrypted_wal import reserved_pages, encrypted_page
        import zstandard
        encrypted = self.root / 'encrypted-workflow'
        (encrypted / 'contact').mkdir(parents=True)
        (encrypted / 'message').mkdir()
        key, salt = b'k'*32, b's'*16
        relatives = ['contact/contact.db'] + [f'message/message_{i}.db' for i in range(4)] + ['message/media_0.db']
        samples = [(1, 'synthetic text'), (49, zstandard.ZstdCompressor().compress(b'<msg><title>synthetic title</title></msg>')),
                   (3, b'synthetic image'), (34, b'synthetic voice')]
        for relative in relatives:
            plain = self.root / ('plain-workflow-' + Path(relative).name)
            reserved_pages(plain, 'synthetic')
            with closing(sqlite3.connect(plain)) as c, c:
                c.execute('DROP TABLE sample')
                if relative.startswith('contact'):
                    c.execute('CREATE TABLE contact(username TEXT,alias TEXT,local_type INTEGER,remark TEXT,nick_name TEXT)')
                    c.executemany('INSERT INTO contact VALUES (?,?,1,?,?)',
                        [(self.a, '', 'Synthetic group', ''), ('wxid_self', 'public_id', 'Synthetic self', ''), ('wxid_a', '', 'Synthetic sender', '')])
                elif 'media_' in relative:
                    c.execute('CREATE TABLE Name2Id(user_name TEXT)')
                    c.execute('INSERT INTO Name2Id VALUES (?)', (self.a,))
                    c.execute('CREATE TABLE VoiceInfo(chat_name_id INTEGER,local_id INTEGER,svr_id INTEGER,create_time INTEGER,voice_data BLOB,data_index TEXT)')
                    c.execute('INSERT INTO VoiceInfo VALUES (1,4,10004,1700000003,?,?)', (b'synthetic audio', 'synthetic'))
                else:
                    i = int(Path(relative).stem.split('_')[1])
                    c.execute('CREATE TABLE Name2Id(user_name TEXT)')
                    c.executemany('INSERT INTO Name2Id(rowid,user_name) VALUES (?,?)', [(7, 'wxid_self'), (8, 'wxid_a'), (9, self.a), (10, self.b)])
                    for u in (self.a, self.b):
                        c.execute(f'CREATE TABLE "{table(u)}"(local_id INTEGER,server_id INTEGER,local_type INTEGER,real_sender_id INTEGER,create_time INTEGER,message_content BLOB,status INTEGER)')
                    c.execute(f'INSERT INTO "{table(self.a)}" VALUES (?,?,?,?,?,?,0)',
                              (i+1, 10001+i, samples[i][0], 7+i%2, 1700000000+i, samples[i][1]))
                    c.execute(f'INSERT INTO "{table(self.b)}" VALUES (?,?,1,8,?,?,0)', (100+i, 50001+i, 1700000100+i, 'synthetic excluded'))
            data = plain.read_bytes()
            (encrypted / relative).write_bytes(b''.join(encrypted_page(data[i:i+4096], i//4096+1, key, salt) for i in range(0, len(data), 4096)))
        keys = self.root / 'workflow-synthetic-keys.json'
        keys.write_text(json.dumps({r: {'enc_key': key.hex()} for r in relatives}))
        manager = Manager(self.root / 'encrypted-state')
        self.addCleanup(manager.executor.shutdown)
        manager.select(str(encrypted), 'synthetic encrypted', True)
        archive = manager.archive()
        archive.save_config({'keys_file': str(keys)})
        def run(action):
            manager.submit(action)
            deadline = time.monotonic() + 10
            while manager.busy() and time.monotonic() < deadline:
                time.sleep(.01)
            status = manager.status()['task']
            self.assertEqual(status['status'], 'success', status.get('reason'))
            return status['result']
        with patch.object(we, 'parse_message', side_effect=AssertionError('catalog must not parse bodies')):
            run('prepare')
            self.assertEqual(archive.contacts(), [])
            self.assertIn(self.b, {r['username'] for r in archive.directory_contacts()})
            candidate = identity_candidates(archive.config()['decrypted_dir'], 'public_id')[0]
            self.assertEqual(candidate['username'], 'wxid_self')
            run('discover')
            self.assertIn(self.b, {r['username'] for r in archive.directory_contacts()})
        archive.save_config({'self_username': 'wxid_self', 'followed_usernames': [self.a]})
        self.assertEqual(run('update')['added'], 4)
        self.assertEqual(archive.messages(self.b), [])
        self.assertEqual(archive.messages(self.a)[1]['content'], 'synthetic title')
        archive.save_transcript(self.a, 'server:10004', b'synthetic audio', 'success', text='synthetic speech')
        with patch.object(encrypted_sync, 'materialize', wraps=encrypted_sync.materialize) as materialize:
            result = run('update')
        self.assertEqual([Path(call.args[0]).name for call in materialize.call_args_list], ['media_0.db'])
        self.assertEqual(result['performance']['parsed_messages'], 0)
        self.assertEqual(archive.contacts()[0]['preview'], 'synthetic speech')

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='wechat-selective-test-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / 'source'
        self.a = seed(self.source)
        self.b = 'deleted@chatroom'
        for i in range(1, 4):
            shard(self.source, i, self.a, [(10+i, 110+i, 49, 8, 1700000010+i,
                   '<msg><title>synthetic</title></msg>', 0)])
        for i in range(4):
            with closing(sqlite3.connect(self.source / 'message' / f'message_{i}.db')) as c, c:
                c.execute('INSERT INTO Name2Id VALUES (?,1)', (self.b,))
                c.execute(f'CREATE TABLE "{table(self.b)}" AS SELECT * FROM "{table(self.a)}"')
        self.archive = Archive(self.root / 'archive')
        self.archive.save_config({'self_username': 'wxid_self'})

    def execute(self, shard_index, sql, args=()):
        with closing(sqlite3.connect(self.source / 'message' / f'message_{shard_index}.db')) as c, c:
            c.execute(sql, args)

    def test_contacts_and_discovery_do_not_read_bodies(self):
        with patch.object(we, 'MessageReader', side_effect=AssertionError('reader')), patch.object(we, 'parse_message', side_effect=AssertionError('parse')):
            self.archive.prepare_directory(self.source)
            self.assertIn(self.b, {r['username'] for r in self.archive.directory_contacts()})
            self.archive.prepare_directory(self.source, discover=True)
        directory = {r['username']: r for r in self.archive.directory_contacts()}
        self.assertIn(self.b, directory)
        self.assertFalse(directory[self.a]['imported'])
        self.assertEqual(self.archive.contacts(), [])

    def test_directory_and_snapshot_publish_together(self):
        self.archive.prepare_directory(self.source, settings={'message_snapshot': 'old'})
        before = self.archive.config()
        with closing(self.archive.connect()) as c, c:
            c.execute("CREATE TRIGGER reject_directory BEFORE INSERT ON settings "
                      "WHEN NEW.key='contact_directory' BEGIN SELECT RAISE(ABORT, 'synthetic failure'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.archive.prepare_directory(self.source, discover=True, settings={'message_snapshot': 'new'})
        self.assertEqual(self.archive.config(), before)
        with closing(self.archive.connect()) as c, c:
            c.execute('DROP TRIGGER reject_directory')
        self.archive.prepare_directory(self.source, discover=True, settings={'message_snapshot': 'new'})
        config = self.archive.config()
        self.assertEqual(config['message_snapshot'], 'new')
        self.assertIn(self.b, {row['username'] for row in config['contact_directory']})

    def test_plaintext_media_is_not_read_without_successful_transcripts(self):
        media = self.source / 'message' / 'media_9.db'
        media.write_bytes(b'synthetic deferred media')
        original = encrypted_sync.file_digest
        paths = []
        def digest(path, progress=lambda *args: None):
            paths.append(path.name)
            return original(path, progress)
        with patch.object(encrypted_sync, 'file_digest', side_effect=digest):
            self.archive.update(self.source, usernames=[self.a])
        self.assertNotIn('media_9.db', paths)

    def test_nullable_system_sender_mapping_can_be_reused(self):
        self.execute(0, f'INSERT INTO "{table(self.a)}" VALUES (99,999,10000,NULL,1700000999,?,0)', ('synthetic system',))
        self.archive.update(self.source, usernames=[self.a])
        with patch.object(we, 'parse_message', side_effect=AssertionError('unchanged system body')):
            result = self.archive.update(self.source, usernames=[self.a])
        self.assertEqual(result['performance']['scanned_messages'], 0)

    def test_a_then_changed_source_then_b_is_complete(self):
        self.archive.update(self.source, usernames=[self.a])
        baseline_a = self.archive.config()['conversation_baselines'][self.a]
        self.execute(0, f'UPDATE "{table(self.b)}" SET status=1 WHERE local_id=1')
        self.archive.update(self.source, usernames=[self.b])
        self.assertEqual(len(self.archive.messages(self.b)), 5)
        self.assertEqual(self.archive.messages(self.b)[0]['status'], 1)
        self.assertEqual(self.archive.config()['conversation_baselines'][self.a], baseline_a)
        with closing(self.archive.connect()) as c:
            self.assertEqual(c.execute('SELECT count(*) FROM current_sources WHERE username=?', (self.b,)).fetchone()[0], 5)

    def test_unselected_body_and_sources_are_untouched(self):
        original = we.MessageReader.iter_messages
        calls = []
        def read(reader, username, *args, **kwargs):
            calls.append(username)
            return original(reader, username, *args, **kwargs)
        with patch.object(we.MessageReader, 'iter_messages', read):
            self.archive.update(self.source, usernames=[self.a])
        self.assertEqual(calls, [self.a])
        self.assertEqual(self.archive.messages(self.b), [])
        self.assertNotIn(self.b, self.archive.config()['conversation_baselines'])

    def test_unselected_existing_history_and_baseline_are_untouched(self):
        self.archive.update(self.source, usernames=[self.b])
        before = self.archive.messages(self.b)
        baseline = self.archive.config()['conversation_baselines'][self.b]
        with closing(self.archive.connect()) as c:
            sources = c.execute('SELECT * FROM current_sources WHERE username=?', (self.b,)).fetchall()
        self.execute(0, f'DELETE FROM "{table(self.b)}"')
        self.archive.update(self.source, usernames=[self.a])
        self.assertEqual(self.archive.messages(self.b), before)
        self.assertEqual(self.archive.config()['conversation_baselines'][self.b], baseline)
        with closing(self.archive.connect()) as c:
            self.assertEqual(c.execute('SELECT * FROM current_sources WHERE username=?', (self.b,)).fetchall(), sources)

    def test_new_shard_is_discovered_even_after_unchanged_update(self):
        self.archive.update(self.source, usernames=[self.a])
        shard(self.source, 4, self.a, [(100, 9999, 1, 8, 1700000100, 'synthetic new shard', 0)])
        result = self.archive.update(self.source, usernames=[self.a])
        self.assertEqual(result['added'], 1)
        self.assertEqual(len(self.archive.messages(self.a)), 6)

    def test_four_shard_duplicate_conflict_and_representative_change(self):
        for i in range(1, 4):
            self.execute(i, f'INSERT INTO "{table(self.a)}" VALUES (?,?,1,8,?, ?,0)',
                         (30+i, 102, 1700000001+i, 'world'))
        self.archive.update(self.source, usernames=[self.a])
        self.assertEqual(len(self.archive.messages(self.a)), 5)
        self.execute(0, f'DELETE FROM "{table(self.a)}" WHERE server_id=102')
        self.archive.update(self.source, usernames=[self.a])
        representative = next(m for m in self.archive.messages(self.a) if m['server_id'] == 102)
        self.assertEqual(representative['source_shard'], 'message_1.db')
        baseline = self.archive.config()['conversation_baselines']
        self.execute(2, f'UPDATE "{table(self.a)}" SET message_content=? WHERE server_id=102', ('conflicting synthetic',))
        with self.assertRaises(RuntimeError):
            self.archive.update(self.source, usernames=[self.a])
        self.assertEqual(self.archive.config()['conversation_baselines'], baseline)

    def test_encrypted_prepare_limits_files_and_defers_media(self):
        # Independently generated pages; keys never refer to a real account.
        from test_encrypted_wal import reserved_pages, encrypted_page
        encrypted = self.root / 'encrypted'
        (encrypted / 'contact').mkdir(parents=True)
        (encrypted / 'message').mkdir()
        pages = reserved_pages(self.root / 'tiny.db', 'synthetic')
        key, salt = b'k'*32, b's'*16
        blob = b''.join(encrypted_page(p, i+1, key, salt) for i, p in enumerate(pages))
        relative = ['contact/contact.db'] + [f'message/message_{i}.db' for i in range(4)] + ['message/media_0.db']
        for rel in relative:
            (encrypted / rel).write_bytes(blob)
        keys = self.root / 'synthetic-keys.json'
        keys.write_text(json.dumps({r: {'enc_key': key.hex()} for r in relative}))
        directory, manifest = encrypted_sync.sync(encrypted, self.root / 'stage', keys, contacts_only=True)
        self.assertEqual({r.replace('\\', '/') for r in manifest}, {'contact/contact.db'})
        self.assertEqual(sum(x['snapshot_bytes'] for x in manifest.values()), len(blob))
        message_dir, messages = encrypted_sync.sync(encrypted, self.root / 'stage', keys, directory)
        self.assertEqual(len(messages), 5)
        self.assertNotIn('message/media_0.db', {r.replace('\\', '/') for r in messages})
        media_dir, media = encrypted_sync.sync(encrypted, self.root / 'stage', keys, directory, contacts_only=True, media=True)
        self.assertEqual({r.replace('\\', '/') for r in media}, {'contact/contact.db', 'message/media_0.db'})
        with patch.object(encrypted_sync, 'materialize', side_effect=AssertionError('message cache should survive media preparation')):
            _, reused = encrypted_sync.sync(encrypted, self.root / 'stage', keys, [media_dir, message_dir])
        self.assertTrue(all(x['reused'] for x in reused.values()))

    def test_empty_selection_does_not_import_all(self):
        with patch.object(self.archive, '_source_signatures', side_effect=AssertionError('empty scope must not prepare sources')):
            result = self.archive.update(self.source, usernames=[])
        self.assertEqual(result['performance']['scanned_messages'], 0)
        self.assertEqual(self.archive.contacts(), [])
        self.assertEqual(self.archive.config().get('conversation_baselines', {}), {})
        manager = Manager(self.root / 'state')
        self.addCleanup(manager.executor.shutdown)
        manager.select(str(self.source), 'synthetic', True)
        with self.assertRaises(ValueError):
            manager.submit('update')
        with self.assertRaises(ValueError):
            manager.submit('update', [])

    def test_names_do_not_reparse_and_unrelated_names_do_not_refresh(self):
        self.archive.update(self.source, usernames=[self.a])
        with closing(sqlite3.connect(self.source / 'contact' / 'contact.db')) as c, c:
            c.execute("UPDATE contact SET remark='new synthetic' WHERE username='wxid_b'")
        with patch.object(we, 'parse_message', side_effect=AssertionError('parse')), patch.object(self.archive, '_refresh_senders', side_effect=AssertionError('unrelated refresh')):
            self.assertEqual(self.archive.update(self.source, usernames=[self.a])['performance']['scanned_messages'], 0)
        with closing(sqlite3.connect(self.source / 'contact' / 'contact.db')) as c, c:
            c.execute("UPDATE contact SET remark='changed synthetic' WHERE username='wxid_a'")
        with patch.object(we, 'parse_message', side_effect=AssertionError('parse')):
            self.archive.update(self.source, usernames=[self.a])
        self.assertEqual(self.archive.messages(self.a)[1]['sender'], 'changed synthetic')

    def test_changed_shard_reuses_body_and_updates_fields(self):
        self.archive.update(self.source, usernames=[self.a])
        self.execute(0, f'UPDATE "{table(self.a)}" SET real_sender_id=9 WHERE local_id=2')
        with patch.object(we, 'parse_message', side_effect=AssertionError('unchanged body')):
            result = self.archive.update(self.source, usernames=[self.a])
        self.assertEqual(result['changed'], 1)
        self.assertEqual(self.archive.messages(self.a)[1]['sender_username'], 'wxid_b')

    def test_retained_history_follows_changed_sender_mapping_without_parsing(self):
        self.archive.update(self.source, usernames=[self.a])
        self.execute(0, f'DELETE FROM "{table(self.a)}" WHERE local_id=2')
        self.archive.update(self.source, usernames=[self.a])
        self.execute(0, 'UPDATE Name2Id SET user_name=? WHERE rowid=8', ('wxid_b',))
        with patch.object(we, 'parse_message', side_effect=AssertionError('mapping must not parse')):
            self.archive.update(self.source, usernames=[self.a])
        self.assertEqual(self.archive.messages(self.a)[1]['sender_username'], 'wxid_b')

    def test_group_name_only_reuses_archived_metadata(self):
        self.archive.update(self.source, usernames=[self.a])
        with closing(sqlite3.connect(self.source / 'contact' / 'contact.db')) as c, c:
            c.execute('UPDATE contact SET remark=? WHERE username=?', ('renamed synthetic group', self.a))
        with patch.object(self.archive, 'messages', side_effect=AssertionError('metadata should be reused')):
            self.archive.update(self.source, usernames=[self.a])
        self.assertEqual(self.archive.contacts()[0]['name'], 'renamed synthetic group')

    def test_failure_rolls_back_conversation_baseline_then_retry(self):
        self.archive.update(self.source, usernames=[self.a])
        before = self.archive.config()
        with patch.object(self.archive, 'metadata', side_effect=RuntimeError('synthetic failure')):
            with self.assertRaises(RuntimeError):
                self.archive.update(self.source, usernames=[self.b])
        self.assertEqual(self.archive.config(), before)
        self.assertEqual(self.archive.messages(self.b), [])
        self.archive.update(self.source, usernames=[self.b])
        self.assertEqual(len(self.archive.messages(self.b)), 5)

    def test_legacy_full_baseline_reuse_and_unarchiveable_new_conversation(self):
        self.archive.update(self.source)
        self.archive.save_config({'conversation_baselines': {}})
        with patch.object(we, 'parse_message', side_effect=AssertionError('legacy reuse')):
            result = self.archive.update(self.source, usernames=[self.a])
        self.assertEqual(result['performance']['scanned_messages'], 0)
        with closing(self.archive.connect()) as c, c:
            c.execute('DELETE FROM chats WHERE username=?', (self.b,))
            c.execute('DELETE FROM messages WHERE username=?', (self.b,))
        self.archive.update(self.source, usernames=[self.b])
        self.assertEqual(len(self.archive.messages(self.b)), 5)

    def test_followed_account_isolation_and_unfollow_retains_history(self):
        manager = Manager(self.root / 'state')
        self.addCleanup(manager.executor.shutdown)
        manager.select(str(self.source), 'first', True)
        a = manager.archive()
        a.save_config({'followed_usernames': [self.a]})
        a.update(self.source, usernames=[self.a])
        a.save_config({'followed_usernames': []})
        self.assertEqual(len(a.messages(self.a)), 5)
        other = self.root / 'other-source'
        seed(other)
        manager.select(str(other), 'second', True)
        self.assertEqual(manager.archive().config().get('followed_usernames', []), [])
        self.assertEqual(manager.archive().contacts(), [])

    def test_task_scope_is_frozen_and_explicit_all_finds_deleted_sessions(self):
        manager = Manager(self.root / 'state')
        manager.select(str(self.source), 'synthetic', True)
        archive = manager.archive()
        archive.save_config({'self_username': 'wxid_self', 'followed_usernames': [self.a]})
        original = archive.update
        def change_followed(*args, **kwargs):
            archive.save_config({'followed_usernames': [self.b]})
            return original(*args, **kwargs)
        with patch.object(archive, 'update', side_effect=change_followed):
            task = manager.submit('update')
            manager.executor.shutdown(wait=True)
        self.assertEqual(task['selected_usernames'], [self.a])
        self.assertEqual(len(archive.messages(self.a)), 5)
        self.assertEqual(archive.messages(self.b), [])
        # A separate Manager over this test's own state exercises explicit all.
        other = Manager(manager.root)
        self.addCleanup(other.executor.shutdown)
        other.submit('update_all')
        other.executor.shutdown(wait=True)
        self.assertEqual(other.status()['task']['status'], 'success')
        self.assertEqual(len(other.archive().messages(self.b)), 5)

    def test_maintenance_progress_publication_is_time_throttled(self):
        manager = Manager(self.root / 'state')
        self.addCleanup(manager.executor.shutdown)
        account = manager.select(str(self.source), 'synthetic', True)
        archive = manager.archive()
        archive.save_config({'self_username': 'wxid_self'})
        task = {'status': 'running', 'stage': '准备'}
        def fast_update(db_dir, progress, **kwargs):
            for i in range(1000):
                progress('synthetic progress', i, 1000)
            return {'added': 0}
        with patch.object(archive, 'update', side_effect=fast_update), patch('maintenance.atomic_json') as publish:
            manager.perform(account, archive, task, 'update', [self.a])
        self.assertEqual(task['status'], 'success')
        self.assertLessEqual(publish.call_count, 3)

    def test_removed_shard_cleanup_only_advances_selected_conversation(self):
        self.archive.update(self.source)
        baseline_b = self.archive.config()['conversation_baselines'][self.b]
        (self.source / 'message' / 'message_1.db').unlink()
        self.archive.update(self.source, usernames=[self.a])
        self.assertEqual(self.archive.config()['conversation_baselines'][self.b], baseline_b)
        with closing(self.archive.connect()) as c:
            self.assertEqual(c.execute('SELECT count(*) FROM current_sources WHERE username=?', (self.b,)).fetchone()[0], 5)
        self.archive.update(self.source, usernames=[self.b])
        self.assertEqual(len(self.archive.messages(self.b)), 5)
        with closing(self.archive.connect()) as c:
            self.assertEqual(c.execute('SELECT count(*) FROM current_sources WHERE username=?', (self.b,)).fetchone()[0], 4)


if __name__ == '__main__':
    unittest.main()
