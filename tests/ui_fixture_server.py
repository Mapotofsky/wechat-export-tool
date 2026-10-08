"""Manual browser acceptance fixture: strictly synthetic data, isolated temporary state."""
import sys
from pathlib import Path
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from test_maintenance import seed
from maintenance import Manager
from web import app as web
import uvicorn

if __name__ == '__main__':
    with tempfile.TemporaryDirectory(prefix='wechat_ui_test_') as tmp:
        source = Path(tmp) / 'source'
        seed(source)
        web.manager = Manager(Path(tmp) / 'state')
        if '--prepared' in sys.argv:
            web.manager.select(str(source), 'synthetic_preview', True)
            web.manager.archive().save_config({'self_username': 'wxid_self'})
            web.manager.archive().update(source)
        print('SYNTHETIC_SOURCE=' + str(source), flush=True)
        uvicorn.run(web.app, host='127.0.0.1', port=8767, access_log=False)
