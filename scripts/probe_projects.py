"""Verify project/hidden behavior using a chosen application copy and synthetic data."""
import argparse
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
import zipfile


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--app', type=Path, required=True)
    parser.add_argument('--export', dest='export_file', type=Path,
                        help='检查界面验收导出的 second@chatroom 合成 ZIP')
    args = parser.parse_args()
    root = Path(__file__).resolve().parent.parent
    app = args.app.resolve()
    with tempfile.TemporaryDirectory(prefix='wechat_project_probe_') as tmp:
        os.environ['WECHAT_ARCHIVE_DATA_DIR'] = str(Path(tmp) / 'initial')
        sys.path.insert(0, str(root / 'tests'))
        sys.path.insert(0, str(app))
        import archive_store
        from web import app as web
        if Path(archive_store.__file__).resolve().parent != app or Path(web.__file__).resolve().parent.parent != app:
            raise RuntimeError('验证未加载指定应用副本')
        import test_projects
        suite = unittest.defaultTestLoader.loadTestsFromTestCase(test_projects.ProjectTests)
        result = unittest.TextTestRunner(verbosity=2).run(suite)
        if not result.wasSuccessful():
            return 1
        if args.export_file:
            with zipfile.ZipFile(args.export_file) as bundle:
                records = [json.loads(bundle.read(n)) for n in bundle.namelist() if n.endswith('.json')]
                assert len(bundle.namelist()) == 2 and len(records) == 1
                record = records[0]
                assert record['contact']['username'] == 'second@chatroom'
                assert record['total'] == len(record['messages']) == 8
                assert any('报销已经到账' in m['content'] for m in record['messages'])
            print('Synthetic UI export: one conversation, 8 messages, successful transcript included.')
        print(f'Application verified: {app}; tests={result.testsRun}')
        web.manager.executor.shutdown()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
