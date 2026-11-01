"""Exercise actual denied data/log writes in an isolated desktop process."""
import argparse
import getpass
import json
import os
from pathlib import Path
import subprocess

parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('--package',type=Path,required=True)
parser.add_argument('--output',type=Path,required=True)
parser.add_argument('--target',choices=('data','logs'),required=True)
args=parser.parse_args()
package,output=args.package.resolve(),args.output.resolve()
assert output.is_relative_to(Path(__file__).resolve().parents[1]/'output/u4')
data=output/('拒绝写入 '+args.target)
target=data if args.target=='data' else data/'logs'
target.mkdir(parents=True,exist_ok=True)
identity=getpass.getuser()
env=dict(os.environ,WECHAT_ARCHIVE_DATA_DIR=str(data),WECHAT_ARCHIVE_HOME=str(package))
try:
    subprocess.run(['icacls',str(target),'/deny',identity+':(OI)(CI)(WD,AD,WEA,WA)'],check=True,capture_output=True)
    result=subprocess.run([str(package/'runtime/pythonw.exe'),str(package/'app/desktop.py')],env=env,
                          creationflags=subprocess.CREATE_NO_WINDOW,timeout=300)
    assert result.returncode==1
finally:
    subprocess.run(['icacls',str(target),'/remove:d',identity],check=True,capture_output=True)
(output/('permission-'+args.target+'.json')).write_text(json.dumps({'target':str(target),'returncode':result.returncode,'acl_restored':True},ensure_ascii=False,indent=2),encoding='utf-8')
print('Native permission failure captured; ACL restored')
