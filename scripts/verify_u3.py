"""Run U3 regressions with an isolated data root before importing web.app."""
import os
from pathlib import Path
import sys
import tempfile
import unittest

root = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(root), str(root / 'tests')]
with tempfile.TemporaryDirectory(prefix='u3-regression-') as tmp:
    os.environ['WECHAT_ARCHIVE_DATA_DIR'] = str(Path(tmp) / 'bootstrap')
    suite = unittest.TestSuite(unittest.defaultTestLoader.loadTestsFromName(name) for name in
                               ('test_u3', 'test_selective_import', 'test_maintenance', 'test_voice', 'test_desktop',
                                'test_background_processes', 'test_setup', 'test_u2', 'test_projects',
                                'test_export_invariants', 'test_search', 'test_web_http', 'test_view_state', 'test_usability',
                                'test_encrypted_wal'))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    sys.exit(not result.wasSuccessful())
