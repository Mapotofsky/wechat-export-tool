"""Run U4 regressions with every module-level Manager isolated from personal data."""
import os
from pathlib import Path
import sys
import tempfile
import unittest

sys.stdout.reconfigure(encoding='utf-8')
sys.stderr.reconfigure(encoding='utf-8')

root = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(root), str(root / 'tests')]
with tempfile.TemporaryDirectory(prefix='U4 合成回归 ') as tmp:
    os.environ['WECHAT_ARCHIVE_DATA_DIR'] = tmp
    suite = unittest.defaultTestLoader.loadTestsFromNames(sys.argv[1:] or [
        'test_u4', 'test_desktop', 'test_launcher', 'test_selective_import',
        'test_maintenance', 'test_view_state', 'test_voice', 'test_web_http'])
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    from web.app import manager
    manager.executor.shutdown()
    sys.exit(not result.wasSuccessful())
