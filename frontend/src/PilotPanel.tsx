import {credential, actorId, clearSession} from './session';
import React,{useEffect,useState} from 'react';
import {ValidationSetupPanel} from './ValidationSetupPanel';
import {TrialPanel} from './TrialPanel';
import {TrialRecordsPanel} from './TrialRecordsPanel';
export function PilotPanel({role}:{role:string}){
 const [step,setStep]=useState(0),[visited,setVisited]=useState([0]);
 const steps=['验证计划','案例回放','结果记录','已有报告'];
 const [data,setData]=useState<any>(null),[error,setError]=useState('');
 useEffect(()=>{setData(null);setError('');if(role==='supervisor')void fetch('/api/pilot-readiness',{headers:{Authorization:`Bearer ${credential(role)}`}}).then(async r=>{const j=await r.json();if(!r.ok)throw new Error(j.errors?.[0]?.message||'读取失败');setData(j.data);}).catch(e=>setError(String(e)));},[role]);
 return <><nav className="task-page-nav" aria-label="商家验证步骤">{steps.map((label,index)=><button key={label} className={step===index?'active':''} onClick={()=>{setStep(index);setVisited(v=>v.includes(index)?v:[...v,index]);window.scrollTo(0,0);}}>{index+1}. {label}</button>)}</nav>
 {visited.includes(0)&&<div hidden={step!==0}><ValidationSetupPanel role={role}/></div>}{visited.includes(1)&&<div hidden={step!==1}><TrialPanel role={role}/></div>}{visited.includes(2)&&<div hidden={step!==2}><TrialRecordsPanel role={role}/></div>}<section className="card" hidden={step!==3}><h2>已有离线验证记录</h2><p>授权资料确认后，先回放脱敏历史咨询；保留问题、依据、实际输出和人工判断，再记录成对耗时。此页展示验证记录，不向客户发送、不调用资金或模型接口。</p>
 {role!=='supervisor'?<p>切换主管查看资料准备与验证记录。</p>:<>{error&&<p role="alert">{error}</p>}{data&&<><h3>{data.real_reports.length?'已有提供者声明的授权观测记录':'待提供授权资料 · 真实商家效果未验证'}</h3><ul>{data.checklist.map((s:string)=><li key={s}>{s}</li>)}</ul>
 <p>真实观测报告 {data.real_reports.length} / 离线检查报告 {data.local_reports.length}</p>
 {data.real_reports.map((r:any)=><article key={r.id}><h3>{r.id}</h3><p>样本 {r.sample_size} · 含审核/返工/维护的净节省 {r.net_saved_seconds} 秒 · 原人工错误 {r.baseline_errors} / 辅助错误 {r.assisted_errors} · 未知语义 {r.semantic_uncertain}</p><p>仅描述本次输入样本；授权和人工审核记录需负责人核实。</p></article>)}
 <details><summary>本地模板与执行入口</summary><pre>{'复制 data/pilot-template 为独立资料包，填写 manifest.json、政策资料和 observations.csv。\npython scripts/verify_merchant_pilot.py "资料包/manifest.json" --report output/pilot/merchant-first.json'}</pre><p>空白模板不会通过验证；保留原始依据，报告另存且不覆盖。</p></details></>}</>}
 </section></>;
}
