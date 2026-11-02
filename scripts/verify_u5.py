"""Run affected analysis/matching/evidence regressions without opening user data."""
import os
from pathlib import Path
import sys
import tempfile
import unittest

with tempfile.TemporaryDirectory(prefix='wechat_u5_tests_') as root:
    os.environ['WECHAT_ARCHIVE_DATA_DIR'] = root
    repo = Path(__file__).resolve().parents[1]
    sys.path[:0] = [str(repo), str(repo / 'tests')]
    suite = unittest.TestLoader().loadTestsFromNames([
        'test_analysis', 'test_analysis_matching', 'test_search', 'test_lexical_exploration'])
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    raise SystemExit(0 if result.wasSuccessful() else 1)
