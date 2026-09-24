"""Regression checks for the local export workflow; all data is synthetic."""

import asyncio
from contextlib import closing
from contextlib import redirect_stdout
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
import zipfile

import zstandard

import run
import wechat_export as export
import wcdb_key_tool_windows as key_tool
from web import app as web_app


class ExportInvariantTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "message").mkdir()
        export._SELF_ID_CACHE.clear()

    def message_db(self, username, rows, *, missing_content=False):
        path = self.root / "message" / "message_0.db"
        table = "Msg_" + hashlib.md5(username.encode("utf-8")).hexdigest()
        content_column = "" if missing_content else ", message_content BLOB"
        with closing(sqlite3.connect(path)) as conn:
            conn.execute(
                f'CREATE TABLE "{table}" ('
                "local_id INTEGER, local_type INTEGER, real_sender_id INTEGER, "
                f"create_time INTEGER{content_column})"
            )
            if rows:
                conn.executemany(
                    f'INSERT INTO "{table}" VALUES (?, ?, ?, ?, ?)', rows
                )
            conn.commit()
        return path

    def seed_exportable_contact(self):
        username = "wxid_test"
        (self.root / "contact").mkdir()
        with closing(sqlite3.connect(self.root / "contact" / "contact.db")) as conn:
            conn.execute(
                "CREATE TABLE contact (username TEXT, local_type INTEGER, "
                "remark TEXT, nick_name TEXT)"
            )
            conn.execute(
                "INSERT INTO contact VALUES (?, ?, ?, ?)",
                (username, 1, "测试联系人", ""),
            )
            conn.commit()
        self.message_db(username, [(1, 1, 1, 1700000000, b"hello")])
        return username

    def test_text_and_zstd_export(self):
        username = "wxid_test"
        (self.root / "contact").mkdir()
        with closing(sqlite3.connect(self.root / "contact" / "contact.db")) as conn:
            conn.execute(
                "CREATE TABLE contact (username TEXT, local_type INTEGER, "
                "remark TEXT, nick_name TEXT)"
            )
            conn.execute(
                "INSERT INTO contact VALUES (?, ?, ?, ?)",
                (username, 1, "测试联系人", ""),
            )
            conn.commit()
        self.message_db(
            username,
            [
                (1, 1, 1, 1700000000, b"hello"),
                (2, 1, 2, 1700000001, zstandard.ZstdCompressor().compress("你好".encode())),
            ],
        )

        contacts = export.load_contacts(str(self.root))
        self.assertEqual(len(contacts), 1)
        result = export.export_contact(str(self.root), contacts[0], str(self.root / "out"))
        record = json.loads(Path(result["json"]).read_text(encoding="utf-8"))
        self.assertEqual([m["content"] for m in record["messages"]], ["hello", "你好"])
        self.assertEqual(result["total"], 2)

    def test_self_sender_scan_checks_later_tables(self):
        path = self.root / "message" / "message_0.db"
        with closing(sqlite3.connect(path)) as conn:
            conn.execute("CREATE TABLE Msg_first (real_sender_id INTEGER)")
            conn.execute("INSERT INTO Msg_first VALUES (2)")
            conn.execute("CREATE TABLE Msg_second (real_sender_id INTEGER)")
            conn.execute("INSERT INTO Msg_second VALUES (4)")
            conn.commit()

        self.assertEqual(export.detect_self_sender_id(str(path)), 4)

    def test_group_members_are_not_merged_under_group_name(self):
        username = "test@chatroom"
        path = self.message_db(
            username,
            [
                (1, 1, 1, 1700000000, b"self"),
                (2, 1, 2, 1700000001, b"member one"),
                (3, 1, 3, 1700000002, b"member two"),
            ],
        )
        with closing(sqlite3.connect(path)) as conn:
            conn.execute("CREATE TABLE Name2Id (user_name TEXT PRIMARY KEY, is_session INTEGER)")
            conn.execute("INSERT INTO Name2Id (rowid, user_name, is_session) VALUES (2, 'wxid_a', 1)")
            conn.execute("INSERT INTO Name2Id (rowid, user_name, is_session) VALUES (3, 'wxid_b', 1)")
            conn.commit()
        (self.root / "contact").mkdir()
        with closing(sqlite3.connect(self.root / "contact" / "contact.db")) as conn:
            conn.execute(
                "CREATE TABLE contact (username TEXT, remark TEXT, nick_name TEXT)"
            )
            conn.execute("INSERT INTO contact VALUES ('wxid_a', '成员甲', '')")
            conn.execute("INSERT INTO contact VALUES ('wxid_b', '成员乙', '')")
            conn.commit()
        messages = export.get_messages(str(self.root), username, other_name="测试群")
        others = [m for m in messages if m["sender_id"] in (2, 3)]
        self.assertEqual(len(others), 2)
        self.assertEqual([m["sender"] for m in others], ["成员甲", "成员乙"])

    def test_read_error_is_reported_instead_of_silent_omission(self):
        username = "wxid_broken"
        self.message_db(username, [], missing_content=True)
        connections = []
        original_connect = export._connect_ro

        def tracked_connect(path):
            conn = original_connect(path)
            connections.append(conn)
            return conn

        try:
            with patch.object(export, "_connect_ro", side_effect=tracked_connect):
                try:
                    export.get_messages(str(self.root), username)
                except Exception:
                    return
            self.fail("A message shard read error was silently treated as an empty conversation")
        finally:
            for conn in connections:
                conn.close()

    def test_batch_export_names_remain_distinct(self):
        contacts = [
            {"username": "wxid_one", "remark": "A B", "nick_name": ""},
            {"username": "wxid_two", "remark": "A_B", "nick_name": ""},
        ]
        with patch.object(export, "get_messages", return_value=[]):
            paths = [
                export.export_contact(str(self.root), contact, str(self.root / "out"))["json"]
                for contact in contacts
            ]
        self.assertNotEqual(paths[0], paths[1])
        self.assertTrue(all(Path(path).exists() for path in paths))

    def test_read_only_connection_sees_pending_wal_transactions(self):
        path = self.root / "message" / "message_0.db"
        conn = sqlite3.connect(path)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("CREATE TABLE sample (value INTEGER)")
            conn.execute("INSERT INTO sample VALUES (1)")
            conn.commit()
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            conn.execute("INSERT INTO sample VALUES (2)")
            conn.commit()
            with closing(export._connect_ro(str(path))) as reader:
                count = reader.execute("SELECT COUNT(*) FROM sample").fetchone()[0]
            self.assertEqual(count, 2)
        finally:
            conn.close()

    def test_unified_cli_list_and_exports(self):
        self.seed_exportable_contact()
        output = io.StringIO()
        with redirect_stdout(output):
            run.main(["list", "--db", str(self.root)])
        self.assertIn("测试联系人", output.getvalue())

        single = self.root / "single"
        run.main(["export", "--db", str(self.root), "--contact", "测试", "--out", str(single)])
        self.assertEqual(len(list(single.glob("*_聊天记录.json"))), 1)
        record = json.loads(next(single.glob("*_聊天记录.json")).read_text(encoding="utf-8"))
        self.assertEqual(record["messages"][0]["content"], "hello")

        all_output = self.root / "all"
        run.main(["export", "--db", str(self.root), "--all", "--out", str(all_output)])
        self.assertEqual(len(list(all_output.glob("*_聊天记录.txt"))), 1)

    def test_default_paths_do_not_depend_on_working_directory(self):
        self.seed_exportable_contact()
        self.assertEqual(run.DEFAULT_DB, run.ROOT / "decrypted")
        self.assertEqual(run.DEFAULT_OUT, run.ROOT / "exports")
        outside = self.root / "outside"
        outside.mkdir()
        previous = Path.cwd()
        try:
            os.chdir(outside)
            with patch.object(run, "DEFAULT_DB", self.root), patch.object(run, "DEFAULT_OUT", self.root / "default-out"):
                run.main(["export", "--all"])
        finally:
            os.chdir(previous)
        self.assertEqual(len(list((self.root / "default-out").glob("*_聊天记录.json"))), 1)

    def test_web_runner_binds_only_to_loopback(self):
        self.seed_exportable_contact()
        with patch.object(web_app, "DB_DIR", self.root), patch("uvicorn.run") as serve:
            run.main(["web", "--db", str(self.root), "--port", "8766"])
            serve.assert_called_once_with(web_app.app, host="127.0.0.1", port=8766)
            self.assertEqual(web_app.DB_DIR, self.root)

    def test_missing_database_has_next_step(self):
        with self.assertRaisesRegex(ValueError, "--db"):
            run.database_dir(self.root / "missing")

    def test_local_web_reads_database_and_exports_zip(self):
        username = self.seed_exportable_contact()
        with patch.object(web_app, "DB_DIR", self.root):
            self.assertEqual(web_app.data_status(), {"has_data": True, "contacts": 1})
            self.assertEqual(web_app.contacts()[0]["username"], username)
            response = web_app.export(web_app.ExportReq(usernames=[username]))

        async def read_body():
            return b"".join([part async for part in response.body_iterator])

        with zipfile.ZipFile(io.BytesIO(asyncio.run(read_body()))) as archive:
            self.assertEqual(len(archive.namelist()), 2)
            self.assertTrue(any(name.endswith("_聊天记录.json") for name in archive.namelist()))

    def test_decrypt_reports_encrypted_wal_it_does_not_process(self):
        source = self.root / "message" / "message_0.db"
        with closing(sqlite3.connect(source)) as conn:
            conn.execute("CREATE TABLE sample (value INTEGER)")
            conn.commit()
        Path(str(source) + "-wal").write_bytes(b"pending frames")
        keys = self.root / "keys.json"
        keys.write_text(
            json.dumps({"message/message_0.db": {"enc_key": "00" * 32}}),
            encoding="utf-8",
        )

        def copy_as_decrypted(src, dst, _key):
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copyfile(src, dst)
            return True

        with patch.object(key_tool, "_decrypt_database", side_effect=copy_as_decrypted):
            with patch.object(key_tool, "_print") as output:
                result = key_tool.decrypt_all(
                    str(self.root), str(self.root / "decrypted"), str(keys)
                )
        self.assertEqual(result["ignored_wals"], ["message\\message_0.db-wal"])
        self.assertTrue(
            any("WAL" in str(call) for call in output.call_args_list)
        )


if __name__ == "__main__":
    unittest.main()
