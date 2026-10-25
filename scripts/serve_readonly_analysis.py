"""用指定程序副本打开真实档案的只读验收界面；阅读位置只保存在内存。"""
import argparse
import json
import os
from pathlib import Path
import sys
import tempfile
from contextlib import closing


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--app',type=Path,required=True)
    p.add_argument('--database',type=Path,required=True)
    p.add_argument('--username',required=True)
    p.add_argument('--port',type=int,default=8769)
    args=p.parse_args()
    sys.path.insert(0,str(Path(__file__).resolve().parent))
    sys.path.insert(0,str(args.app.resolve()))
    from profile_analysis import ReadOnlyArchive
    sys.path.insert(0,str(args.app.resolve()))
    with tempfile.TemporaryDirectory(prefix='readonly_analysis_ui_') as temp:
        os.environ['WECHAT_ARCHIVE_DATA_DIR']=temp
        from web import app as web
        assert Path(web.__file__).resolve().parents[1]==args.app.resolve()
        assert Path(web.analysis.__file__).resolve().parent==args.app.resolve()
        from fastapi.responses import JSONResponse
        import uvicorn
        class UIArchive(ReadOnlyArchive):
            view=dict(active=args.username,offset=0,ms='',me='',scroll=0)
            def config(self):
                keys=('self_username','my_name','contact_directory','followed_usernames','hidden_usernames',
                      'session_groups','analysis_options','analysis_aliases','analysis_blocklist')
                with closing(self.connect()) as c:
                    config={k:json.loads(v) for k,v in c.execute('SELECT key,value FROM settings WHERE key IN ('+','.join('?' for _ in keys)+')',keys)}
                return dict(config,view_state=self.view)
            def save_config(self,values):
                if set(values)!={'view_state'}:raise ValueError('只读验收不保存设置')
                self.view=values['view_state']
        archive=UIArchive(args.database);archive.directory=Path(temp)
        account=dict(id='readonly',label='只读性能验收',directory='',mode='plaintext')
        web.manager.registry=dict(active='readonly',accounts=[account])
        web.manager.archives={'readonly':archive}
        @web.app.middleware('http')
        async def read_only(request,call_next):
            if request.method!='GET' and request.url.path not in ('/api/analysis','/api/analysis/messages','/api/view-state'):
                return JSONResponse({'detail':'只读验收不执行此操作'},status_code=403)
            return await call_next(request)
        print('只读验收服务已启动；不写真实档案，不保存聊天正文证据。',flush=True)
        try:uvicorn.run(web.app,host='127.0.0.1',port=args.port,access_log=False)
        finally:web.manager.executor.shutdown()


if __name__=='__main__':main()
