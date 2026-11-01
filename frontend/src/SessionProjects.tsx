import React, {useEffect, useState} from 'react';

export type SessionGroup={id:string;name:string;usernames:string[]};
export type SessionMember={username:string;name:string;imported:boolean;hidden?:boolean};
export type GroupScope={id:string;name:string;members:SessionMember[];imported:SessionMember[];pending:SessionMember[]};
type Props={groups:SessionGroup[];initialId:string;targets:string[];chats:SessionMember[];busy:boolean;api:(path:string,body?:unknown)=>Promise<any>;run:(fn:()=>Promise<unknown>)=>Promise<void>;changed:()=>Promise<void>};

export function ProjectManager({groups,initialId,targets,chats,busy,api,run,changed}:Props){
 const [id,setId]=useState(initialId||'followed'),[name,setName]=useState(''),[newName,setNewName]=useState(''),[confirmDelete,setConfirmDelete]=useState(false);
 const group=groups.find(g=>g.id===id);
 useEffect(()=>{setName(group?.name||'');setConfirmDelete(false)},[id,group?.name]);
 async function save(usernames:string[],nextName=group?.name){if(!group)return;await api('groups',{id,name:nextName,usernames});await changed()}
 return <><p>一个会话可加入多个项目。关注会话是内置项目，用于日常关注更新。</p>
 <form onSubmit={e=>{e.preventDefault();run(async()=>{const r=await api('groups',{name:newName});setId(r.id);setNewName('');await changed()})}}><label>新项目名称<input value={newName} onChange={e=>setNewName(e.target.value)}/></label><button disabled={busy||!newName.trim()}>新建项目</button></form>
 <label>管理项目<select value={id} onChange={e=>setId(e.target.value)}>{groups.map(g=><option key={g.id} value={g.id}>{g.name}（{g.usernames.length}）</option>)}</select></label>
 {group&&<><label>项目名称<input value={name} disabled={id==='followed'} onChange={e=>setName(e.target.value)}/></label><div className="row"><button disabled={busy||id==='followed'||!name.trim()} onClick={()=>run(()=>save(group.usernames,name))}>保存名称</button>{id!=='followed'&&<button disabled={busy} onClick={()=>setConfirmDelete(true)}>删除项目</button>}</div>
 {confirmDelete&&<div className="panel-alert"><p>删除项目“{group.name}”后，聊天记录仍保留，会话不再归入此项目。</p><button disabled={busy} onClick={()=>run(async()=>{await api('groups',{id,delete:true});setId('followed');await changed()})}>确认删除项目</button><button onClick={()=>setConfirmDelete(false)}>取消</button></div>}
 <h3>加入会话</h3><p>已选 {targets.length} 个会话，可加入或移出当前项目。</p><details><summary>查看已选会话</summary>{targets.map(u=><div className="selection-line" key={u}>{chats.find(c=>c.username===u)?.name||u}<small>{u}</small></div>)}</details><div className="row"><button disabled={busy||!targets.length} onClick={()=>run(()=>save(Array.from(new Set([...group.usernames,...targets]))))}>将这些会话加入项目</button><button disabled={busy||!targets.some(u=>group.usernames.includes(u))} onClick={()=>run(()=>save(group.usernames.filter(u=>!targets.includes(u))))}>将这些会话移出项目</button></div>
 <h3>项目中的会话（{group.usernames.length}）</h3>{group.usernames.map(u=>{const c=chats.find(c=>c.username===u);return <div className="selection-line" key={u}><div>{c?.name||u}<small>{u}{c?.hidden?' · 已隐藏':c?.imported?' · 已归档':' · 待导入'}</small></div><button disabled={busy} onClick={()=>run(()=>save(group.usernames.filter(x=>x!==u)))}>移出</button></div>})}{!group.usernames.length&&<p className="muted">项目暂无会话。</p>}</>}
 </>;
}

export function HiddenManager({chats,busy,onRestore,onPurge}:{chats:SessionMember[];busy:boolean;onRestore:(usernames:string[])=>void;onPurge:(usernames:string[])=>void}){
 const [selected,setSelected]=useState<string[]>([]),[q,setQ]=useState('');
 const valid=selected.filter(u=>chats.some(c=>c.username===u));
 const rows=chats.filter(c=>(c.name+' '+c.username).toLowerCase().includes(q.toLowerCase()));
 return <><p>隐藏会话不参与日常列表、关注更新或项目操作；项目归属仍保留。</p><input aria-label="搜索隐藏会话" placeholder="搜索隐藏会话" value={q} onChange={e=>setQ(e.target.value)}/><div className="row"><button disabled={busy||!valid.length} onClick={()=>onRestore(valid)}>恢复所选（{valid.length}）</button><button disabled={busy||!valid.length} onClick={()=>onPurge(valid)}>清理所选档案…</button></div>{rows.map(c=><label className="selection-line" key={c.username}><input type="checkbox" checked={valid.includes(c.username)} onChange={()=>setSelected(s=>s.includes(c.username)?s.filter(u=>u!==c.username):[...s,c.username])}/><span>{c.name}<small>{c.username} · {c.imported?'保留档案':'待导入'}</small></span></label>)}{!rows.length&&<p className="muted">{chats.length?'没有匹配的隐藏会话。':'没有隐藏会话。'}</p>}</>;
}
