import {useSyncExternalStore} from 'react';
export const routePaths:Record<string,string>={'主管待办':'/manager','客服工作台':'/service/list','商品问题':'/feedback','改进任务':'/tasks','动作审批':'/after-sales','人工交接':'/handoffs','附件与对话':'/attachments','商品与规则':'/knowledge','质量与效果':'/quality','数据导入':'/imports','商家验证':'/validation'};
export function readRoute(){const raw=location.hash.slice(1)||'/service/list';const [path,query='']=raw.split('?');return {path,params:new URLSearchParams(query)};}
export function tabForRoute(role:string){const {path}=readRoute();if(path.startsWith('/service/'))return role==='agent'?'客服工作台':'主管待办';return Object.entries(routePaths).find(([,p])=>p===path)?.[0]||(role==='supervisor'?'主管待办':'客服工作台');}
export function navigateRoute(tab:string,id?:string){location.hash=routePaths[tab]+(id?'?ticket='+encodeURIComponent(id):'');window.scrollTo(0,0);}
export function setRouteParams(values:Record<string,string>){const {path,params}=readRoute();for(const [k,v] of Object.entries(values)){if(v)params.set(k,v);else params.delete(k);}location.hash=path+(params.size?'?'+params:'');}
export function useRouteValue(key:string,fallback=''){const hash=useSyncExternalStore(cb=>{window.addEventListener('hashchange',cb);return()=>window.removeEventListener('hashchange',cb);},()=>location.hash);const query=hash.split('?')[1]||'';return new URLSearchParams(query).get(key)||fallback;}
