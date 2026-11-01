import React from 'react';
import {useEffect,useState} from 'react';

export type Task={id:string;account_id:string;action:string;status:string;stage:string;activity?:string;unit?:string;done:number;total:number;cancellable?:boolean;reason:string;next_step:string;selected_count:number;selected_usernames:string[];scope:string;voice_targets?:{username:string;message_id:string}[];diagnostic?:Record<string,unknown>;diagnostic_code?:string;failed_stage?:string;error_location?:string;result?:Record<string,any>};
export const taskName=(action:string)=>({extract:'提取密钥',prepare:'准备通讯录',discover:'发现历史会话',update:'导入 / 更新档案',update_all:'更新全部档案',transcribe:'语音转写'} as Record<string,string>)[action]||'处理任务';
export function taskStageLabel(stage:string){
 const labels:Record<string,string>={
  '提交消息档案':'保存聊天记录','提交通讯录':'保存通讯录','提交密钥':'保存密钥',
  '读取微信进程并验证候选密钥':'查找并验证密钥','复制快照':'复制聊天数据','稳定快照':'准备聊天数据副本',
  '解密主数据库页面':'解密聊天文件','解密 WAL 页面':'解密更新数据','验证加密 WAL 提交边界':'核对可读取的更新数据',
  '解密与 WAL 合并':'准备可读取的聊天数据','核对导入签名':'检查源文件变化','复核明文源库签名':'再次检查源文件',
  '计算会话元数据':'整理会话信息','提取与语音转写':'处理语音转写',
  '检查本地识别工具':'检查本地语音识别','检查在线识别与音频工具':'检查语音转换工具',
  '正在清理任务资源':'正在结束任务','资源清理完成':'临时文件已清理'
 };
 if(stage.startsWith('解密与 WAL 合并'))return labels['解密与 WAL 合并'];
 return labels[stage]||stage;
}
const states:Record<string,string>={running:'进行中',cancelling:'正在停止',cancelled:'已停止',success:'已完成',failed:'失败',interrupted:'已中断'};
const byteCount=(n:number)=>{const scale=n>=1e9?1e9:n>=1e6?1e6:n>=1000?1000:1;return `${(n/scale).toLocaleString(undefined,{maximumFractionDigits:1})} ${{1:'B',1000:'KB',1000000:'MB',1000000000:'GB'}[scale]}`};
const fields:Record<string,[string,string][]>= {update:[['added','新增消息'],['changed','更新消息']],update_all:[['added','新增消息'],['changed','更新消息']],transcribe:[['success','本次转写成功'],['reused','已有转写，未重复识别'],['skipped','无需转写，已跳过'],['missing','找不到语音文件'],['failed','识别失败'],['remaining','尚未处理']]};
export default function TaskFeedback({task,account,chats,onStop,onRetry,onSettings,onCleanup,blocked=false}:{task:Task;account:string;chats:{username:string;name:string}[];onStop:()=>void;onRetry:()=>void;onSettings:()=>void;onCleanup?:()=>void;blocked?:boolean}){
 const running=['running','cancelling'].includes(task.status),r=task.result;
 const [copyState,setCopyState]=useState('');
 useEffect(()=>setCopyState(''),[task.id]);
 const activity=task.activity?.startsWith('读取消息批次（')||task.activity?.startsWith('导入消息（')?task.activity.replace('读取消息批次（','正在读取聊天记录（').replace('导入消息（','正在导入聊天记录（').replace('已处理 ','本会话已读取来源记录 ').replace('复用 ','累计未变化或重复来源 ').replace('新增 ','累计新增消息 ').replace('变化 ','累计更新消息 '):task.activity;
 const count=task.unit==='字节'?`${byteCount(task.done)} / ${byteCount(task.total)}`:`${task.done.toLocaleString()} / ${task.total.toLocaleString()} ${task.unit||''}`;
 const currentFile=task.unit==='字节'||task.unit==='页';
 async function copy(){try{const response=await fetch('/api/runtime-summary');if(!response.ok)throw Error('无法读取程序位置');const runtime=await response.json();await navigator.clipboard.writeText(JSON.stringify({runtime,task:{id:task.id,action:task.action,status:task.status,stage:task.stage,failed_stage:task.failed_stage,reason:task.reason,error_location:task.error_location,diagnostic:task.diagnostic,next_step:task.next_step}},null,2));setCopyState('本次诊断已复制')}catch(e){setCopyState('复制失败：'+(e as Error).message)}}
 return <section className="task-feedback" aria-label={taskName(task.action)}>
  <div className="task-heading"><h3>{taskName(task.action)}</h3><span className={'tag '+(task.status==='failed'?'danger':'')} role="status">{task.status==='success'&&task.diagnostic_code==='partial'?'部分完成':states[task.status]}</span></div>
  <div className="task-progress" aria-busy={running}><strong>{taskStageLabel(task.stage)}</strong>{(task.total>0||running)&&<div className="progress-slot">{task.total>0?<><progress aria-label={taskStageLabel(task.stage)} value={task.done} max={task.total}/><span>{currentFile?'当前文件：':''}{count}</span></>:<span>{task.unit&&`${task.stage==='发现历史会话'?'已找到':'已处理'} ${task.unit==='字节'?byteCount(task.done):`${task.done.toLocaleString()} ${task.unit}`}；`}总量尚未确定，请稍候…</span>}</div>}{running&&activity&&<p className="task-activity">{activity}</p>}</div>
  <div className="task-actions row"><button disabled={!running||task.status==='cancelling'||!task.cancellable||blocked} onClick={onStop}>{task.status==='cancelling'?'正在停止…':running&&!task.cancellable?'请等待当前操作完成':'停止任务'}</button>{!running&&!blocked&&<button onClick={onRetry}>重试此任务</button>}{!running&&['extract','prepare'].includes(task.action)&&<button onClick={onSettings}>打开数据设置</button>}</div>
  {task.status==='cancelling'&&<p role="status">{task.action==='transcribe'?'已保存的转写保留；正在识别的在线语音需等待返回或超时。':'原有聊天记录保留，本次尚未保存的改动会取消。'}请等待停止完成。</p>}
  {running&&!task.cancellable&&task.status!=='cancelling'&&<p>请稍候，当前操作完成后才能停止或退出。仍可阅读已保存的聊天记录。</p>}
  {task.reason&&<p className="panel-alert task-error" role="alert">{task.reason}</p>}
  {onCleanup&&<div className="panel-alert"><p>任务尚未结束，已保存的结果保留。请重试清理临时文件，完成后可重试此任务或退出。</p><button onClick={onCleanup}>重试清理临时文件</button></div>}
  {r&&<dl className="task-results">{(fields[task.action]||[]).filter(([k])=>r[k]!==undefined).map(([k,label])=><React.Fragment key={k}><dt>{label}</dt><dd>{String(r[k])}</dd></React.Fragment>)}</dl>}
  {r?.keys_saved&&<p>本账号密钥已保存。</p>}
  {r?.errors?.map((e:{reason:string;count:number},i:number)=><details key={i} className="task-error"><summary>{e.count} 条语音识别失败 · 查看原因</summary><pre>{e.reason}</pre></details>)}
  {task.next_step&&!running&&<p>{task.next_step}</p>}
  <details className="task-scope"><summary>{task.selected_count?`${account} · 查看此次处理的会话（${task.selected_count}）`:`此次处理账号：${account}`}{task.voice_targets?` · ${task.voice_targets.length} 条语音`:''}</summary><div className="scope-list">{task.selected_count?task.selected_usernames?.map(u=><div className="selection-line" key={u}>{chats.find(c=>c.username===u)?.name||u}<small>{u}</small></div>):<small>账号编号：{task.account_id}</small>}</div></details>
  <details><summary>处理明细与诊断</summary><button onClick={copy}>复制本次诊断</button>{copyState&&<p role="status">{copyState}</p>}{task.unit==='字节'&&task.total>0&&<p>当前文件原始计数：{task.done.toLocaleString()} / {task.total.toLocaleString()} 字节</p>}{task.activity&&<p>{task.activity}</p>}{r?.databases!==undefined&&<p>已准备数据库：{r.databases}</p>}{r?.contacts!==undefined&&<p>通讯录条目：{r.contacts}</p>}{['update','update_all'].includes(task.action)&&r?.reused!==undefined&&<p>未变化或重复的来源记录：{r.reused}（可能包含同一消息的多个来源，不等于消息数）</p>}{task.failed_stage&&<p>失败时正在：{taskStageLabel(task.failed_stage)}</p>}{task.error_location&&<p>错误位置：{task.error_location}</p>}{r?.device&&<p>识别设备：{r.device}</p>}{r?.notes?.map((n:string)=><p key={n}>{n}</p>)}{task.diagnostic&&<pre>{JSON.stringify(task.diagnostic,null,2)}</pre>}</details>
 </section>
}
