import React from 'react';

export default function SetupSummary({status,busy,onAction}:{status:any;busy:boolean;onAction:(action:string)=>void}){
 const s=status?.setup||{},a=status?.account;
 const parts=a?.directory.replace(/\\/g,'/').split('/')||[];
 const labels:Record<string,string>={directory:'选择并检查目录',extract:'提取密钥',prepare:'准备通讯录',identity:'查找并确认我的身份',read:'开始阅读或导入会话'};
 return <section className="panel-notice setup-summary" aria-label="准备进度">
  <strong>{a?.label||'尚未保存账号'}</strong>
  {a&&<details><summary>已保存的目录与身份</summary><p>已保存源目录：{a.directory}</p><p>账号子目录：{a.account_directory||(parts.at(-1)==='db_storage'?parts.at(-2):parts.at(-1))}</p>
   <p>本人：{status.config.self_username||'尚未设置本人身份'} / {status.config.my_name||'我'}</p></details>}
  <p>目录 {s.directory_saved?'已保存':'待选择'} · 密钥 {!a?'待判断':s.needs_keys?(status.config.keys_error?'需重新选择':s.has_keys?'已选择':'待提取'):'无需'} · 通讯录 {s.contacts_ready?'已准备':'待准备'} · 本人 {s.identity_saved?'已确认':'待选择'}</p>
  {s.blocked_reason&&<p>{s.blocked_reason}</p>}
  <button className="primary" disabled={busy} onClick={()=>onAction(s.next_action||'directory')}>{labels[s.next_action]||labels.directory}</button>
 </section>
}
