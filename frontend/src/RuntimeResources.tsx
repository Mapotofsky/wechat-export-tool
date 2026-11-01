import {useState} from 'react';

type Api=(path:string,body?:unknown)=>Promise<any>;
export const bytes=(n:number|null)=>n===null?'未知':n<1024?`${n} B`:`${(n/1024/1024).toLocaleString(undefined,{maximumFractionDigits:1})} MiB`;

export function Capacity({info}:{info:any}){
 if(!info)return null;
 return <section aria-label="容量预检"><p>本次已知复制量 {bytes(info.copy_bytes)}；{info.blocked?'必要空间不足，请释放目标卷空间后重试。':'实际用量随任务变化。'}</p>
 {info.volumes.map((v:any)=><p key={v.volume}>{v.volume} · 可用 {bytes(v.free_bytes)} · 必要复制 {bytes(v.necessary_bytes)} · 重叠估计 {v.unknown_growth&&!v.estimated_bytes?'未知':bytes(v.estimated_bytes)}{v.unknown_growth&&!!v.estimated_bytes?'，另有待估计项':''}</p>)}
 <details><summary>容量、目标位置与准备文件</summary><p>当前档案及保留代次占用 {bytes(info.retained_bytes)}，已反映在可用空间中。</p>{info.volumes.map((v:any)=><p key={v.volume}>{v.purposes.join('、')}：{v.paths.join('；')}</p>)}{info.unknown.map((s:string)=><p key={s}>{s}</p>)}<div className="scope-list">{info.files.length?info.files.map((p:string)=><p key={p}>{p}</p>):<p>本任务不复制加密快照。</p>}</div></details></section>
}

export function RuntimeResources({api,native,task,onVoice}:{api:Api;native:boolean;task:any;onVoice:()=>void}){
 const [info,setInfo]=useState<any>(null),[tools,setTools]=useState<any>(null),[error,setError]=useState(''),[notice,setNotice]=useState(''),[working,setWorking]=useState(false);
 async function run(fn:()=>Promise<void>){setError('');setNotice('');setWorking(true);try{await fn()}catch(e){setError((e as Error).message)}finally{setWorking(false)}}
 async function load(){setInfo(await api('runtime-summary'))}
 async function open(kind:string){const result=await window.pywebview!.api.open_resource(kind);if(!result.opened)throw Error(result.error)}
 async function copy(){const t=task||{};const diagnostic={runtime:info||await api('runtime-summary'),task:{id:t.id,action:t.action,status:t.status,stage:t.stage,failed_stage:t.failed_stage,reason:t.reason,error_location:t.error_location,diagnostic:t.diagnostic,next_step:t.next_step},tools};await navigator.clipboard.writeText(JSON.stringify(diagnostic,null,2));setNotice('本次诊断已复制')}
 return <details className="settings-section" onToggle={e=>{if(e.currentTarget.open&&!info)run(load)}}><summary>程序与能力、数据位置及升级</summary>
 {error&&<p role="alert" className="panel-alert">{error}</p>}{notice&&<p role="status">{notice}</p>}
 {info&&<><p>{info.package_type} · Python {info.python}{info.version?' · 版本 '+info.version:''}{info.built_at?' · 构建 '+info.built_at:''}</p><p>阅读、检索、分析和导出可独立使用。</p><p>程序：{info.program}</p><p>数据：{info.data}</p><p>日志：{info.logs}</p>
 <div className="row">{native&&<><button disabled={working} onClick={()=>run(()=>open('data'))}>打开数据目录</button><button disabled={working} onClick={()=>run(()=>open('logs'))}>打开日志目录</button></>}<button disabled={working} onClick={()=>run(copy)}>复制本次诊断</button><button disabled={working} onClick={()=>run(async()=>{setTools(await api('asr-status'))})}>检查语音能力</button><button onClick={onVoice}>语音准备与设置</button></div>
 <p>本地语音需要语音运行时、Whisper 模型、FFmpeg 和 SILK 解码组件；在线模式仍需 FFmpeg 和 SILK，无需模型或 GPU。</p>
 {tools&&<p role="status">{tools.available?'已保存模式的工具可用':tools.reason||'检测失败'}{tools.reason&&tools.available?'；'+tools.reason:''}；{tools.device_name||tools.device||''}</p>}
 <p>补齐组件：将完整语音包的 voice-runtime、tools、models 保持完整放在程序根目录，或到语音设置指定已有模型。检查仅运行本地工具，不发送在线转写请求。</p>
 <details><summary>升级步骤</summary><ol><li>正常退出应用，记下上方实际数据目录。</li><li>新目录解压程序后，设置 WECHAT_ARCHIVE_DATA_DIR 指向原数据目录；原地升级则保留 local_data，替换程序及运行时。</li><li>重新启动并核对账号与阅读位置。外部源目录、手选密钥和模型继续按已保存路径访问。</li></ol><p>升级保留账号、消息、成功转写、无需转写标记、关注、项目、隐藏和阅读位置。</p></details></>}
 </details>
}
