"""Run isolated U1 regressions with no production data directory access."""
import os
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT / 'tests')]
with tempfile.TemporaryDirectory(prefix='u1-tests-') as tmp:
    os.environ['WECHAT_ARCHIVE_DATA_DIR'] = tmp
    suite = unittest.TestSuite(unittest.defaultTestLoader.loadTestsFromName(name) for name in (
        'test_setup', 'test_maintenance', 'test_background_processes', 'test_selective_import',
        'test_encrypted_wal', 'test_web_http', 'test_desktop', 'test_launcher', 'test_view_state'))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    report = ROOT / 'output/u1/regression.json'
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(json.dumps({'tests':result.testsRun,'failures':len(result.failures),
        'errors':len(result.errors),'skipped':len(result.skipped),'passed':result.wasSuccessful()},indent=2),encoding='utf-8')
    sys.exit(not result.wasSuccessful())
