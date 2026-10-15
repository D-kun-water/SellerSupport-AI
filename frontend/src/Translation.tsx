import React, {useEffect, useState, useRef} from 'react';
import {credential,actorId} from './session';
import {Icon} from './Icon';
import {recoverEditor} from './editorCache';

type Context={role:string;ticketId:string;contextVersion:number};
type Result={key:string;status:'waiting'|'done'|'error';text:string;error:string};
class TranslationFailure extends Error {
 constructor(message:string,readonly recoverable=false){super(message);}
}

// Remove authoring boilerplate from generated customer copy, while keeping raw runs and evidence intact.
export function customerReply(text:string){return text.replace(/the test product /g,'product ').replace(/test device /g,'device ').replace(/test return time window/g,'return time window');}

// Limit automatic requests from long conversations to two concurrent HTTP calls.
let active=0;
const waiters:Array<()=>void>=[];
async function request(path:string,body:unknown,role:string,signal:AbortSignal){
 if(active>=2)await new Promise<void>(resolve=>waiters.push(resolve));
 else active++;
 try{
  if(signal.aborted)throw new DOMException('Cancelled','AbortError');
  for(let attempt=0;attempt<2;attempt++){
   let response:Response;
   try{response=await fetch(path,{method:'POST',headers:{Authorization:`Bearer ${credential(role)}`,'Content-Type':'application/json'},body:JSON.stringify(body),signal});}
   catch(error){if(signal.aborted)throw error;throw new TranslationFailure('翻译连接中断，原文已保留。',true);}
   const json=await response.json().catch(()=>null);
   if(!response.ok){
    const code=json?.errors?.[0]?.code;
    const transient=['BUSY','TRANSLATION_TIMEOUT','PROVIDER_RATE_LIMIT','PROVIDER_HTTP_ERROR','PROVIDER_TIMEOUT','PROVIDER_NETWORK'].includes(code)||(!json&&response.status>=500);
    if(attempt===0&&transient){await new Promise<void>((resolve,reject)=>{const stop=()=>{clearTimeout(timer);reject(new DOMException('Cancelled','AbortError'));};const timer=window.setTimeout(()=>{signal.removeEventListener('abort',stop);resolve();},900);signal.addEventListener('abort',stop,{once:true});if(signal.aborted)stop();});continue;}
    // A missing route can occur while an older server is being replaced. Domain 404s
    // (missing/unauthorized tickets) must never be treated as a startup failure.
    if(response.status===404&&!code)throw new TranslationFailure('翻译接口暂不可用（HTTP 404），原文已保留。',true);
    throw new TranslationFailure(json?.errors?.[0]?.message||`翻译服务暂不可用（HTTP ${response.status}），原文已保留。`);
   }
   if(typeof json?.data?.translation!=='string')throw Error('翻译结果格式异常，原文已保留。');
   return json.data.translation as string;
  }
  throw Error('翻译暂不可用，原文已保留。');
 }finally{const next=waiters.shift();if(next)next();else active--;}
}

function useAutoTranslation(context:Context,text:string,target:'zh'|'en',messageIndex?:number,composing=false,delay=target==='en'?700:150){
 const key=JSON.stringify([context.role,context.ticketId,context.contextVersion,text,target,messageIndex,composing]);
 const [result,setResult]=useState<Result>({key:'',status:'waiting',text:'',error:''});
 useEffect(()=>{
  if(!text.trim()||composing)return;
  const controller=new AbortController();
  let retryTimer:number|undefined,failed=false,inFlight=false,retries=0,lastRecovery=0;
  setResult({key,status:'waiting',text:'',error:''});
  const body=context.ticketId?{context_version:context.contextVersion,target,...(messageIndex===undefined?{text}:{message_index:messageIndex})}:{text,target};
  const path=context.ticketId?`/api/tickets/${encodeURIComponent(context.ticketId)}/translation`:'/api/translation-preview';
  function scheduleRecovery(){
   if(controller.signal.aborted||!failed||retries>=3)return;
   clearTimeout(retryTimer);
   retryTimer=window.setTimeout(()=>{retries++;void recover();},[2000,5000,10000][retries]);
  }
  async function run(){
   if(controller.signal.aborted||inFlight)return;
   inFlight=true;failed=false;
   setResult({key,status:'waiting',text:'',error:''});
   try{
    const translated=await request(path,body,context.role,controller.signal);
    if(!controller.signal.aborted)setResult({key,status:'done',text:translated,error:''});
   }catch(error){
    if(!controller.signal.aborted){
     failed=error instanceof TranslationFailure&&error.recoverable;
     setResult({key,status:'error',text:'',error:error instanceof Error?error.message:'翻译失败，原文已保留。'});
     scheduleRecovery();
    }
   }finally{inFlight=false;}
  }
  async function recover(){
   if(controller.signal.aborted||!failed||inFlight)return;
   lastRecovery=Date.now();inFlight=true;
   let ready=false;
   try{
    const response=await fetch('/api/health',{cache:'no-store',signal:controller.signal});
    const json=await response.json();
    ready=response.ok&&json?.data?.translation_api_version===1&&json?.data?.model?.translation_enabled===true&&json?.data?.model?.configured===true;
   }catch{/* Keep the source and error visible while the service is offline. */}
   finally{inFlight=false;}
   if(ready){clearTimeout(retryTimer);await run();}else scheduleRecovery();
  }
  const resume=()=>{if(document.visibilityState==='visible'&&Date.now()-lastRecovery>=10000)void recover();};
  const timer=window.setTimeout(()=>{void run();},delay);
  window.addEventListener('focus',resume);window.addEventListener('online',resume);document.addEventListener('visibilitychange',resume);
  return()=>{clearTimeout(timer);clearTimeout(retryTimer);controller.abort();window.removeEventListener('focus',resume);window.removeEventListener('online',resume);document.removeEventListener('visibilitychange',resume);};
 },[key]);
 return result.key===key?result:{key,status:'waiting' as const,text:'',error:''};
}

export function ChineseMeaning({context,text,messageIndex}:{context:Context;text:string;messageIndex?:number}){
 const result=useAutoTranslation(context,text,'zh',messageIndex);
 if(!text.trim())return null;
 return <div className="translation-result" aria-live="polite"><strong>中文译文</strong>
  {result.status==='done'?<p>{result.text}</p>:result.status==='error'?<p className="translation-error">⚠ 翻译失败：{result.error}</p>:<p className="muted">◌ 正在自动翻译…</p>}
 </div>;
}

export function CustomerMessages({context,messages}:{context:Context;messages:{text:string;role?:string}[]}){
 return <div className="bilingual-messages">{messages.map((message,index)=><article key={index+':'+message.text}>
  <strong>{!message.role||message.role==='customer'?'顾客原文':message.role==='agent'?'客服记录':'会话记录'}</strong><p>{message.text}</p>
  {(!message.role||message.role==='customer')&&<ChineseMeaning context={context} text={message.text} messageIndex={index}/>}
 </article>)}</div>;
}

export function ReplyComposer({context,draft,onDraftChange,onPendingChange}:{context:Context;draft:string;onDraftChange:(text:string)=>void;onPendingChange:(pending:boolean)=>void}){
 const initialSide=/[\u3400-\u9fff]/.test(draft)?'zh':'en';
 const cacheKey=`seller-editor:${actorId(context.role)}:${context.ticketId}:${context.contextVersion}`;
 const [source,setSource]=useState(()=>{
  try{const cached=recoverEditor(JSON.parse(sessionStorage.getItem(cacheKey)||'null'),draft);if(cached)return cached;}catch{}
  return {side:initialSide as 'zh'|'en',text:customerReply(draft)};
 }),[composing,setComposing]=useState(false);
 const lastDraft=useRef(draft);
 const baseDraft=useRef(draft);
 const publish=(value:string)=>{lastDraft.current=value;onDraftChange(value);};
 // A newly generated/loaded answer becomes the source; translated output never starts a reverse loop.
 useEffect(()=>{if(draft!==lastDraft.current){baseDraft.current=draft;lastDraft.current=draft;setSource({side:/[\u3400-\u9fff]/.test(draft)?'zh':'en',text:customerReply(draft)});}},[draft]);
 const result=useAutoTranslation(context,source.text,source.side==='zh'?'en':'zh',undefined,composing,700);
 const pending=!!source.text.trim()&&(composing||(source.side==='zh'&&result.status!=='done'));
 const chinese=source.side==='zh'?source.text:result.status==='done'?result.text:'';
 const english=source.side==='en'?source.text:result.status==='done'?result.text:'';
 useEffect(()=>{onPendingChange(pending);},[pending,onPendingChange]);
 useEffect(()=>{if(source.side==='zh')publish(result.status==='done'&&!composing?result.text:'');else if(source.text!==draft)publish(source.text);},[result.key,result.status,result.text,source.side,composing]);
 useEffect(()=>{try{sessionStorage.setItem(cacheKey,JSON.stringify({source,baseDraft:baseDraft.current,draft:lastDraft.current}));}catch{}},[source,result.status,result.text,cacheKey]);
 function edit(side:'zh'|'en',text:string){setSource({side,text});publish(side==='en'?text:'');onPendingChange(side==='zh'&&!!text.trim());}
 return <div className="reply-composer">
  <p role="status" className={'sync-state '+(result.status==='error'?'is-error':result.status==='done'?'is-success':'is-pending')}><Icon name={result.status==='error'?'warning':result.status==='done'?'check':'language'}/>{!source.text.trim()?'可输入中文或英文，也可生成参考回复。':composing?'正在输入…':result.status==='done'?'中英文已同步，请核对后复制。':result.status==='error'?'翻译失败：'+result.error:'正在同步'+(source.side==='zh'?'英文':'中文')+'回复…'}</p>
  <div className="reply-fields"><label><span className="editor-label">中文回复<small aria-hidden="true">客服核对</small></span><textarea aria-label="中文回复" rows={4} value={chinese} placeholder="输入中文回复，英文将自动同步"
   onCompositionStart={()=>setComposing(true)} onCompositionEnd={()=>setComposing(false)}
   onChange={e=>edit('zh',e.target.value)}/></label>
  <label><span className="editor-label">英文回复<small aria-hidden="true">顾客回复</small></span><textarea aria-label="英文回复" rows={4} value={english} placeholder="输入英文回复，中文将自动同步" onChange={e=>edit(/[\u3400-\u9fff]/.test(e.target.value)?'zh':'en',e.target.value)}/></label></div>
  <p className="editor-help">中文需先译成英文再复制；直接编辑英文可复制，中文译文失败时请自行核对英文。</p>
 </div>;
}
