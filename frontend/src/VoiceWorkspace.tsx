import type {ReactNode} from 'react';
import {useEffect,useState} from 'react';

type Api=(path:string,body?:unknown)=>Promise<any>;
const defaults:Record<string,{url:string;model:string}>={
 local:{url:'',model:''},
 dashscope:{url:'https://dashscope.aliyuncs.com/api/v1/services/aigc/multimodal-generation/generation',model:'fun-asr-flash-2026-06-15'},
 openai:{url:'',model:'whisper-1'}
};

export function VoiceAvailability({info,checking=false}:{info:any;checking?:boolean}){
 if(checking)return <p role="status">正在检查本机语音工具…</p>;
 if(!info)return <p className="muted">还没有检查结果。使用已保存的设置检查，不会开始转写。</p>;
 const online=info.provider&&info.provider!=='local',partial=info.available&&info.silk===false;
 return <div role="status"><p><strong>{info.available?(partial?'识别组件可用，部分微信语音仍缺少解码组件':online?'本机音频处理已就绪':'本地语音识别已就绪'):'语音识别尚未就绪'}</strong></p>
 {!info.available&&<p>{info.missing?.length?'缺少：'+info.missing.join('、')+'。':'工具检查未通过。'}{info.missing?.length?(info.missing.every((m:string)=>m==='本地语音模型')?'请在语音设置中选择已有模型，或使用完整语音版。':'请使用完整语音版补齐所缺组件，再重新检查。'):'请展开检查详情，按原因处理后重新检查。'}</p>}
 {partial&&<p>部分微信语音需要 SILK 解码组件，可使用完整语音版补齐后重新检查。</p>}
 {online&&<p className="muted">在线服务能否识别将在转写时确认；本次检查不会上传语音或产生识别费用。</p>}
 <details><summary>语音检查详情</summary>{info.reason&&<p>{info.reason}</p>}{!online&&<p>识别设备：{info.device_name||info.device||'未检测'}{info.device_reason?'；'+info.device_reason:''}</p>}{info.python&&<p>语音运行时：{info.python}</p>}{info.models?.map((m:string)=><p key={m}>模型：{m}</p>)}</details></div>
}

export function VoiceSettings({config,busy,api,changed}:{config:Record<string,any>;busy:boolean;api:Api;changed:()=>Promise<any>}){
 const [provider,setProvider]=useState(config.asr_provider||'local'),[url,setUrl]=useState(config.asr_api_url||''),[model,setModel]=useState(config.asr_api_model||''),[localModel,setLocalModel]=useState(config.asr_model||''),[key,setKey]=useState(''),[info,setInfo]=useState<any>(null),[message,setMessage]=useState(''),[error,setError]=useState(''),[working,setWorking]=useState(false),[checking,setChecking]=useState(false);
 async function run(fn:()=>Promise<void>){setWorking(true);setError('');setMessage('');try{await fn()}catch(e){setError(String((e as Error).message))}finally{setWorking(false)}}
 return <section aria-label="语音识别配置">{error&&<p role="alert" className="panel-alert">{error}</p>}{message&&<p role="status">{message}</p>}
 <VoiceAvailability info={info} checking={checking}/><label>识别方式<select value={provider} disabled={busy||working} onChange={e=>{const p=e.target.value;setProvider(p);setUrl(defaults[p].url);setModel(defaults[p].model);setInfo(null);setMessage('');setError('')}}><option value="local">本地识别（Whisper）</option><option value="dashscope">阿里 Fun-ASR-Flash</option><option value="openai">自定义在线识别（OpenAI 兼容）</option></select></label>
 {provider==='local'?<><p className="muted">在本机识别，不上传语音；自动选择可用的显卡或 CPU。</p><details><summary>高级：本地模型路径</summary><label>Whisper .pt 模型<input value={localModel} onChange={e=>setLocalModel(e.target.value)} placeholder="留空自动使用随包 small.pt"/></label></details></>:<><p className="muted">执行转写时会将待识别语音上传至下方地址，可能产生服务费用。已有成功转写不会重复识别；手动重新识别除外。</p><label>完整 API 地址<input value={url} onChange={e=>setUrl(e.target.value)} placeholder="https://服务地址/v1/audio/transcriptions"/></label><label>在线模型名<input value={model} onChange={e=>setModel(e.target.value)}/></label><label>API Key<input type="password" autoComplete="off" value={key} onChange={e=>setKey(e.target.value)} placeholder={config.asr_api_key_set?'已保存；留空保留原 Key':'填入 API Key；无鉴权接口可留空'}/></label>{config.asr_api_key_set&&<button disabled={busy||working} onClick={()=>run(async()=>{await api('settings',{asr_provider:'local',asr_api_key:''});setProvider('local');setKey('');await changed();setMessage('API Key 已清除，已切换到本地识别')})}>清除已保存 Key</button>}{provider==='dashscope'&&<p className="muted">使用支持 Base64 的 Fun-ASR-Flash 模型；可修改为对应地域或业务空间的接口地址。</p>}</>}
 <div className="row"><button disabled={busy||working} onClick={()=>run(async()=>{await api('settings',{asr_provider:provider,...(provider==='local'?{asr_model:localModel}:{asr_api_url:url,asr_api_model:model,...(key?{asr_api_key:key}:{})})});setKey('');await changed();setInfo(null);setMessage('语音设置已保存')})}>{working?'正在处理…':'保存语音设置'}</button><button disabled={working} onClick={()=>run(async()=>{setChecking(true);setInfo(null);try{setInfo(await api('asr-status'))}finally{setChecking(false)}})}>检查已保存设置</button></div>
 </section>
}

type Target={username:string;message_id:string;label:string};
type Voice=Target&{time:string;sender:string;transcription:{status:string;text:string;reason:string;media_source?:{provider?:string;model?:string;device_name?:string;device?:string}}};
const statuses:Record<string,string>={not_requested:'未转写',success:'成功',failed:'失败',media_missing:'媒体缺失',tool_unavailable:'工具不可用',skipped:'无需转写'};
const id=(t:Target)=>JSON.stringify([t.username,t.message_id]);
export function VoiceManager({accountId,usernames,chats,task,feedback,api,changed,onSettings,onSubmit}:{accountId:string;usernames:string[];chats:{username:string;name:string}[];task:any;feedback:ReactNode;api:Api;changed:()=>Promise<any>;onSettings:()=>void;onSubmit:(users:string[],targets:{username:string;message_id:string}[])=>Promise<void>}){
 const [rows,setRows]=useState<Voice[]>([]),[total,setTotal]=useState(0),[offset,setOffset]=useState(0),[status,setStatus]=useState(''),[query,setQuery]=useState(''),[q,setQ]=useState(''),[selection,setSelection]=useState<Target[]>([]),[error,setError]=useState(''),[loading,setLoading]=useState(false),[submitting,setSubmitting]=useState(false),[review,setReview]=useState(false),[revision,setRevision]=useState(0),[dismissedTask,setDismissedTask]=useState('');
 const busy=['running','cancelling'].includes(task?.status||'');
 useEffect(()=>{let alive=true;setLoading(true);setError('');api('voices',{usernames,status,q,offset}).then(r=>{if(alive){setRows(r.messages);setTotal(r.total)}}).catch(e=>{if(alive)setError(e.message)}).finally(()=>{if(alive)setLoading(false)});return()=>{alive=false}},[api,usernames,status,q,offset,revision,task?.id,task?.status]);
 const name=(u:string)=>chats.find(c=>c.username===u)?.name||u;
 const target=(v:Voice):Target=>({username:v.username,message_id:v.message_id,label:`${name(v.username)} · ${v.sender||'未知发送者'} · ${v.time}`});
 async function submit(){setSubmitting(true);setError('');try{await onSubmit([...new Set(selection.map(t=>t.username))],selection.map(({username,message_id})=>({username,message_id})));setRevision(v=>v+1)}catch(e){setError((e as Error).message)}finally{setSubmitting(false)}}
 async function mark(targets:Target[],skipped:boolean){setSubmitting(true);setError('');try{await api('voice-skip',{account_id:accountId,targets:targets.map(({username,message_id})=>({username,message_id})),skipped});setSelection([]);setDismissedTask(task?.id||'');setRevision(v=>v+1);await changed()}catch(e){setError((e as Error).message)}finally{setSubmitting(false)}}
 return <section className="voice-manager"><details><summary>查看这 {usernames.length} 个会话</summary><div className="scope-list">{usernames.map(u=><p key={u}>{name(u)}<small>{u}</small></p>)}</div></details><p>重新识别使用已保存的语音设置。识别失败时，已有成功转写仍保留。</p><button onClick={onSettings}>语音识别设置</button>
 <form className="row" onSubmit={e=>{e.preventDefault();setQ(query.trim());setOffset(0)}}><input aria-label="搜索语音转写或发送者" value={query} onChange={e=>setQuery(e.target.value)} placeholder="搜索转写或发送者"/><button>搜索</button><select aria-label="语音转写状态" value={status} onChange={e=>{setStatus(e.target.value);setOffset(0)}}><option value="">全部状态</option>{Object.entries(statuses).map(([s,label])=><option key={s} value={s}>{label}</option>)}</select></form>
 {error&&<p className="panel-alert" role="alert">{error}</p>}
 {task?.id!==dismissedTask&&feedback}
 <div className="row"><span>共 {total} 条；已选 {selection.length} 条，当前页外 {selection.filter(t=>!rows.some(v=>id(v)===id(t))).length} 条</span><button disabled={loading||submitting} onClick={()=>setSelection(s=>[...s,...rows.filter(v=>!s.some(t=>id(t)===id(v))).map(target)])}>全选本页</button><button onClick={()=>setReview(v=>!v)}>核对全部选择</button><button disabled={submitting} onClick={()=>setSelection([])}>清空选择</button></div>
 {review&&<div className="voice-selection">{selection.length?selection.map(t=><div className="selection-line" key={id(t)}><span>{t.label}<small>{t.message_id}</small></span><button onClick={()=>setSelection(s=>s.filter(x=>id(x)!==id(t)))}>取消选择</button></div>):<p>尚未选择语音</p>}</div>}
 <div className="row"><button disabled={busy||loading||submitting||!selection.length} onClick={()=>mark(selection,true)}>所选标记无需转写</button><button disabled={busy||loading||submitting||!selection.length} onClick={()=>mark(selection,false)}>恢复所选转写</button></div><p className="muted">识别失败或找不到语音的条目可标记“无需转写”，以后会跳过。恢复后可再次识别；成功转写不受影响。</p>
 <button className="primary" disabled={busy||loading||submitting||!selection.length} onClick={submit}>{submitting?'正在开始识别…':`重新识别所选 ${selection.length} 条`}</button>
 {loading?<p role="status">正在读取语音…</p>:rows.length?rows.map(v=><article className="message" key={id(v)}><label className="check"><input type="checkbox" disabled={submitting} checked={selection.some(t=>id(t)===id(v))} onChange={e=>setSelection(s=>e.target.checked?[...s,target(v)]:s.filter(t=>id(t)!==id(v)))}/><strong>{name(v.username)} · {v.sender||'身份未知'}</strong><time>{v.time}</time><span className="tag">{statuses[v.transcription.status]||v.transcription.status}</span></label><div className="message-body">{v.transcription.text||'暂无转写文本'}</div>{v.transcription.status==='skipped'?<button disabled={busy||submitting} onClick={()=>mark([target(v)],false)}>恢复转写</button>:['failed','media_missing','tool_unavailable'].includes(v.transcription.status)&&<button disabled={busy||submitting} onClick={()=>mark([target(v)],true)}>无需转写</button>}{v.transcription.reason&&<details className="task-error"><summary>查看失败原因</summary><p>{v.transcription.status==='success'?'最近重试失败：':''}{v.transcription.reason}</p></details>}{v.transcription.media_source?.provider&&<small>来源：{v.transcription.media_source.provider} · {v.transcription.media_source.model} · {v.transcription.media_source.device_name||v.transcription.media_source.device}</small>}</article>):<p>当前条件下没有语音消息</p>}
 <div className="row"><button disabled={loading||offset===0} onClick={()=>setOffset(v=>Math.max(0,v-50))}>更新 50 条</button><span>{total?`${offset+1}–${Math.min(offset+50,total)} / ${total}`:'0 条'}</span><button disabled={loading||offset+50>=total} onClick={()=>setOffset(v=>v+50)}>更早 50 条</button><button onClick={()=>setRevision(v=>v+1)}>刷新</button></div>
 </section>
}
