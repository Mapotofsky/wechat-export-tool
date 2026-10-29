import React from 'react';

export type Task={id:string;account_id:string;action:string;status:string;stage:string;activity?:string;unit?:string;done:number;total:number;cancellable?:boolean;reason:string;next_step:string;selected_count:number;selected_usernames:string[];scope:string;voice_targets?:{username:string;message_id:string}[];diagnostic?:Record<string,unknown>;diagnostic_code?:string;failed_stage?:string;error_location?:string;result?:Record<string,any>};
export const taskName=(action:string)=>({extract:'提取密钥',prepare:'准备通讯录',discover:'发现历史会话',update:'导入 / 更新档案',update_all:'更新全部档案',transcribe:'语音转写'} as Record<string,string>)[action]||'处理任务';
const states:Record<string,string>={running:'进行中',cancelling:'正在停止',cancelled:'已停止',success:'已完成',failed:'失败',interrupted:'已中断'};
const fields:Record<string,[string,string][]>= {prepare:[['databases','已准备数据库'],['contacts','通讯录条目']],discover:[['contacts','通讯录条目']],update:[['added','新增消息'],['changed','更新消息'],['reused','复用记录']],update_all:[['added','新增消息'],['changed','更新消息'],['reused','复用记录']],transcribe:[['success','新转写成功'],['reused','复用成功结果'],['skipped','无需转写 · 已跳过'],['missing','媒体缺失'],['failed','失败'],['remaining','未处理']]};
export default function TaskFeedback({task,account,chats,onStop,onRetry,onSettings,onCleanup,blocked=false}:{task:Task;account:string;chats:{username:string;name:string}[];onStop:()=>void;onRetry:()=>void;onSettings:()=>void;onCleanup?:()=>void;blocked?:boolean}){
 const running=['running','cancelling'].includes(task.status),r=task.result;
 return <section className="task-feedback" aria-label={taskName(task.action)}>
  <div className="task-heading"><h3>{taskName(task.action)}</h3><span className={'tag '+(task.status==='failed'?'danger':'')} role="status">{task.diagnostic_code==='partial'?'部分完成':states[task.status]}</span></div>
  <div className="task-progress" aria-busy={running}><strong>{['完成','已停止'].includes(task.stage)?'本次处理进度':task.stage}</strong><div className="progress-slot">{task.total>0?<><progress aria-label={task.stage} value={task.done} max={task.total}/><span>{task.done.toLocaleString()} / {task.total.toLocaleString()} {task.unit}</span></>:<span>{running?`${task.done>0?`已发现 ${task.done} ${task.unit}；`:''}正在处理，当前阶段总量尚未确定。`:''}</span>}</div><p className="task-activity">{running?task.activity||'正在处理当前阶段…':''}</p></div>
  <div className="task-actions row"><button disabled={!running||task.status==='cancelling'||!task.cancellable||blocked} onClick={onStop}>{task.status==='cancelling'?'正在停止…':running&&!task.cancellable?'等待提交完成':'停止任务'}</button>{!running&&!blocked&&<button onClick={onRetry}>按原范围重新执行</button>}{!running&&['extract','prepare'].includes(task.action)&&<button onClick={onSettings}>返回数据设置</button>}</div>
  {task.status==='cancelling'&&<p role="status">正在等待当前工作结束并释放资源。{task.action==='transcribe'?'已保存的成功转写保留；已发出的在线请求需等待响应或超时。':'本次未提交的修改将撤销，原档案仍可阅读。'}</p>}
  {running&&!task.cancellable&&<p>正在提交或清理资源，请等待实际结果；仍可阅读已发布档案。</p>}
  {task.reason&&<p className="panel-alert task-error" role="alert">{task.reason}</p>}
  {onCleanup&&<div className="panel-alert"><p>任务资源尚未清理完成，已发布结果保留；清理完成后可重试任务或退出。</p><button onClick={onCleanup}>重试任务资源清理</button></div>}
  {r&&<dl className="task-results">{(fields[task.action]||[]).filter(([k])=>r[k]!==undefined).map(([k,label])=><React.Fragment key={k}><dt>{label}</dt><dd>{String(r[k])}</dd></React.Fragment>)}</dl>}
  {r?.keys_saved&&<p>本账号密钥已保存。</p>}
  {r?.errors?.map((e:{reason:string;count:number},i:number)=><details key={i} className="task-error"><summary>{e.count} 条语音失败：{e.reason.split('\n')[0].slice(0,160)}</summary><pre>{e.reason}</pre></details>)}
  {task.next_step&&!running&&<p>{task.next_step}</p>}
  <details className="task-scope"><summary>{account} · {task.voice_targets?`${task.voice_targets.length} 条语音 / `:''}{task.selected_count?`${task.selected_count} 个会话`:'当前账号'} · 核对固定范围</summary><div className="scope-list">{task.selected_usernames?.map(u=><div className="selection-line" key={u}>{chats.find(c=>c.username===u)?.name||u}<small>{u}</small></div>)}</div></details>
  {(task.diagnostic||task.failed_stage||task.error_location||r?.device||r?.notes?.length)&&<details><summary>诊断与工具详情</summary>{task.failed_stage&&<p>失败阶段：{task.failed_stage}</p>}{task.error_location&&<p>错误位置：{task.error_location}</p>}{r?.device&&<p>识别设备：{r.device}</p>}{r?.notes?.map((n:string)=><p key={n}>{n}</p>)}{task.diagnostic&&<pre>{JSON.stringify(task.diagnostic,null,2)}</pre>}</details>}
 </section>
}
