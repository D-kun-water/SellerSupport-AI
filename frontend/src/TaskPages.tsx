import React,{Children,isValidElement} from 'react';

function sections(children:React.ReactNode):React.ReactNode[]{
 return Children.toArray(children).flatMap(child=>{
  if(!isValidElement<{children?:React.ReactNode;className?:string}>(child))return [];
  if(child.type===React.Fragment||child.props.className==='improvement-grid')return sections(child.props.children);
  return [child];
 });
}
function words(node:React.ReactNode):string{
 if(typeof node==='string'||typeof node==='number')return String(node);
 if(isValidElement<{children?:React.ReactNode}>(node))return words(node.props.children);
 return Children.toArray(node).map(words).join('');
}
function title(node:React.ReactNode):string{
 if(!isValidElement<{children?:React.ReactNode}>(node))return '';
 if(node.type==='h2'||node.type==='summary')return words(node.props.children);
 for(const child of Children.toArray(node.props.children)){const value=title(child);if(value)return value;}
 return '';
}
const shortNames:Record<string,string>={'记录客户反馈':'记录反馈','从已接受反馈建立改进任务':'创建任务','候选归集与原始证据':'整理反馈','专题审核':'审核问题','原因分析与核验':'确认原因','分组操作与撤回':'调整分组','改进任务列表':'选择任务','任务处理与交付':'处理与交付','观察窗口与人工结论':'记录效果','观察记录':'查看结果','审核案例与测试回答':'测试回答','回答测试记录':'测试记录','问题回答 → 审核答案 → 重新测试':'记录问题回答'};
export function TaskPages({children,index,onStep}:{children:React.ReactNode;index:number;onStep:(step:number)=>void}){
 const parts=sections(children);
 if(parts.length<=1)return <div className="task-pages">{parts.map((part,i)=><div key={i}>{part}</div>)}</div>;
 const active=Math.min(index,Math.max(parts.length-1,0));
 function move(next:number){onStep(next);window.scrollTo(0,0);}
 if(!parts.length)return null;
 return <div className="task-pages">
 {parts.length>1&&<div className="task-page-nav" aria-label="操作步骤">{parts.map((part,i)=>{const heading=title(part);return <button key={i} className={active===i?'active':''} onClick={()=>move(i)}>{i+1}. {shortNames[heading]||heading||(i===0?'记录反馈':'反馈处理进度')}</button>;})}</div>}
 {parts.map((part,i)=><div key={i} hidden={active!==i}>{part}</div>)}
 {parts.length>1&&<div className="flow-bottom"><button disabled={active===0} onClick={()=>move(active-1)}>← {shortNames[title(parts[active-1])]||title(parts[active-1])||'前一项'}</button><button disabled={active===parts.length-1} onClick={()=>move(active+1)}>{shortNames[title(parts[active+1])]||title(parts[active+1])||'后一项'} →</button></div>}
 </div>;
}
