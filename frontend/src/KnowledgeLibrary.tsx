import React,{useEffect,useMemo,useState} from 'react';
import {credential} from './session';
import {useRouteValue,setRouteParams} from './navigation';
import {Icon} from './Icon';
import {ChineseMeaning} from './Translation';
import {categoryNames,filterKnowledge,knowledgeRows,KnowledgePolicy,KnowledgeProduct,ruleNames,useCaseNames,validity,validityNames} from './knowledgeView';
import './knowledge.css';

type Source={id:string;version:string;locator:string;text:string;status?:string};
type Props={role:string;products:KnowledgeProduct[];policies:KnowledgePolicy[];busy:boolean;onRefresh:()=>void};
const pageSize=8;
function dateLabel(value?:string|null,missing='时间待核对'){if(!value)return missing;const date=new Date(value);return Number.isNaN(date.getTime())?'时间待核对':date.toLocaleString('zh-CN',{hour12:false});}

export function KnowledgeLibrary({role,products,policies,busy,onRefresh}:Props){
 const query=useRouteValue('q'),type=useRouteValue('kind','all'),region=useRouteValue('region','all'),category=useRouteValue('category','all'),currentOnly=useRouteValue('current','true')!=='false',page=Number(useRouteValue('page','0'))||0,selectedKey=useRouteValue('selected');
 const setQuery=(v:string)=>setRouteParams({q:v}),setType=(v:string)=>setRouteParams({kind:v}),setRegion=(v:string)=>setRouteParams({region:v}),setCategory=(v:string)=>setRouteParams({category:v}),setCurrentOnly=(v:boolean)=>setRouteParams({current:String(v)}),setPage=(v:number)=>setRouteParams({page:v?String(v):''}),setSelectedKey=(v:string)=>setRouteParams({selected:v});
 const [clock,setClock]=useState(Date.now);
 const [sourceState,setSourceState]=useState<{key:string;source?:Source;product?:KnowledgeProduct;error?:string}>({key:''});
 const rows=useMemo(()=>knowledgeRows(products,policies),[products,policies]);
 const filtered=useMemo(()=>filterKnowledge(rows,query,type,region,currentOnly,clock,category),[rows,query,type,region,currentOnly,clock,category]);
 const pages=Math.max(1,Math.ceil(filtered.length/pageSize)),currentPage=Math.min(page,pages-1);
 const visible=filtered.slice(currentPage*pageSize,(currentPage+1)*pageSize);
 const selected=filtered.find(row=>row.key===selectedKey)||visible[0];
 const sourceKey=JSON.stringify([role,selected?.key,selected?.record.version,selected?.record.source_id]);
 const loaded=sourceState.key===sourceKey?sourceState:null;
 const product=selected?.type==='products'?(loaded?.product||selected.record as KnowledgeProduct):null;
 const policy=selected?.type==='policies'?selected.record as KnowledgePolicy:null;
 const record=product||policy;
 useEffect(()=>{const timer=window.setInterval(()=>setClock(Date.now()),60000);return()=>clearInterval(timer);},[]);
 useEffect(()=>{
  if(!selected)return;
  const controller=new AbortController();
  setSourceState({key:sourceKey});
  const path=selected.type==='products'?'/api/products/'+encodeURIComponent(selected.record.id):'/api/sources/'+encodeURIComponent(selected.record.source_id);
  void fetch(path,{headers:{Authorization:`Bearer ${credential(role)}`},signal:controller.signal}).then(async response=>{
   const json=await response.json();if(!response.ok)throw Error(json.errors?.[0]?.message||'资料来源暂不可用');
   const source:Source=selected.type==='products'?json.data.evidence:json.data;
   if(source.status&&source.status!=='published')throw Error('来源已停用，不能用于当前回复。');
   if(!controller.signal.aborted)setSourceState({key:sourceKey,source,product:json.data.product});
  }).catch(error=>{if(!controller.signal.aborted)setSourceState({key:sourceKey,error:error instanceof Error?error.message:'资料来源暂不可用'});});
  return()=>controller.abort();
 },[sourceKey]);
 function resetSelection(){setPage(0);setSelectedKey('');}
 function turnPage(next:number){setPage(next);setSelectedKey(filtered[next*pageSize]?.key||'');}
 return <div className="knowledge-library">
  <div className="knowledge-intro"><p>查找商品参数、设备适配条件与售后政策，核对后用于客户回复。</p><button disabled={busy} onClick={()=>{setClock(Date.now());onRefresh();}}>刷新知识</button></div>
  <section className="card knowledge-search"><label className="knowledge-search-input"><Icon name="search"/><input aria-label="搜索知识库" value={query} onChange={e=>{setQuery(e.target.value);resetSelection();}} placeholder="搜索商品名、SKU、设备型号或退货政策"/></label><label className="knowledge-region">商品类别<select value={category} onChange={e=>{setCategory(e.target.value);if(e.target.value!=='all')setType('products');resetSelection();}}><option value="all">全部类别</option>{Object.entries(categoryNames).map(([value,label])=><option key={value} value={value}>{label}</option>)}</select></label><label className="knowledge-region">适用市场<select value={region} onChange={e=>{setRegion(e.target.value);resetSelection();}}><option value="all">全部市场</option><option>US</option><option>EU</option></select></label></section>
  <div className="knowledge-filter-row"><div className="knowledge-tabs" aria-label="知识分类">{[['all','全部知识'],['products','商品资料'],['policies','售后政策']].map(([value,label])=><button key={value} aria-pressed={type===value} className={type===value?'active':''} onClick={()=>{setType(value);setCategory('all');resetSelection();}}>{label}<span>{rows.filter(row=>(value==='all'||row.type===value)&&(region==='all'||row.record.region===region)&&(!currentOnly||validity(row.record,clock)==='active')).length}</span></button>)}</div><label className="knowledge-current"><input type="checkbox" checked={currentOnly} onChange={e=>{setCurrentOnly(e.target.checked);resetSelection();}}/>只看当前生效</label></div>
  {busy&&!rows.length?<section className="card" role="status">正在读取知识库…</section>:<div className="knowledge-workspace">
   <section className="card knowledge-directory" aria-label="知识目录"><div className="knowledge-directory-heading"><strong>查询结果</strong><span>{filtered.length}条</span></div>
    {!filtered.length&&<div className="knowledge-empty"><Icon name="search"/><p>未找到匹配资料</p><small>调整关键词、市场或生效筛选后重试。</small></div>}
    {visible.map(row=><button key={row.key} className={'knowledge-row'+(selected?.key===row.key?' selected':'')} aria-pressed={selected?.key===row.key} onClick={()=>setSelectedKey(row.key)}><span className="knowledge-row-symbol"><Icon name={row.type==='products'?'box':'shield'}/></span><span className="knowledge-row-text"><strong>{row.title}</strong><small>{row.subtitle}</small><span>{row.record.region} · {validityNames[validity(row.record,clock)]}</span></span><Icon name="arrow-right"/></button>)}
    {pages>1&&<div className="knowledge-pagination"><button aria-label="上一页知识" disabled={!currentPage} onClick={()=>turnPage(currentPage-1)}><Icon name="arrow-left"/></button><span>{currentPage+1} / {pages}</span><button aria-label="下一页知识" disabled={currentPage>=pages-1} onClick={()=>turnPage(currentPage+1)}><Icon name="arrow-right"/></button></div>}
   </section>
   <section className="card knowledge-detail" aria-label="知识详情">{selected&&record?<>
    <div className="section-heading"><div><span className="section-kicker">{product?'商品资料':'售后政策'}</span><h2>{product?.name||selected.title}</h2><p className="knowledge-detail-meta">{product?.sku||record.id} · {record.region} · 版本 {record.version}</p></div><span className={'badge knowledge-validity '+validity(record,clock)}>{validityNames[validity(record,clock)]}</span></div>
    {loaded?.error&&<p className="error" role="alert">{loaded.error} 请刷新核对，当前来源不能作为回复依据。</p>}
    {validity(record,clock)!=='active'&&<p className="flow-hint">此版本当前不适用。可查阅历史内容，回复客户时应核对当前生效版本。</p>}
    {product&&<><dl className="knowledge-facts"><div><dt>商品类别</dt><dd>{categoryNames[product.category]||product.category}</dd></div>{(product.power_w!=null||['charger','cable'].includes(product.category))&&<div><dt>额定功率</dt><dd>{product.power_w==null?'尚未确认':product.power_w+' W'}</dd></div>}{product.connector&&<div><dt>接口</dt><dd>{product.connector}</dd></div>}{(product.category==='cable'||product.data_rate_mbps!=null)&&<div><dt>数据传输</dt><dd>{product.data_rate_mbps==null?'尚未确认':product.data_rate_mbps+' Mbps'}</dd></div>}{(product.category==='cable'||product.video_support!=null)&&<div><dt>视频输出</dt><dd>{product.video_support==null?'尚未确认':product.video_support?'支持，须核对适用条件':'不支持'}</dd></div>}</dl>
     <div className="knowledge-section"><h3>设备与用途条件</h3>{product.rules?.length?product.rules.map((rule,index)=><article className="knowledge-rule" key={index}><div className="knowledge-rule-heading"><strong>{rule.device_id} · {useCaseNames[rule.use_case]||rule.use_case}</strong><span className={'badge rule-'+rule.result}>{ruleNames[rule.result]||rule.result}</span></div>{rule.conditions.length>0&&<><p>{rule.conditions.join(' ')}</p>{loaded?.source&&!/[\u4e00-\u9fff]/.test(rule.conditions.join(' '))&&<ChineseMeaning context={{role,ticketId:'',contextVersion:1}} text={rule.conditions.join(' ')}/>}</>}</article>):<p className="muted">当前资料未列出设备适配规则，请按来源核对具体用途。</p>}</div>
    </>}
    {product&&Object.keys(product.specifications||{}).length>0&&<div className="knowledge-section"><h3>规格与使用限制</h3><dl className="knowledge-extra-specs">{Object.entries(product.specifications||{}).map(([key,value])=><div key={key}><dt>{key}</dt><dd>{value}</dd></div>)}</dl></div>}
    {policy&&<><dl className="knowledge-facts"><div><dt>政策类型</dt><dd>{policy.kind==='returns'?'退货':policy.kind==='reships'?'补发':policy.kind}</dd></div><div><dt>适用渠道</dt><dd>{policy.channel==='direct'?'店铺直销':policy.channel||'尚未确认'}</dd></div>{policy.kind==='returns'&&<div><dt>退货时间窗口</dt><dd>{policy.window_days==null?'尚未确认':policy.window_days+'天，自签收起算'}</dd></div>}</dl>{policy.kind==='reships'&&<div className="knowledge-section"><h3>补发约束</h3><p>按原订单商品及已批准数量核对，提交申请后由主管审核。</p></div>}{policy.kind==='returns'&&<p className="muted">核对签收时间与凭据；时间窗口判断和退款审批分别处理。</p>}</>}
    <div className="knowledge-section knowledge-effective"><h3>适用时间与来源</h3><dl><dt>生效时间</dt><dd>{dateLabel(record.valid_from)}</dd><dt>截止时间</dt><dd>{dateLabel(record.valid_to,'无截止时间')}</dd></dl>
     {loaded?.source?<details><summary>查看来源原文与版本</summary><p className="muted">来源版本：{loaded.source.version}</p><pre>{loaded.source.text}</pre><small>{loaded.source.locator}</small></details>:!loaded?.error&&<p className="muted" role="status">正在核对来源…</p>}
    </div>
   </>:<div className="knowledge-empty"><Icon name="box"/><p>选择知识条目查看详情</p></div>}</section>
  </div>}
 </div>;
}
