"""U1 synthetic setup and extraction regressions; never inspect real processes."""
from contextlib import closing
import ctypes
import hashlib
import hmac
import json
import os
from pathlib import Path
import sqlite3
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from maintenance import Manager, identity_candidates, extraction_feedback
import wcdb_key_tool_windows as tool
from test_maintenance import seed


class SetupTests(unittest.TestCase):
    def test_all_directory_candidates_and_empty_detection(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / 'Tencent/xwechat/config'
            config.mkdir(parents=True)
            (config / 'data.ini').write_text(str(root),encoding='utf-8')
            with patch.dict(os.environ, {'APPDATA':str(root)}):
                self.assertEqual(tool.candidate_db_dirs(),[])
                for account in ('A','B'):
                    seed(root / 'xwechat_files' / account / 'db_storage')
                self.assertEqual(len(tool.candidate_db_dirs()),2)

    def test_extraction_task_feedback_and_sanitized_diagnostic(self):
        outcomes = [({'stage':'complete','databases':2,'verified_databases':2},0,True,'complete'),
                    ({'stage':'complete','databases':2,'verified_databases':1},0,True,'partial'),
                    ({'stage':'validation','candidate_count':32},1,False,'validation_failed'),
                    ({'stage':'scan','open_errors':[5],'opened_processes':0},1,False,'access_denied'),
                    ({'stage':'write_result','system_code':13},1,False,'write_failed'),
                    (None,1,False,'unknown')]
        with tempfile.TemporaryDirectory() as tmp:
            manager = Manager(Path(tmp) / 'state')
            self.addCleanup(manager.executor.shutdown)
            account = {'id':'synthetic','mode':'encrypted','directory':'synthetic'}
            archive = manager.archive(account)
            for diagnostic,exitcode,exists,expected in outcomes:
                def run(command,**options):
                    if diagnostic is not None:
                        raw = dict(diagnostic, enc_key='private-key',salt='private-salt',memory='private-memory')
                        Path(command[6]).write_text(json.dumps(raw),encoding='utf-8')
                    if exists:
                        Path(command[-1]).write_text('{}')
                    return subprocess.CompletedProcess(command,exitcode,b'private-output',b'private-output')
                task = {'status':'running'}
                with patch('supervised_process.run',side_effect=run):
                    manager.perform(account,archive,task,'extract',[])
                self.assertEqual(task['diagnostic_code'],expected)
                self.assertEqual(task['status'],'success' if expected in ('complete','partial') else 'failed')
                self.assertNotIn('private-',json.dumps(task))

    def test_config_string_and_reference_cross_boundaries_with_hmac(self):
        chunk = 2*1024*1024
        memory = bytearray(3*chunk)
        base = 65536
        name = chunk-8
        reference = 2*chunk-8
        obj, blob_at = 1024, 2048
        key, salt = bytes(range(32)), bytes(range(16))
        literal = b"x'"+key.hex().encode()+salt.hex().encode()+b"'"
        blob = tool._xor_repeat(literal,tool.WINDOWS_CONFIG_XOR_MASK)
        memory[name:name+len(tool.WINDOWS_CONFIG_CIPHER_NAME)] = tool.WINDOWS_CONFIG_CIPHER_NAME
        struct.pack_into('<Q',memory,reference,base+name)
        struct.pack_into('<Q',memory,reference+8,len(tool.WINDOWS_CONFIG_CIPHER_NAME))
        struct.pack_into('<Q',memory,reference+0x18,base+obj)
        struct.pack_into('<QQ',memory,obj+0x90,base+blob_at,len(blob))
        memory[blob_at:blob_at+len(blob)] = blob
        page = bytearray(4096)
        page[:16] = salt
        mac_key = hashlib.pbkdf2_hmac('sha512',key,bytes(b^0x3a for b in salt),2,dklen=32)
        page[-64:] = hmac.new(mac_key,bytes(page[16:4032])+struct.pack('<I',1),hashlib.sha512).digest()
        read = lambda a,n: bytes(memory[a-base:a-base+n])
        keys,remaining = {},{salt.hex()}
        stats = tool._scan_windows_v411_config_cipher(123,[(base,len(memory))],read,read,
            [('a.db','a',4096,salt.hex(),bytes(page))],{salt.hex():['a.db']},keys,remaining,lambda *a:None)
        self.assertEqual(stats['matched_salts'],1)
        self.assertFalse(remaining)

    def test_cli_encoding_reaches_expected_failure(self):
        with tempfile.TemporaryDirectory(prefix='密钥 输出 ') as tmp:
            for encoding in ('cp1252', 'gbk', 'utf-8'):
                diagnostic = Path(tmp) / (encoding + '.json')
                result = subprocess.run([sys.executable, str(Path(tool.__file__)), 'extract',
                    '--db-dir', tmp, '--diagnostics-output', str(diagnostic)],
                    capture_output=True, timeout=10,
                    env=dict(os.environ, PYTHONIOENCODING=encoding),
                    creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('未找到可解密', result.stdout.decode('utf-8'))
                self.assertNotIn(b'UnicodeEncodeError',result.stderr)
                self.assertEqual(json.loads(diagnostic.read_text())['stage'],'preflight')

    def test_plaintext_identity_names_restart_and_missing_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / 'source'
            seed(source)
            with closing(sqlite3.connect(source / 'contact/contact.db')) as c, c:
                c.execute("UPDATE contact SET nick_name='同名',remark='' WHERE username IN ('wxid_a','wxid_b')")
            self.assertEqual(len(identity_candidates(source, '同名')), 2)
            self.assertEqual(identity_candidates(source, '本人')[0]['username'], 'wxid_self')
            self.assertEqual(identity_candidates(source, '无命中'), [])
            with self.assertRaises(ValueError):
                identity_candidates(source, '')
            manager = Manager(root / 'state')
            self.addCleanup(manager.executor.shutdown)
            manager.select(str(source), 'A', True)
            self.assertEqual(manager.status()['setup']['next_action'], 'identity')
            manager.archive().save_config({'self_username': 'wxid_self'})
            manager.archive().update(source)
            self.assertTrue(manager.status()['setup']['has_readable_archive'])
            other = root / 'other'
            seed(other)
            manager.select(str(other), 'B', True)
            self.assertFalse(manager.status()['setup']['identity_saved'])
            manager.select(str(source), 'A', True)
            restarted = Manager(root / 'state')
            self.addCleanup(restarted.executor.shutdown)
            self.assertEqual(restarted.status()['setup']['next_action'], 'read')
            source.rename(root / 'moved')
            self.assertEqual(restarted.archive().message_count('test@chatroom'), 2)
            self.assertEqual(restarted.status()['setup']['next_action'], 'directory')

    def test_encrypted_preconditions_and_existing_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / 'source'
            seed(source)
            (source / 'contact/contact.db').write_bytes(b'encrypted synthetic')
            manager = Manager(root / 'state')
            self.addCleanup(manager.executor.shutdown)
            manager.select(str(source), 'encrypted', True)
            self.assertEqual(manager.status()['setup']['next_action'], 'extract')
            with self.assertRaisesRegex(ValueError, '密钥'):
                manager.submit('prepare')
            keys = root / 'synthetic.json'
            keys.write_text('{}')
            manager.archive().save_config({'keys_file': str(keys)})
            self.assertEqual(manager.status()['setup']['next_action'], 'prepare')
            with self.assertRaisesRegex(ValueError, '本人身份'):
                manager.submit('update_all')
            import encrypted_sync
            from web import app as web
            task = {'status':'running', 'stage':'准备通讯录'}
            with patch.object(encrypted_sync,'sync',side_effect=encrypted_sync.KeyPreparationError('密钥验证失败')):
                manager.perform(manager.account(),manager.archive(),task,'prepare',[])
            self.assertEqual(manager.status()['setup']['next_action'],'extract')
            self.assertEqual(task['diagnostic_code'],'keys_invalid')
            with patch.object(web,'manager',manager):
                web.settings(web.SettingsReq(keys_file=str(keys)))
            self.assertEqual(manager.status()['setup']['next_action'],'prepare')

    def test_tasklist_encodings_numbers_and_exit(self):
        for encoding, number in [('utf-8', '1,234'), ('gbk', '1.234'), ('cp1252', '1\xa0234')]:
            output = f'"Weixin.exe","123","Console","1","{number} K"'.encode(encoding)
            with patch.object(subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, output, b'')):
                self.assertEqual(tool._get_pids_windows(), [(123, 1234)])
        for encoding, text in [('utf-8','输出'), ('gbk','输出'), ('cp1252','café')]:
            self.assertEqual(tool.decode_output(text.encode(encoding)), text)
        with patch.object(subprocess, 'run', return_value=subprocess.CompletedProcess([], 7, b'', b'')):
            with self.assertRaisesRegex(RuntimeError, '7'):
                tool._get_pids_windows()

    def test_large_region_and_cross_block_literals(self):
        # Sparse reader covers >500MiB without allocating that much test memory.
        size = 501 * 1024 * 1024
        needle = tool.WINDOWS_CONFIG_CIPHER_NAME
        position = 500 * 1024 * 1024 - 8
        def read(address, count):
            result = bytearray(count)
            start, end = max(position, address), min(position + len(needle), address + count)
            if start < end:
                result[start-address:end-address] = needle[start-position:end-position]
            return bytes(result)
        self.assertEqual(tool._find_bytes_in_regions([(0,size)], read, needle), {position})
        literal = b"x'" + b'a'*96 + b"'"
        payload = b'_'*30 + literal + b'_'*20
        chunks = tool._iter_windows_region_chunks([(0,len(payload))], lambda a,n:payload[a:a+n], chunk_size=32, overlap=195)
        self.assertTrue(any(tool._HEX_RE.search(data) for _,data in chunks))

    def test_feedback_classification(self):
        for diagnostic, exitcode, exists, expected in [
            ({'databases':2,'verified_databases':2},0,True,'complete'),
            ({'databases':2,'verified_databases':1},0,True,'partial'),
            ({'candidate_count':32},1,False,'validation_failed'),
            ({'open_errors':[5],'opened_processes':0},1,False,'access_denied'),
            ({'stage':'write_result'},1,False,'write_failed'),
            ({'processes':0},1,False,'not_running'),
            ({'process_list_returncode':7},1,False,'process_list_failed'),
            ({},1,False,'unknown')]:
            self.assertEqual(extraction_feedback(diagnostic,exitcode,exists)[0],expected)

    def test_large_regions_are_enumerated_and_diagnostic_redacted(self):
        class Kernel:
            def VirtualQueryEx(self, handle, address, pointer, length):
                if address.value:
                    return 0
                mbi = pointer._obj
                mbi.BaseAddress, mbi.RegionSize = 65536, 501*1024*1024
                mbi.State, mbi.Protect = 0x1000, 4
                return length
            def OpenProcess(self, *args): return 1
            def CloseHandle(self, handle): pass
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / 'keys.json'
            diagnostic = {}
            def scan(pid, regions, read_region, read_mem, files, salts, keys, remaining, printer):
                self.assertEqual(regions, [(65536,501*1024*1024)])
                keys['synthetic-salt'] = 'synthetic-secret'
                remaining.clear()
                return {'candidate_count':1}
            with patch.object(ctypes.windll, 'kernel32', Kernel()), patch.object(tool, '_get_pids_windows', return_value=[(123,10)]), patch.object(
                    tool, 'collect_db_files', return_value=([('a.db','a',4096,'synthetic-salt',b'')], {'synthetic-salt':['a.db']})), patch.object(
                    tool, '_scan_windows_v411_config_cipher', side_effect=scan):
                tool._scan_memory_raw_key('synthetic', str(output), diagnostic)
            self.assertEqual(diagnostic['verified_databases'],1)
            self.assertNotIn('synthetic-secret',json.dumps(diagnostic))
            self.assertNotIn('synthetic-salt',json.dumps(diagnostic))


if __name__ == '__main__':
    unittest.main()
