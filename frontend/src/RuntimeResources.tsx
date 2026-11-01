import {useState} from 'react';
import {VoiceAvailability} from './VoiceWorkspace';

type Api=(path:string,body?:unknown)=>Promise<any>;
export const bytes=(n:number|null)=>{if(n===null)return '未知';const [unit,scale]=n<1000?['B',1]:n<1e6?['KB',1000]:n<1e9?['MB',1e6]:['GB',1e9];return `${(n/Number(scale)).toLocaleString(undefined,{maximumFractionDigits:1})} ${unit}`};

export function Capacity({info}:{info:any}){
 if(!info)return null;
 return <section aria-label="所需磁盘空间"><h4>所需磁盘空间</h4>
 <div className="capacity-volumes">{info.volumes.map((v:any)=><div key={v.volume}><strong>{v.volume}</strong><span>预计新增占用：{v.estimated_bytes>0?`约 ${bytes(v.estimated_bytes)}（部分估算）`:'尚无法估算'}</span><span>当前可用：{bytes(v.free_bytes)}</span></div>)}</div>
 <p className={info.blocked?'panel-alert':'muted'}>{info.blocked?'空间不足，请先释放相应磁盘的空间，再重试。':'尚未计入全部用量，无法确定空间是否足够。'}</p>
 <details><summary>查看空间估算明细</summary><p>预计新增占用包含复制和解密文件同时保留时的用量；聊天记录增长、临时文件等尚未完整估算。</p><p>已有数据占用 {bytes(info.retained_bytes)}，已计入磁盘当前占用，不重复加到新增用量中。</p>
 {info.volumes.map((v:any)=><div key={v.volume}><p>{v.volume} · 已确定需要复制 {bytes(v.necessary_bytes)}{v.unknown_growth?'；临时用量尚无法估算':''}</p><p>{v.purposes.join('、')}：{v.paths.join('；')}</p></div>)}
 {info.unknown.map((s:string)=><p key={s}>{s}</p>)}<details><summary>本次准备的文件（{info.files.length}）</summary><div className="scope-list">{info.files.length?info.files.map((p:string)=><p key={p}>{p}</p>):<p>无需复制加密数据；导入或临时文件仍可能占用空间。</p>}</div></details></details></section>
}

export function RuntimeResources({api,native,task,onVoice}:{api:Api;native:boolean;task:any;onVoice:()=>void}){
 const [info,setInfo]=useState<any>(null),[tools,setTools]=useState<any>(null),[error,setError]=useState(''),[notice,setNotice]=useState(''),[working,setWorking]=useState(false),[checking,setChecking]=useState(false);
 async function run(fn:()=>Promise<void>){setError('');setNotice('');setWorking(true);try{await fn()}catch(e){setError((e as Error).message)}finally{setWorking(false)}}
 async function load(){setInfo(await api('runtime-summary'))}
 async function open(kind:string){const result=await window.pywebview!.api.open_resource(kind);if(!result.opened)throw Error(result.error)}
 async function copy(){const t=task||{};const diagnostic={runtime:info||await api('runtime-summary'),task:{id:t.id,action:t.action,status:t.status,stage:t.stage,failed_stage:t.failed_stage,reason:t.reason,error_location:t.error_location,diagnostic:t.diagnostic,next_step:t.next_step},tools};await navigator.clipboard.writeText(JSON.stringify(diagnostic,null,2));setNotice('本次诊断已复制')}
 return <details className="settings-section" onToggle={e=>{if(e.currentTarget.open&&!info)run(load)}}><summary>数据位置、语音准备与升级</summary>
 {error&&<p role="alert" className="panel-alert">{error}</p>}{notice&&<p role="status">{notice}</p>}
 {info&&<><p>{info.package_type}</p><p>数据目录：{info.data}</p><p>日志目录：{info.logs}</p>
 <div className="row">{native&&<><button disabled={working} onClick={()=>run(()=>open('data'))}>打开数据目录</button><button disabled={working} onClick={()=>run(()=>open('logs'))}>打开日志目录</button></>}<button disabled={working} onClick={()=>run(copy)}>复制本次诊断</button></div>
 <h4>语音识别</h4><VoiceAvailability info={tools} checking={checking}/><div className="row"><button disabled={working} onClick={()=>run(async()=>{setChecking(true);setTools(null);try{setTools(await api('asr-status'))}finally{setChecking(false)}})}>检查已保存设置</button><button onClick={onVoice}>打开语音设置</button></div>
 <details><summary>程序与工具详情</summary><p>程序目录：{info.program}</p><p>Python {info.python}{info.version?' · 资源版本 '+info.version:''}{info.built_at?' · 更新时间 '+info.built_at:''}</p><p>本地识别需要 Whisper、模型和音频处理组件；在线识别无需本地模型，但仍需 FFmpeg 转换音频及 SILK 解码微信语音。</p></details>
 <details><summary>如何升级并保留记录</summary><ol><li>正常退出应用，保留上方数据目录。</li><li>原地升级时替换程序和运行时，保留 local_data；解压到新目录时，继续使用原数据目录。</li><li>启动后核对账号和阅读位置。原有消息、转写、无需转写标记、关注和项目都保留。</li></ol><details><summary>使用外置数据目录</summary><p>新程序通过 WECHAT_ARCHIVE_DATA_DIR 指向原数据目录。外部源目录、手选密钥和模型继续使用已保存的路径。</p></details></details></>}
 </details>
}
