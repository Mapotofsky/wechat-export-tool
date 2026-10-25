import React,{useEffect,useLayoutEffect,useMemo,useRef,useState} from 'react';

type Row={username:string;match_reasons?:string[]};
type Props<T extends Row>={rows:T[];children:(row:T,index:number,focus:(index:number)=>void)=>React.ReactNode;header:React.ReactNode};

// 只减少页面节点；完整 rows 仍用于筛选、勾选和批量范围。
export default function SessionList<T extends Row>({rows,children,header}:Props<T>){
 const root=useRef<HTMLDivElement>(null),pendingFocus=useRef<number|null>(null);
 const [viewport,setViewport]=useState({top:0,height:600});
 const offsets=useMemo(()=>{const a=[0];for(const row of rows)a.push(a[a.length-1]+(row.match_reasons?.length?79:55));return a},[rows]);
 function indexAt(top:number){let a=0,b=rows.length;while(a<b){const m=(a+b)>>1;if(offsets[m+1]<=top)a=m+1;else b=m}return a}
 const start=Math.max(0,indexAt(viewport.top)-6),end=Math.min(rows.length,indexAt(viewport.top+viewport.height)+7);
 useEffect(()=>{const el=root.current!;const observer=new ResizeObserver(()=>setViewport(v=>({...v,height:el.clientHeight})));observer.observe(el);return()=>observer.disconnect()},[]);
 useLayoutEffect(()=>{const el=root.current!;el.scrollTop=0;setViewport(v=>({...v,top:0}));pendingFocus.current=null},[rows]);
 useLayoutEffect(()=>{if(pendingFocus.current===null)return;const button=root.current?.querySelector<HTMLButtonElement>(`[data-chat-index="${pendingFocus.current}"] .chat-select`);if(button){button.focus({preventScroll:true});pendingFocus.current=null}},[start,end,viewport]);
 function focus(index:number){
  const el=root.current!;pendingFocus.current=index;
  if(offsets[index]<el.scrollTop)el.scrollTop=offsets[index];
  else if(offsets[index+1]>el.scrollTop+el.clientHeight)el.scrollTop=offsets[index+1]-el.clientHeight;
  setViewport({top:el.scrollTop,height:el.clientHeight});
 }
 return <div className="session-scroll" role="list" aria-label="会话列表" ref={root} onScroll={e=>setViewport({top:e.currentTarget.scrollTop,height:e.currentTarget.clientHeight})}>
  {header}
  <div role="presentation" style={{height:offsets[start]}}/>
  {rows.slice(start,end).map((row,i)=><div key={row.username} role="presentation" data-chat-index={start+i} style={{height:offsets[start+i+1]-offsets[start+i]}}>{children(row,start+i,focus)}</div>)}
  <div role="presentation" style={{height:offsets[rows.length]-offsets[end]}}/>
 </div>
}
