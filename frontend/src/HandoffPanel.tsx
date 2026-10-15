import {credential, actorId, clearSession} from './session';
import {setRouteParams} from './navigation';
import React,{useEffect,useState} from 'react';

type Props={role:string;initialTicketId?:string};
const states:Record<string,string>={assigned:'待负责人接单',in_progress:'人工处理中',closed:'已结案'};
export function HandoffPanel({role,initialTicketId}:Props){
 const [tickets,setTickets]=useState<any[]>([]),[rows,setRows]=useState<any[]>([]),[team,setTeam]=useState<any[]>([]);
 const [ticketId,setTicketId]=useState(''),[assignee,setAssignee]=useState('');
 const [reason,setReason]=useState(''),[steps,setSteps]=useState(''),[checks,setChecks]=useState(''),[note,setNote]=useState(''),[reference,setReference]=useState('');
 const [showClosed,setShowClosed]=useState(false);
 const [busy,setBusy]=useState(false),[error,setError]=useState(''),[notice,setNotice]=useState('');
 const actor=actorId(role);
 async function api(path:string,body?:any){
  const response=await fetch('/api'+path,{method:body?'POST':'GET',headers:{Authorization:`Bearer ${credential(role)}`,'Content-Type':'application/json'},body:body?JSON.stringify(body):undefined});
  const result=await response.json();if(!response.ok)throw new Error(result.errors?.[0]?.message||'请求未通过，请检查输入并刷新');return result.data;
 }
 async function refresh(id=ticketId){const [ts,hs,ms]=await Promise.all([api('/tickets'),api('/handoffs'),api('/handoff-team'+(id?'?ticket_id='+encodeURIComponent(id):''))]);setTickets(ts.slice().reverse());setRows(hs.slice().reverse());setTeam(ms);}
 async function act(task:()=>Promise<void>){setBusy(true);setError('');setNotice('');try{await task();}catch(e){setError(e instanceof Error?e.message:String(e));}finally{setBusy(false);}}
 useEffect(()=>{setTickets([]);setRows([]);setTicketId(initialTicketId||'');setReason('');setSteps('');setChecks('');setNote('');setReference('');void act(()=>refresh(initialTicketId||''));},[role,initialTicketId]);
 const selected=tickets.find(t=>t.id===ticketId);
 const eligible=team.filter(m=>m.id!==actor);
 useEffect(()=>{if(ticketId)void api('/handoff-team?ticket_id='+encodeURIComponent(ticketId)).then(setTeam).catch(e=>setError(String(e)));},[ticketId]);
 useEffect(()=>{if(!eligible.some(m=>m.id===assignee))setAssignee(eligible.find(m=>m.role===(role==='supervisor'?'agent':'supervisor'))?.id||eligible[0]?.id||'');},[ticketId,team]);
 const queueTickets=role==='supervisor'?tickets.filter(t=>rows.some(h=>h.ticket_id===t.id&&(showClosed||h.state!=='closed'||t.id===initialTicketId))):tickets;
 const hand=rows.find(h=>h.ticket_id===ticketId);
 const dispatch=hand?.history.slice().reverse().find((event:any)=>['assigned','reassign','reopen'].includes(event.action));
 async function action(op:string){
  if(!hand||!selected)return;
  await api(`/handoffs/${hand.id}/actions`,{version:hand.version,action:op,note,reference:reference||null,assignee:op==='reassign'?assignee:null,ticket_version:op==='close'?selected.version:null});
  await refresh();setNotice('交接操作已记录；没有向客户发送消息。');
 }
 return <section className="card"><h2>{role==='supervisor'?'升级工单调度':'人工交接队列'} <button disabled={busy} onClick={()=>void act(refresh)}>刷新交接</button></h2>
 <p>{role==='supervisor'?'只列出已升级工单。核对原因、记录决策，再派给有订单授权的客服跟进。':'日常咨询由客服处理；需要额外权限或无法解决时，再升级交接。负责人接单后记录处理并结案。'}</p>
 {error&&<p role="alert" className="error">{error}</p>}{notice&&<p role="status">{notice}</p>}
 <p>待接单 {rows.filter(h=>h.state==='assigned').length} / 处理中 {rows.filter(h=>h.state==='in_progress').length} / 已结案 {rows.filter(h=>h.state==='closed').length}</p>
 {role==='supervisor'&&<label className="check"><input type="checkbox" checked={showClosed} onChange={e=>setShowClosed(e.target.checked)}/>包含已结案升级工单</label>}
 <label>{role==='supervisor'?'升级工单':'交接工单'}<select value={ticketId} onChange={e=>{setTicketId(e.target.value);setRouteParams({ticket:e.target.value});setNote('');setReference('');}}><option value="">选择工单</option>{queueTickets.map(t=><option key={t.id} value={t.id}>{t.id} · {t.messages.at(-1)?.text} · {rows.find(h=>h.ticket_id===t.id)?states[rows.find(h=>h.ticket_id===t.id).state]:'未交接'}</option>)}</select></label>
 {selected&&<><p>当前工单：{({new:'待处理',in_progress:'处理中',escalated:'待人工处理',resolved:'已解决',closed:'已关闭',reopened:'重新处理',waiting_approval:'待审批',waiting_execution:'待执行 / 对账'} as any)[selected.status]||selected.status} · 版本 {selected.version}</p><blockquote>{selected.messages.at(-1)?.text}</blockquote>
 {(!hand||role==='supervisor'&&hand.state!=='closed')&&<label>{hand?'重新指派给':'交接负责人'}<select value={assignee} onChange={e=>setAssignee(e.target.value)}>{eligible.map(m=><option key={m.id} value={m.id}>{m.label} · {m.role==='supervisor'?'主管':'客服'}</option>)}</select></label>}
 {!hand?(role==='supervisor'?<p className="muted">此工单尚未升级，由客服继续处理。</p>:<><label>交接原因<textarea value={reason} onChange={e=>setReason(e.target.value)}/></label><label>已尝试步骤（未尝试须写明）<textarea value={steps} onChange={e=>setSteps(e.target.value)}/></label><label>待核对事项 / 保留诉求<textarea value={checks} onChange={e=>setChecks(e.target.value)}/></label><button disabled={busy||!assignee||Math.min(reason.trim().length,steps.trim().length,checks.trim().length)<3||['closed','resolved'].includes(selected.status)} onClick={()=>void act(async()=>{await api(`/tickets/${selected.id}/handoffs`,{version:selected.version,assignee,reason,attempted_steps:steps,pending_checks:checks});await refresh();setNotice('已指派，等待负责人接单。');})}>创建人工交接</button></>):<>
 <h3>{states[hand.state]} · {team.find(m=>m.id===hand.assignee)?.label||hand.assignee}</h3><p>原因：{hand.reason}</p><p>已尝试：{hand.attempted_steps}</p><p>待核对：{hand.pending_checks}</p>
 {dispatch?.action==='reassign'&&<div className="notice"><strong>本次指派的处理要求</strong><p>{dispatch.note}</p></div>}
 {hand.snapshot.ticket.context_version!==selected.context_version&&<p className="notice">交接后客户信息已更新，请同时核对当前原文和历史快照。</p>}
 <details><summary>交接时原文、订单、依据与待办快照</summary><pre>{JSON.stringify(hand.snapshot,null,2)}</pre></details>
 <label>{role==='supervisor'?'处理决策 / 指派说明':'本次处理结果 / 操作理由'}<textarea value={note} onChange={e=>setNote(e.target.value)}/></label><label>处理凭据<input value={reference} onChange={e=>setReference(e.target.value)}/></label>
 <div className="actions">{hand.assignee===actor&&hand.state==='assigned'&&<button disabled={busy||note.trim().length<3} onClick={()=>void act(()=>action('accept'))}>负责人接单</button>}
 {hand.assignee===actor&&hand.state==='in_progress'&&<><button disabled={busy||note.trim().length<3} onClick={()=>void act(()=>action('note'))}>记录处理进展</button><button disabled={busy||note.trim().length<3||reference.trim().length<3} onClick={()=>void act(()=>action('close'))}>凭结果结案</button></>}
 {role==='supervisor'&&hand.state!=='closed'&&<button disabled={busy||!assignee||assignee===hand.assignee||note.trim().length<3} onClick={()=>void act(()=>action('reassign'))}>{assignee===hand.assignee?'已派给该负责人':team.find(m=>m.id===assignee)?.role==='agent'?'派给客服跟进':'转交负责人'}</button>}
 {hand.state==='closed'&&<button disabled={busy||note.trim().length<3} onClick={()=>void act(()=>action('reopen'))}>重开交接</button>}</div>
 {hand.resolution&&<p>结案结果：{hand.resolution.result} · {hand.resolution.reference}</p>}
 <h3>本轮处理记录</h3>{hand.notes.map((n:any,i:number)=><p key={i}>{n.at} · {n.actor} · {n.text} · {n.reference}</p>)}
 <details><summary>指派与状态历史 · {hand.history.length}条 / 已完成轮次 {hand.cycles.length}</summary><pre>{JSON.stringify({history:hand.history,cycles:hand.cycles},null,2)}</pre></details>
 </>}</>}
 </section>;
}
