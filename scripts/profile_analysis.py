"""只读测量指定会话分析；输出规模、阶段耗时和进程内存，不输出原文。"""
import argparse
from contextlib import closing
import ctypes
from ctypes import wintypes
import importlib.util
import json
from pathlib import Path
import sqlite3
import sys
import time

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from archive_store import Archive


class ReadOnlyArchive(Archive):
    def __init__(self, path):
        self.path = path.resolve()

    def connect(self):
        c = sqlite3.connect(self.path.as_uri()+'?mode=ro', uri=True, timeout=30)
        c.execute('PRAGMA query_only=ON')
        return c


def memory():
    class Counters(ctypes.Structure):
        _fields_ = [('cb',wintypes.DWORD),('faults',wintypes.DWORD)]+[(x,ctypes.c_size_t) for x in
            ('peak_working','working','peak_pool','pool','peak_nonpaged','nonpaged','private','peak_private')]
    c=Counters();c.cb=ctypes.sizeof(c)
    ctypes.windll.kernel32.GetCurrentProcess.restype=wintypes.HANDLE
    ctypes.windll.psapi.GetProcessMemoryInfo.argtypes=[wintypes.HANDLE,ctypes.c_void_p,wintypes.DWORD]
    handle=ctypes.windll.kernel32.GetCurrentProcess()
    ctypes.windll.psapi.GetProcessMemoryInfo(handle,ctypes.byref(c),c.cb)
    return dict(working_mib=round(c.working/2**20,2),peak_working_mib=round(c.peak_working/2**20,2),private_mib=round(c.private/2**20,2))


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--database',type=Path,required=True)
    p.add_argument('--username',required=True)
    p.add_argument('--module',type=Path,default=ROOT/'archive_analysis.py')
    p.add_argument('--output',type=Path,required=True)
    p.add_argument('--rounds',type=int,default=1)
    p.add_argument('--verify',action='store_true',help='以逐表达原文匹配对账展示结果，不输出表达或正文')
    p.add_argument('--app',type=Path,help='从便携 app 加载被测分析模块')
    a=p.parse_args()
    if a.app:
        sys.path.insert(0,str(a.app.resolve()))
        a.module=a.app.resolve()/'archive_analysis.py'
        sys.modules.pop('analysis_lexicon',None)
    spec=importlib.util.spec_from_file_location('profiled_analysis',a.module)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    archive=ReadOnlyArchive(a.database)
    stages=[]
    def wrap(owner,name):
        original=getattr(owner,name)
        def measured(*args,**kwargs):
            print(json.dumps(dict(stage=name,event='start',**memory())),flush=True)
            t=time.perf_counter();result=original(*args,**kwargs)
            record=dict(stage=name,seconds=round(time.perf_counter()-t,4),**memory())
            if name=='content_index':record['candidates']=len(result['vocabulary'])
            stages.append(record);print(json.dumps(record),flush=True)
            return result
        setattr(owner,name,measured)
    for name in ('load_rows','content_index','fill_comparison_counts','content_stats'):
        wrap(module,name)
    for name in ('discover','nested_groups'):wrap(module.lex,name)
    timings=[]
    for _ in range(a.rounds):
        t=time.perf_counter();result=module.analyze(archive,dict(usernames=[a.username]))
        timings.append(round(time.perf_counter()-t,4))
    record=dict(seconds=timings,messages=result['overview']['total'],text_messages=result['content']['text_messages'],
                candidates=result['content']['candidate_total'],stages=stages,**memory())
    if a.verify:
        with closing(archive.connect()) as c:
            rows,_=module.load_rows(archive,c,[a.username],None,None,'',True)
            context=module.analysis_context(c)
        items=result['content']['words']+result['content']['phrases']+result['content']['discovered']+result['content']['groups']
        items += [n for x in result['content']['phrases'] for n in x.get('nested',[])]
        differences=0
        for item in items:
            hits=[module.lex.expression_spans(module.text_for(m,True),item['term'],context) for m in rows]
            differences+=int((sum(bool(s) for s in hits),sum(len(s) for s in hits))!=(item['count'],item['occurrences']))
        record['verification']=dict(expressions=len(items),count_or_occurrence_differences=differences,
                                    overview_equals_loaded=len(rows)==result['overview']['total'])
        if differences:raise RuntimeError('原文计数对账失败（正文未输出）')
    a.output.parent.mkdir(parents=True,exist_ok=True)
    a.output.write_text(json.dumps(record,indent=2),encoding='utf-8')
    print(json.dumps({k:v for k,v in record.items() if k!='stages'}),flush=True)


if __name__=='__main__':main()
