"""Run targeted U2 regressions with web.app isolated before any test import."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

root = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(root), str(root / 'tests')]
with tempfile.TemporaryDirectory(prefix='u2-regression-') as tmp:
    os.environ['WECHAT_ARCHIVE_DATA_DIR'] = str(Path(tmp) / 'bootstrap')
    suite = unittest.TestSuite(unittest.defaultTestLoader.loadTestsFromName(name) for name in (
        'test_u2', 'test_projects', 'test_usability', 'test_maintenance', 'test_selective_import',
        'test_export_invariants', 'test_search', 'test_voice', 'test_web_http', 'test_desktop', 'test_view_state', 'test_setup'))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    report = root / 'output/u2/regression.json'
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(json.dumps(dict(tests=result.testsRun, failures=len(result.failures), errors=len(result.errors),
                                     skipped=len(result.skipped), passed=result.wasSuccessful()), indent=2), encoding='utf-8')
    sys.exit(not result.wasSuccessful())
