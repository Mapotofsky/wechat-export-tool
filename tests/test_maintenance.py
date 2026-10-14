"""Synthetic account, persistence, identity and local voice behavior tests."""
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock, patch
import asyncio
import io
import zipfile
import threading

from archive_store import Archive
from maintenance import Manager, identity_candidates, atomic_json
import voice_service
import wechat_export as we
from web import app as web


def seed(root, username='test@chatroom'):
    root = Path(root)
    (root / 'contact').mkdir(parents=True)
    (root / 'message').mkdir()
    with closing(sqlite3.connect(root / 'contact' / 'contact.db')) as c, c:
        c.execute('CREATE TABLE contact(username TEXT,alias TEXT,local_type INTEGER,remark TEXT,nick_name TEXT)')
        c.executemany('INSERT INTO contact VALUES (?,?,?,?,?)',
                      [(username, '', 1, '测试聊天', ''), ('wxid_self', 'public_id', 1, '本人', ''),
                       ('wxid_a', '', 1, '甲', ''), ('wxid_b', '', 1, '乙', '')])
    shard(root, 0, username, [(1, 101, 1, 7, 1700000000, 'hello', 0),
                              (2, 102, 1, 8, 1700000001, 'world', 0)])
    return username


def table(username):
    return 'Msg_' + hashlib.md5(username.encode()).hexdigest()


def shard(root, index, username, rows):
    path = Path(root) / 'message' / f'message_{index}.db'
    with closing(sqlite3.connect(path)) as c, c:
        c.execute('CREATE TABLE Name2Id(user_name TEXT,is_session INTEGER)')
        c.executemany('INSERT INTO Name2Id(rowid,user_name,is_session) VALUES (?,?,1)',
                      [(7, 'wxid_self'), (8, 'wxid_a'), (9, 'wxid_b')])
        c.execute(f'CREATE TABLE "{table(username)}"(local_id INTEGER,server_id INTEGER,local_type INTEGER,'
                  'real_sender_id INTEGER,create_time INTEGER,message_content TEXT,status INTEGER)')
        c.executemany(f'INSERT INTO "{table(username)}" VALUES (?,?,?,?,?,?,?)', rows)
    return path


class MaintenanceTests(unittest.TestCase):
    def test_state_publication_retries_transient_permission_error(self):
        target = self.root / 'progress.json'
        atomic_json(target, {'done': 1})
        replace = Path.replace
        calls = []
        def busy_once(path, destination):
            calls.append(1)
            if len(calls) == 1:
                self.assertEqual(json.loads(target.read_text())['done'], 1)
                raise PermissionError(13, 'Access denied', str(destination))
            return replace(path, destination)
        with patch.object(Path, 'replace', busy_once):
            atomic_json(target, {'done': 2})
        self.assertEqual(len(calls), 2)
        self.assertEqual(json.loads(target.read_text())['done'], 2)

    @unittest.skipUnless(__import__('sys').platform == 'win32', 'Windows sharing semantics')
    def test_state_publication_survives_real_windows_reader(self):
        target = self.root / 'progress.json'
        atomic_json(target, {'done': 1})
        reader = target.open('rb')
        timer = threading.Timer(.15, reader.close)
        timer.start()
        try:
            atomic_json(target, {'done': 2})
        finally:
            timer.join()
            reader.close()
        self.assertEqual(json.loads(target.read_text())['done'], 2)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / 'source'
        self.username = seed(self.source)
        self.archive = Archive(self.root / 'account')
        self.archive.save_config({'self_username': 'wxid_self', 'my_name': '我'})

    def test_identity_three_paths_and_alias_resolution(self):
        path = self.source / 'message' / 'message_0.db'
        self.assertEqual(we.detect_self_sender_id(str(path), 'wxid_self', True), (7, 'username_mapping'))
        self.assertEqual(identity_candidates(self.source, 'public_id')[0]['username'], 'wxid_self')
        self.assertEqual(identity_candidates(self.source, 'not_an_internal_id'), [])
        with closing(sqlite3.connect(path)) as c, c:
            c.execute(f'CREATE TABLE "{table("filehelper")}"(local_type INTEGER,real_sender_id INTEGER)')
            c.executemany(f'INSERT INTO "{table("filehelper")}" VALUES (?,?)', [(1, 3), (10000, 9)])
        self.assertEqual(we.detect_self_sender_id(str(path), 'wxid_self', True), (3, 'filehelper'))
        with closing(sqlite3.connect(path)) as c, c:
            c.execute(f'DROP TABLE "{table("filehelper")}"')
            c.execute(f'UPDATE "{table(self.username)}" SET real_sender_id=4 WHERE local_id=1')
        self.assertEqual(we.detect_self_sender_id(str(path), None, True), (4, 'heuristic_4'))
        with closing(sqlite3.connect(path)) as c, c:
            c.execute(f'UPDATE "{table(self.username)}" SET real_sender_id=2')
        self.assertEqual(we.detect_self_sender_id(str(path), None, True), (1, 'heuristic_1'))

    def test_auxiliary_databases_excluded_and_sqlite_failure_keeps_stage(self):
        for name in ('message_fts.db', 'message_resource.db'):
            (self.source / 'message' / name).write_bytes(b'not a message shard')
        self.archive.update(self.source)
        self.assertEqual(len(self.archive.messages(self.username)), 2)
        manager = Manager(self.root / 'manager')
        self.addCleanup(manager.executor.shutdown)
        task = {'stage': '消息导入', 'status': 'running'}
        with patch.object(self.archive, 'update', side_effect=sqlite3.OperationalError('database is locked')):
            manager.perform({'mode': 'plaintext'}, self.archive, task, 'update', [])
        self.assertEqual(task['status'], 'failed')
        self.assertEqual(task['stage'], '消息导入')
        self.assertIn('database is locked', task['reason'])

    def test_multishard_dedup_repeat_changes_history_restart_and_metadata(self):
        shard(self.source, 1, self.username, [(1, 103, 1, 9, 1700000002, 'third', 0),
                                             (2, 102, 1, 8, 1700000001, 'world', 0)])
        self.archive.update(self.source)
        self.assertEqual(len(self.archive.messages(self.username)), 3)
        result = self.archive.update(self.source)
        self.assertEqual((result['added'], result['changed']), (0, 0))
        path = self.source / 'message' / 'message_0.db'
        with closing(sqlite3.connect(path)) as c, c:
            c.execute(f'UPDATE "{table(self.username)}" SET message_content=?,local_type=10000,status=1 WHERE local_id=1', ('你撤回了一条消息',))
            c.execute(f'DELETE FROM "{table(self.username)}" WHERE local_id=2')
        with closing(sqlite3.connect(self.source / 'message' / 'message_1.db')) as c, c:
            c.execute(f'DELETE FROM "{table(self.username)}" WHERE local_id=2')
        self.archive.update(self.source)
        reopened = Archive(self.archive.directory)
        messages = reopened.messages(self.username)
        self.assertEqual(len(messages), 3)
        self.assertEqual(messages[0]['content'], '你撤回了一条消息')
        metadata = reopened.contacts()[0]
        self.assertEqual(metadata['total'], 3)
        self.assertEqual(metadata['first_timestamp'], 1700000000)
        self.assertEqual(metadata['last_timestamp'], 1700000002)
        self.assertEqual(metadata['preview'], 'third')
        self.assertEqual(metadata['recorded_sender_count'], 3)
        self.assertEqual({m['sender'] for m in messages}, {'我', '甲', '乙'})

    def test_local_ids_are_shard_scoped_and_server_promotion_merges(self):
        with closing(sqlite3.connect(self.source / 'message' / 'message_0.db')) as c, c:
            c.execute(f'UPDATE "{table(self.username)}" SET server_id=0')
        shard(self.source, 1, self.username, [(1, 0, 1, 9, 1700000000, 'distinct', 0)])
        self.archive.update(self.source)
        self.assertEqual(len(self.archive.messages(self.username)), 3)
        with closing(sqlite3.connect(self.source / 'message' / 'message_0.db')) as c, c:
            c.execute(f'UPDATE "{table(self.username)}" SET server_id=777 WHERE local_id=1')
        self.archive.update(self.source)
        self.assertEqual(len(self.archive.messages(self.username)), 3)
        self.assertIn('server:777', {m['message_id'] for m in self.archive.messages(self.username)})

    def test_failed_import_rolls_back_and_retries(self):
        self.archive.update(self.source)
        before = self.archive.messages(self.username)
        config = self.archive.config()
        with patch.object(self.archive, 'metadata', side_effect=RuntimeError('synthetic interruption')):
            with self.assertRaises(RuntimeError):
                self.archive.update(self.source, settings={'decrypted_dir': 'unpublished'})
        self.assertEqual(self.archive.messages(self.username), before)
        self.assertEqual(self.archive.config(), config)
        self.archive.update(self.source)
        self.assertEqual(self.archive.messages(self.username), before)

    def test_account_isolation_and_interrupted_task_restart(self):
        manager = Manager(self.root / 'state')
        account = manager.select(str(self.source), 'A', True)
        manager.archive().save_config({'self_username': 'wxid_self'})
        manager.archive().update(self.source)
        other = self.root / 'other'
        seed(other, 'friend')
        manager.select(str(other), 'B', True)
        self.assertEqual(manager.archive().contacts(), [])
        self.assertNotIn('self_username', manager.archive().config())
        manager.select(str(self.source), 'A', True)
        self.assertEqual(manager.archive().contacts()[0]['total'], 2)
        path = manager.archive().directory / 'task.json'
        path.write_text(json.dumps({'id': 'test', 'status': 'running'}), encoding='utf-8')
        restarted = Manager(manager.root)
        self.assertEqual(restarted.status()['task']['status'], 'interrupted')
        self.assertEqual(restarted.archive().config()['self_username'], 'wxid_self')
        with self.assertRaises(ValueError):
            manager.select(str(self.source), 'different account', True)
        manager.executor.shutdown()
        restarted.executor.shutdown()

    def test_background_task_status_reads_do_not_race_windows_publication(self):
        manager = Manager(self.root / 'state')
        manager.select(str(self.source), 'synthetic', True)
        manager.archive().save_config({'self_username': 'wxid_self'})
        entered, release = threading.Event(), threading.Event()
        original = Archive.update
        def slow_update(archive, *args, **kwargs):
            entered.set()
            release.wait(5)
            return original(archive, *args, **kwargs)
        with patch.object(Archive, 'update', slow_update):
            manager.submit('update', [self.username])
            self.assertTrue(entered.wait(5))
            self.assertEqual(manager.status()['task']['status'], 'running')
            self.assertEqual(manager.archive().contacts(), [])
            with self.assertRaises(ValueError):
                manager.submit('update')
            # Status uses the in-memory task under the same lock as JSON publication.
            with patch.object(Path, 'read_text', side_effect=AssertionError('must not read task file while running')):
                self.assertEqual(manager.status()['task']['status'], 'running')
            release.set()
            manager.executor.shutdown(wait=True)
        self.assertEqual(manager.status()['task']['status'], 'success')
        self.assertEqual(manager.archive().contacts()[0]['total'], 2)

    def add_voice(self, data=b'RIFF_synthetic'):
        with closing(sqlite3.connect(self.source / 'message' / 'message_0.db')) as c, c:
            c.execute(f'INSERT INTO "{table(self.username)}" VALUES (3,103,34,8,1700000002,?,0)', ('voice',))
        path = self.source / 'message' / 'media_0.db'
        with closing(sqlite3.connect(path)) as c, c:
            c.execute('CREATE TABLE Name2Id(user_name TEXT)')
            c.execute('INSERT INTO Name2Id(rowid,user_name) VALUES (99,?)', (self.username,))
            c.execute('CREATE TABLE VoiceInfo(chat_name_id INTEGER,local_id INTEGER,svr_id INTEGER,create_time INTEGER,voice_data BLOB,data_index TEXT)')
            c.execute('INSERT INTO VoiceInfo VALUES (99,3,103,1700000002,?,?)', (data, 'synthetic_media'))
        self.archive.update(self.source)
        return path

    def test_voice_success_reuse_export_and_changed_media(self):
        path = self.add_voice()
        recognize = Mock(return_value='本地识别文本')
        result = voice_service.transcribe(self.archive, self.source, [self.username], 'local_model', recognizer=recognize)
        self.assertEqual(result['success'], 1)
        voice_service.transcribe(self.archive, self.source, [self.username], 'local_model', recognizer=recognize)
        recognize.assert_called_once()
        messages = Archive(self.archive.directory).messages(self.username)
        self.assertEqual(messages[-1]['content'], '本地识别文本')
        self.assertEqual(messages[-1]['type'], 34)
        self.assertEqual(messages[-1]['transcription']['media_source']['database'], 'media_0.db')
        self.assertIn('[语音] 本地识别文本', we.render_txt('test', messages))
        self.assertIn('本地识别文本', we.render_json(self.archive.contacts()[0], messages))
        with closing(sqlite3.connect(self.source / 'message' / 'message_0.db')) as c, c:
            c.execute(f'UPDATE "{table(self.username)}" SET status=1 WHERE local_id=3')
        self.archive.update(self.source)
        voice_service.transcribe(self.archive, self.source, [self.username], 'local_model', recognizer=recognize)
        recognize.assert_called_once()
        with closing(sqlite3.connect(path)) as c, c:
            c.execute('UPDATE VoiceInfo SET voice_data=?', (b'changed_audio',))
        self.archive.update(self.source)
        self.assertEqual(self.archive.messages(self.username)[-1]['content'], '')
        voice_service.transcribe(self.archive, self.source, [self.username], 'local_model', recognizer=recognize)
        self.assertEqual(recognize.call_count, 2)

    def test_voice_media_missing_tool_missing_and_failure(self):
        path = self.add_voice()
        def unavailable(*args):
            raise voice_service.ToolUnavailable('tool missing')
        voice_service.transcribe(self.archive, self.source, [self.username], '', recognizer=unavailable)
        self.assertEqual(self.archive.messages(self.username)[-1]['transcription']['status'], 'tool_unavailable')
        def failed(*args):
            raise RuntimeError('recognition failed')
        voice_service.transcribe(self.archive, self.source, [self.username], '', recognizer=failed)
        self.assertEqual(self.archive.messages(self.username)[-1]['transcription']['status'], 'failed')
        with closing(sqlite3.connect(path)) as c, c:
            c.execute('DELETE FROM VoiceInfo')
        voice_service.transcribe(self.archive, self.source, [self.username], '', recognizer=failed)
        self.assertEqual(self.archive.messages(self.username)[-1]['transcription']['status'], 'media_missing')
        self.assertEqual(len(self.archive.messages(self.username)), 3)

    def test_api_metadata_filters_and_export_formats(self):
        manager = Manager(self.root / 'state')
        manager.select(str(self.source), 'synthetic', True)
        manager.archive().save_config({'self_username': 'wxid_self'})
        manager.archive().update(self.source)
        async def body(response):
            return b''.join([part async for part in response.body_iterator])
        with patch.object(web, 'manager', manager):
            self.assertEqual(len(web.contacts(q='测试', category='group', start=1700000000, end=1700000001)), 1)
            self.assertEqual(web.contacts(start=1700000001), [])
            self.assertEqual(web.contacts(end=1700000000), [])
            with patch.object(we, 'get_messages', side_effect=AssertionError('list must not scan source')):
                self.assertEqual(web.contacts(sort='count')[0]['total'], 2)
            for fmt in ('txt', 'json', 'zip'):
                data = asyncio.run(body(web.export(web.ExportReq(usernames=[self.username], format=fmt))))
                if fmt == 'zip':
                    with zipfile.ZipFile(io.BytesIO(data)) as z:
                        record = json.loads(z.read(next(n for n in z.namelist() if n.endswith('.json'))))
                elif fmt == 'json':
                    record = json.loads(data)
                else:
                    self.assertIn('hello', data.decode())
                    continue
                self.assertEqual(record['total'], 2)
                self.assertEqual(record['contact']['total'], 2)
        manager.executor.shutdown()

    def test_export_snapshot_keeps_metadata_and_messages_from_same_commit(self):
        self.archive.update(self.source)
        with closing(self.archive.connect()) as reader:
            reader.execute('BEGIN')
            self.assertEqual(self.archive.contacts(reader)[0]['total'], 2)
            with closing(sqlite3.connect(self.source / 'message' / 'message_0.db')) as c, c:
                c.execute(f'INSERT INTO "{table(self.username)}" VALUES (3,103,1,8,1700000002,?,0)', ('new',))
            self.archive.update(self.source)
            self.assertEqual(len(self.archive.messages(self.username, reader)), 2)
            self.assertEqual(self.archive.contacts(reader)[0]['total'], 2)
        self.assertEqual(len(self.archive.messages(self.username)), 3)

    def test_metadata_sort_order_and_changed_last_message(self):
        friend = 'new_friend'
        with closing(sqlite3.connect(self.source / 'contact' / 'contact.db')) as c, c:
            c.execute('INSERT INTO contact VALUES (?,?,1,?,?)', (friend, '', '新好友', ''))
        shard(self.source, 1, friend, [(1, 201, 1, 8, 1700000010, 'latest', 0)])
        manager = Manager(self.root / 'state')
        manager.select(str(self.source), 'synthetic', True)
        manager.archive().save_config({'self_username': 'wxid_self'})
        manager.archive().update(self.source)
        with patch.object(web, 'manager', manager):
            self.assertEqual([r['username'] for r in web.contacts(sort='recent') if r['imported']], [friend, self.username])
            self.assertEqual([r['username'] for r in web.contacts(sort='count') if r['imported']], [self.username, friend])
            self.assertEqual([r['username'] for r in web.contacts(category='friend') if r['imported']], [friend])
        with closing(sqlite3.connect(self.source / 'message' / 'message_1.db')) as c, c:
            c.execute(f'UPDATE "{table(friend)}" SET message_content=?,local_type=10000', ('对方撤回了一条消息',))
        manager.archive().update(self.source)
        metadata = next(r for r in manager.archive().contacts() if r['username'] == friend)
        self.assertEqual(metadata['preview'], '对方撤回了一条消息')
        self.assertEqual(metadata['last_type'], '系统消息')
        self.assertEqual(metadata['total'], 1)
        manager.executor.shutdown()


if __name__ == '__main__':
    unittest.main()
