import React,{useEffect,useState} from 'react';
import {credential,actorId} from './session';

export function ManagerHome({role,onNavigate}:{role:string;onNavigate:(page:string,ticketId?:string)=>void}){
 const [proposals,setProposals]=useState<any[]>([]),[handoffs,setHandoffs]=useState<any[]>([]),[issues,setIssues]=useState<any[]>([]);
 const [tasks,setTasks]=useState<any[]>([]);
 const [busy,setBusy]=useState(true),[error,setError]=useState('');
 const [revision,setRevision]=useState(0);
 useEffect(()=>{
  const controller=new AbortController();
  async function load(){setBusy(true);setError('');try{
   const data=await Promise.all(['/action-proposals','/handoffs','/issues','/tasks'].map(async path=>{
    const r=await fetch('/api'+path,{headers:{Authorization:`Bearer ${credential(role)}`},signal:controller.signal});
    const j=await r.json();if(!r.ok)throw Error(j.errors?.[0]?.message||'读取主管待办失败');return j.data;
   }));
   if(!controller.signal.aborted){setProposals(data[0]);setHandoffs(data[1]);setIssues(data[2]);setTasks(data[3]);}
  }catch(e){if(!controller.signal.aborted)setError(e instanceof Error?e.message:String(e));}finally{if(!controller.signal.aborted)setBusy(false);}}
  void load();return ()=>controller.abort();
 },[role,revision]);
 const approvals=proposals.filter(p=>p.state==='pending_approval'&&p.created_by!==actorId(role));
 const unknown=proposals.filter(p=>p.state==='unknown');
 const assigned=handoffs.filter(h=>h.assignee===actorId(role)&&h.state!=='closed');
 const review=issues.filter(i=>i.state==='under_review');
 return <div className="manager-home">
  <div className="flow-heading"><p className="manager-intro">客服负责接待和执行；你负责审批、异常决策与质量把关。</p><button disabled={busy} onClick={()=>setRevision(v=>v+1)}>刷新待办</button></div>
  {error&&<p className="error" role="alert">{error}</p>}
  {busy?<section className="card" role="status">正在读取主管待办…</section>:!error&&<>
   <div className="manager-todos">
    {[
     {title:'售后待审批',rows:approvals,page:'动作审批',empty:'没有待审批申请',description:'核对金额或补发数量，批准后交客服执行'},
     {title:'执行待对账',rows:unknown,page:'动作审批',empty:'没有结果未知的执行',description:'核实执行结果，避免客服重复退款或补发'},
     {title:'改进待验收',rows:tasks.filter(t=>t.state==='delivered'),page:'改进任务',empty:'没有等待验收的交付',description:'核对客服交付，确认实施后观察效果'},
     {title:'我的升级工单',rows:assigned,page:'人工交接',empty:'没有分派给你的升级工单',description:'决定处理方式，再指派客服跟进'}
    ].map(group=><section className="card" key={group.title}>
     <div className="flow-heading"><h2>{group.title}</h2><span className="badge">{group.rows.length}</span></div>
     <p className="muted">{group.description}</p>
     {group.rows.slice(0,4).map(item=><button className="manager-todo" key={item.id} onClick={()=>group.page==='改进任务'?(location.hash='/tasks?step=work&task='+encodeURIComponent(item.id)):onNavigate(group.page,item.ticket_id)}>
      <strong>{group.page==='改进任务'?'任务交付':item.type==='refund'?'退款':item.type==='reship'?'补发':'升级处理'}{item.amount?` · ${item.amount} ${item.currency}`:''}</strong>
      <span>{item.title||item.reason}</span>
     </button>)}
     {!group.rows.length&&<p className="manager-empty">{group.empty}</p>}
     {!!group.rows.length&&<button className="text-button" onClick={()=>onNavigate(group.page)}>查看全部 →</button>}
    </section>)}
   </div>
   <section className="card manager-routine"><h2>管理工作</h2><div className="actions">
    <button onClick={()=>onNavigate('商品问题')}>问题审核{review.length?` · ${review.length} 个待审`:''}</button>
    <button onClick={()=>onNavigate('改进任务')}>改进跟踪</button>
    <button onClick={()=>onNavigate('质量与效果')}>服务质检</button>
   </div></section>
  </>}
 </div>;
}
