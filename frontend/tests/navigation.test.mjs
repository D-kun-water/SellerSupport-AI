import test from 'node:test';
import assert from 'node:assert/strict';
import {tabForRoute,readRoute,navigateRoute,setRouteParams} from '../src/navigation.ts';
function page(hash){let current=hash;globalThis.location={get hash(){return current;},set hash(value){current=value.startsWith('#')?value:'#'+value;}};globalThis.window={scrollTo(){}};}
test('refresh resolves manager and customer modules from their own routes',()=>{page('#/after-sales?ticket=TKT-1');assert.equal(tabForRoute('supervisor'),'动作审批');assert.equal(readRoute().params.get('ticket'),'TKT-1');page('#/tasks?task=TASK-2&step=work');assert.equal(tabForRoute('agent'),'改进任务');assert.equal(readRoute().params.get('task'),'TASK-2');});
test('filters retain the selected ticket and use URL encoding',()=>{page('#/after-sales?ticket=TKT-1');setRouteParams({filter:'all',q:'1美元 & refund'});assert.equal(readRoute().params.get('ticket'),'TKT-1');assert.equal(readRoute().params.get('q'),'1美元 & refund');setRouteParams({q:''});assert.equal(readRoute().params.has('q'),false);});
test('menu navigation clears the old module context',()=>{page('#/tasks?task=TASK-2&step=work');navigateRoute('人工交接','TKT-3');assert.equal(location.hash,'#/handoffs?ticket=TKT-3');navigateRoute('客服工作台');assert.equal(location.hash,'#/service/list');});
