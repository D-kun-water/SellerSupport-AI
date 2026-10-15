import test from 'node:test';
import assert from 'node:assert/strict';
import {knowledgeRows,filterKnowledge,validity} from '../src/knowledgeView.ts';

const start='2026-10-13T00:00:00Z',end='2026-10-14T00:00:00Z';
const product={id:'CABLE-1',name:'60W USB-C 线材',category:'cable',region:'US',source_id:'SOURCE-1',version:'1',review_status:'published',valid_from:start,valid_to:end,power_w:60,video_support:false,data_rate_mbps:480,rules:[{device_id:'DEVICE-1',use_case:'charging',result:'conditional',conditions:['Use a USB-C PD adapter.']}]};
const policy={id:'RETURN-EU',kind:'returns',region:'EU',source_id:'SOURCE-2',version:'1',review_status:'published',valid_from:start,window_days:14};
const rows=knowledgeRows([product],[policy]);
const at=Date.parse('2026-10-13T12:00:00Z');

test('lookup combines product facts, device conditions and regional policies',()=>{
 assert.deepEqual(filterKnowledge(rows,'US 60W DEVICE-1','all','all',true,at).map(r=>r.key),['products:CABLE-1']);
 assert.equal(filterKnowledge(rows,'14天','policies','EU',true,at)[0].title,'退货政策 · EU');
 assert.equal(filterKnowledge(rows,'DEVICE-1','all','EU',true,at).length,0);
 assert.equal(filterKnowledge(rows,'不存在的型号','all','all',true,at).length,0);
});
test('effective-time boundary includes start and excludes end',()=>{
 assert.equal(validity(product,Date.parse(start)-1),'scheduled');
 assert.equal(validity(product,Date.parse(start)),'active');
 assert.equal(validity(product,Date.parse(end)),'expired');
 assert.equal(filterKnowledge(rows,'','products','all',true,Date.parse(end)).length,0);
 assert.equal(filterKnowledge(rows,'','products','all',false,Date.parse(end)).length,1);
});
test('unpublished and malformed time records cannot appear as currently effective',()=>{
 assert.equal(validity({...product,review_status:'draft'},at),'unpublished');
 assert.equal(validity({...product,valid_from:'bad'},at),'unknown');
 assert.equal(validity({...product,valid_to:'bad'},at),'unknown');
 assert.equal(validity({...product,valid_to:start},at),'unknown');
});
test('new product categories and Chinese specifications are searchable independently',()=>{
 const storage={...product,id:'STORAGE-1',name:'便携固态硬盘',category:'storage',specifications:{标称容量:'1TB',文件系统:'出厂为 exFAT'}};
 const items=knowledgeRows([product,storage],[policy]);
 assert.deepEqual(filterKnowledge(items,'1TB exFAT','products','all',true,at,'storage').map(r=>r.key),['products:STORAGE-1']);
 assert.equal(filterKnowledge(items,'1TB','products','all',true,at,'cable').length,0);
 assert.equal(filterKnowledge(items,'','all','all',true,at,'storage').length,1);
});

