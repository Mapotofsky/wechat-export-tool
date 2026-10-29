"""Isolated regressions for feedback, lifecycle, query and export contracts."""
import os
import json
from pathlib import Path
import sys
import tempfile
import unittest

root = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(root), str(root / 'tests')]
with tempfile.TemporaryDirectory(prefix='frontend-regression-') as tmp:
    os.environ['WECHAT_ARCHIVE_DATA_DIR'] = str(Path(tmp) / 'bootstrap')
    names = ('test_frontend_feedback', 'test_u3', 'test_desktop', 'test_background_processes',
             'test_u2', 'test_projects', 'test_export_invariants', 'test_search', 'test_web_http',
             'test_analysis', 'test_analysis_matching', 'test_view_state', 'test_setup',
             'test_maintenance', 'test_selective_import', 'test_voice')
    suite = unittest.TestSuite(unittest.defaultTestLoader.loadTestsFromName(name) for name in names)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    output = root / 'output/frontend-regression.json'
    output.parent.mkdir(exist_ok=True)
    output.write_text(json.dumps({'tests':result.testsRun, 'failures':len(result.failures),
        'errors':len(result.errors), 'skipped':len(result.skipped), 'passed':result.wasSuccessful()},
        ensure_ascii=False, indent=2), encoding='utf-8')
    sys.exit(not result.wasSuccessful())
