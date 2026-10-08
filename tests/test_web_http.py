"""Actual localhost HTTP workflow with isolated synthetic data."""
import io
import json
from pathlib import Path
import subprocess
import sys
import time
import unittest
import urllib.error
import urllib.request
import zipfile


class WebHttpTests(unittest.TestCase):
    def test_prepare_identity_background_update_and_downloads_over_http(self):
        fixture = Path(__file__).parent / 'ui_fixture_server.py'
        process = subprocess.Popen([sys.executable, str(fixture)], stdout=subprocess.PIPE,
                                   stderr=subprocess.DEVNULL, text=True, encoding='utf-8')
        self.addCleanup(process.stdout.close)
        def stop():
            process.terminate()
            process.wait(timeout=10)
        self.addCleanup(stop)
        source = process.stdout.readline().strip().removeprefix('SYNTHETIC_SOURCE=')
        base = 'http://127.0.0.1:8767/api/'
        def request(path, data=None):
            req = urllib.request.Request(base + path, data=json.dumps(data).encode() if data is not None else None,
                                         headers={'Content-Type': 'application/json'})
            return urllib.request.urlopen(req, timeout=5)
        def api(path, data=None):
            with request(path, data) as response:
                return json.load(response)
        for _ in range(50):
            try:
                initial = api('data-status')
                break
            except urllib.error.URLError:
                time.sleep(.1)
        else:
            self.fail('Synthetic localhost server did not start')
        self.assertFalse(initial['has_data'])
        self.assertEqual(api('check-directory', {'directory': source})['mode'], 'plaintext')
        api('account', {'directory': source, 'label': 'synthetic_http', 'confirmed': True})
        candidate = api('identity-candidates', {'query': 'public_id'})[0]
        self.assertEqual(candidate['username'], 'wxid_self')
        api('settings', {'self_username': candidate['username'], 'confirmed': True})
        api('tasks', {'action': 'update'})
        for _ in range(100):
            status = api('data-status')
            if status['task']['status'] != 'running':
                break
            time.sleep(.02)
        self.assertEqual(status['task']['status'], 'success', status['task'])
        rows = api('contacts?q=test%40chatroom&category=group&sort=count')
        self.assertEqual(rows[0]['total'], 2)
        messages = api('preview?username=test%40chatroom')
        self.assertEqual([m['content'] for m in messages], ['hello', 'world'])
        self.assertEqual(api('preview?username=missing'), [])
        for fmt in ('txt', 'json', 'zip'):
            with request('export', {'usernames': ['test@chatroom'], 'format': fmt}) as response:
                self.assertIn("filename*=UTF-8''", response.headers['Content-Disposition'])
                body = response.read()
            if fmt == 'zip':
                with zipfile.ZipFile(io.BytesIO(body)) as archive:
                    self.assertEqual(len(archive.namelist()), 2)
                    record = json.loads(archive.read(next(n for n in archive.namelist() if n.endswith('.json'))))
                    self.assertEqual(record['total'], 2)
            elif fmt == 'json':
                self.assertEqual(json.loads(body)['total'], 2)
            else:
                self.assertIn('hello', body.decode('utf-8'))
        api('tasks', {'action': 'update'})
        for _ in range(100):
            status = api('data-status')
            if status['task']['status'] != 'running':
                break
            time.sleep(.02)
        self.assertEqual(status['task']['result']['added'], 0)
        self.assertEqual(status['task']['result']['changed'], 0)


if __name__ == '__main__':
    unittest.main()
