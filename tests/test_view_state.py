"""Reading state survives process/port changes and stays account-local."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from maintenance import Manager
from test_maintenance import seed
from web import app as web


class ViewStateTests(unittest.TestCase):
    def test_restart_and_account_switch(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / 'source'
            seed(source)
            manager = Manager(root / 'state')
            account = manager.select(str(source), '合成账号', True)
            with patch.object(web, 'manager', manager):
                self.assertEqual(web.view_state(), {})
                web.save_view_state(web.ViewStateReq(account_id=account['id'], active='test@chatroom',
                    offset=50, ms='2023-11-15', me='2023-11-25', scroll=123.5))
            manager.executor.shutdown()
            reopened = Manager(root / 'state')
            try:
                with patch.object(web, 'manager', reopened):
                    self.assertEqual(web.view_state()['scroll'], 123.5)
                    self.assertEqual(web.view_state()['offset'], 50)
                    second = root / 'second'
                    seed(second)
                    reopened.select(str(second), '另一个账号', True)
                    self.assertEqual(web.view_state(), {})
                    with self.assertRaisesRegex(ValueError, '账号已切换'):
                        web.save_view_state(web.ViewStateReq(account_id=account['id'], active='old'))
                    self.assertEqual(web.view_state(), {})
            finally:
                reopened.executor.shutdown()
