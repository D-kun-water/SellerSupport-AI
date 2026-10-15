export type Rule={device_id:string;use_case:string;result:string;conditions:string[]};
export type KnowledgeProduct={id:string;sku?:string;name:string;category:string;region:string;version:string;source_id:string;review_status?:string;valid_from?:string;valid_to?:string|null;power_w:number|null;connector?:string|null;data_rate_mbps:number|null;video_support:boolean|null;rules?:Rule[];specifications?:Record<string,string>};
export type KnowledgePolicy={id:string;kind:string;region:string;channel?:string;version:string;source_id:string;review_status?:string;valid_from?:string;valid_to?:string|null;window_days?:number;original_sku_only?:boolean;quantity_limit?:string};
export type KnowledgeRow={key:string;type:'products'|'policies';title:string;subtitle:string;record:KnowledgeProduct|KnowledgePolicy};
export const categoryNames:Record<string,string>={charger:'充电器',cable:'线材',stand:'支架',case:'保护壳',hub:'扩展坞',powerbank:'移动电源',storage:'存储设备'};
export const useCaseNames:Record<string,string>={charging:'充电',data:'传输数据',video:'视频输出'};
export const ruleNames:Record<string,string>={compatible:'支持',conditional:'满足条件时支持',incompatible:'不支持'};
export type Validity='active'|'scheduled'|'expired'|'unpublished'|'unknown';
export const validityNames:Record<Validity,string>={active:'当前生效',scheduled:'尚未生效',expired:'已过期',unpublished:'尚未发布',unknown:'时间待核对'};

export function validity(record:Pick<KnowledgeRow['record'],'review_status'|'valid_from'|'valid_to'>,at=Date.now()):Validity{
 if(record.review_status!=='published')return 'unpublished';
 const start=Date.parse(record.valid_from||''),end=record.valid_to?Date.parse(record.valid_to):Infinity;
 if(!Number.isFinite(start)||Number.isNaN(end)||end<=start)return 'unknown';
 if(at<start)return 'scheduled';
 if(at>=end)return 'expired';
 return 'active';
}
export function knowledgeRows(products:KnowledgeProduct[],policies:KnowledgePolicy[]):KnowledgeRow[]{
 return [...products.map(record=>({key:'products:'+record.id,type:'products' as const,title:record.name,subtitle:(record.sku||record.id)+' · '+(categoryNames[record.category]||'商品资料'),record})),
  ...policies.map(record=>({key:'policies:'+record.id,type:'policies' as const,title:(record.kind==='returns'?'退货政策':record.kind==='reships'?'补发政策':'售后政策')+' · '+record.region,subtitle:record.kind==='returns'?(record.window_days==null?'退货窗口待核对':record.window_days+'天退货窗口'):record.kind==='reships'?'订单补发规则':'售后政策说明',record}))];
}
export function filterKnowledge(rows:KnowledgeRow[],query:string,type:string,region:string,currentOnly:boolean,at=Date.now(),category='all'){
 const terms=query.trim().toLocaleLowerCase().split(/\s+/).filter(Boolean);
 return rows.filter(row=>{
  if(type!=='all'&&row.type!==type)return false;
  if(region!=='all'&&row.record.region!==region)return false;
  if(currentOnly&&validity(row.record,at)!=='active')return false;
  const product=row.type==='products'?row.record as KnowledgeProduct:null;
  if(category!=='all'&&product?.category!==category)return false;
  const policy=row.type==='policies'?row.record as KnowledgePolicy:null;
  const text=[row.title,row.subtitle,row.record.id,row.record.region,product?.connector,
   product?.power_w==null?'':product.power_w+'W',product?.data_rate_mbps==null?'':product.data_rate_mbps+'Mbps',
   ...(product?.rules||[]).flatMap(r=>[r.device_id,useCaseNames[r.use_case]||r.use_case,ruleNames[r.result]||r.result,...r.conditions]),
   ...Object.entries(product?.specifications||{}).flat(),
   policy?.channel,policy?.window_days==null?'':policy.window_days+'天'].join(' ').toLocaleLowerCase();
  return terms.every(term=>text.includes(term));
 });
}
