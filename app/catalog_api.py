"""local catalog publication: source-bound review, regression, optimistic publish."""
from copy import deepcopy
from datetime import timedelta
from typing import Literal
from pydantic import Field, model_validator
from fastapi import Depends
from .merchant import Input, digest, configured
from .domain import DomainError, timestamp, now
from .store import uid, encode
from . import services as svc

class Rule(Input):
    device_id:str=Field(pattern=r'^[A-Z0-9_-]{3,100}$')
    use_case:Literal['charging','data','video']
    result:Literal['compatible','conditional','incompatible']
    conditions:list[str]=Field(min_length=1,max_length=10)
    conditions_en:list[str]|None=Field(default=None,min_length=1,max_length=10)
    @model_validator(mode='after')
    def bilingual(self):
        if self.conditions_en is not None and (len(self.conditions_en)!=len(self.conditions) or any(not x.strip() for x in self.conditions_en)):
            raise ValueError('中英文适配条件必须逐条对应且不能为空')
        return self

class Product(Input):
    id:str=Field(pattern=r'^[A-Z0-9_-]{3,100}$')
    sku:str=Field(pattern=r'^[A-Z0-9_-]{3,100}$')
    shop_id:str
    source_type:Literal['local']
    category:Literal['charger','cable','stand','case','hub','powerbank','storage']
    name:str=Field(min_length=1,max_length=300)
    region:Literal['US','EU']
    version:str=Field(min_length=1,max_length=100)
    valid_from:str
    valid_to:str|None=None
    power_w:int|None=Field(default=None,ge=0,le=500)
    connector:str|None=Field(default=None,max_length=100)
    data_rate_mbps:int|None=Field(default=None,ge=0,le=100000)
    video_support:bool|None=None
    specifications:dict[str,str]=Field(default_factory=dict,max_length=15)
    rules:list[Rule]=Field(max_length=30)
    @model_validator(mode='after')
    def check(self):
        if self.id!=self.sku:raise ValueError('商品id须与SKU一致')
        if len({(r.device_id,r.use_case) for r in self.rules})!=len(self.rules):raise ValueError('重复设备用途规则')
        dates(self);return self

class Policy(Input):
    id:str=Field(pattern=r'^[A-Za-z0-9_-]{3,100}$')
    shop_id:str
    source_type:Literal['local']
    region:Literal['US','EU']
    channel:Literal['direct']='direct'
    kind:Literal['returns','reships']
    version:str=Field(min_length=1,max_length=100)
    valid_from:str
    valid_to:str|None=None
    window_days:int|None=Field(default=None,ge=1,le=365)
    timezone:Literal['UTC']='UTC'
    original_sku_only:bool=True
    quantity_limit:Literal['original_order_quantity']='original_order_quantity'
    @model_validator(mode='after')
    def check(self):
        dates(self)
        if self.kind=='returns' and self.window_days is None:raise ValueError('退货政策需要时间窗口')
        if self.kind=='reships' and (not self.original_sku_only or self.window_days is not None):raise ValueError('补发仅限原SKU原订单数量')
        return self

def dates(obj):
    start=timestamp(obj.valid_from)
    if obj.valid_to and timestamp(obj.valid_to)<=start:raise ValueError('截止时间须晚于生效时间')

class Draft(Input):
    kind:Literal['products','policies']
    object:dict
    source_facts:dict
    source_text:str=Field(min_length=20,max_length=20000)
    expected_version:str|None=None
    reason:str=Field(min_length=3,max_length=1000)

class Action(Input):
    version:int=Field(ge=1)
    reason:str=Field(min_length=3,max_length=1000)

def normalize(kind,obj):return (Product if kind=='products' else Policy).model_validate(obj).model_dump()

def validate(store,db,row):
    obj=row['object'];shop=configured(store,db)['shop_id']
    if obj['shop_id']!=shop:raise DomainError('VALIDATION_ERROR','商品或政策商家不匹配')
    old=next((x for x in store.all(db,row['kind']) if x['id']==obj['id']),None)
    if (old.get('version') if old else None)!=row['expected_version']:raise DomainError('VERSION_CONFLICT','发布对象版本已变化',409)
    if old and obj['version']==old['version']:raise DomainError('VALIDATION_ERROR','新版本必须改变版本号')
    if row['kind']=='policies':
        for p in store.all(db,'policies'):
            if p['id']==obj['id'] or p['shop_id']!=shop or p['review_status']!='published' or any(p.get(k)!=obj[k] for k in ('kind','region','channel')):continue
            if (not p.get('valid_to') or timestamp(obj['valid_from'])<timestamp(p['valid_to'])) and (not obj.get('valid_to') or timestamp(p['valid_from'])<timestamp(obj['valid_to'])):raise DomainError('POLICY_CONFLICT','同区域渠道政策生效区间重叠',409)

def fingerprint(store,db):
    from pathlib import Path
    code=digest(''.join((Path(__file__).parent/n).read_text(encoding='utf-8-sig') for n in ('catalog_api.py','services.py','assist.py')))
    return digest(encode(dict(code=code,data={t:store.all(db,t) for t in ('products','policies','sources','orders','merchant_settings')})))

def regression(store,db,row):
    """Actually execute current filters and return-window code in a rollback savepoint."""
    obj=dict(row['object'],review_status='published',source_id=row['source_id'])
    src=dict(id=row['source_id'],version=obj['version'],text=row['source_text']+'\nStructured facts: '+encode(row['source_facts']),checksum=digest(row['source_text']+'\nStructured facts: '+encode(row['source_facts'])),locator='local-catalog:'+row['id'],source_type='local',status='published')
    checks=[]
    def check(name,actual,expected):checks.append(dict(name=name,actual=actual,expected=expected,passed=actual==expected))
    db.execute('SAVEPOINT catalog_regression')
    try:
        store.put(db,row['kind'],obj['id'],obj);store.put(db,'sources',src['id'],src)
        start=timestamp(obj['valid_from']);end=timestamp(obj['valid_to']) if obj.get('valid_to') else None
        selected=lambda region,at: any(x['id']==obj['id'] for x in svc.active(store,db,row['kind'],region,at.isoformat()))
        check('before_effective',selected(obj['region'],start-timedelta(microseconds=1)),False)
        check('at_effective',selected(obj['region'],start),True)
        check('other_region',selected('EU' if obj['region']=='US' else 'US',start),False)
        check('source_checksum',svc.evidence(store,db,src['id'])['checksum'],src['checksum'])
        if end:check('at_expiry',selected(obj['region'],end),False)
        if row['kind']=='policies' and obj['kind']=='returns':
            n=obj['window_days'];anchor=start+timedelta(seconds=1)
            for name,delta,expected in [('inside',timedelta(days=n)-timedelta(seconds=1),'eligible'),('boundary',timedelta(days=n),'eligible'),('outside',timedelta(days=n,seconds=1),'outside_window'),('future_delivery',timedelta(seconds=-1),'outside_window')]:
                result=svc.policy_result(store,db,{'delivered_at':(anchor-delta).isoformat()},obj['region'],anchor.isoformat())
                check(name,result['result'],expected)
            check('missing_delivery',svc.policy_result(store,db,{},obj['region'],start.isoformat())['result'],'missing_delivery')
        if row['kind']=='products':
            from .assist import assist
            from .merchant import actors
            actor=next(iter(actors(store,db).values()))
            for i,r in enumerate(obj['rules']):
                ticket=svc.create_ticket(store,db,actor,dict(source_type='local',message='Please confirm compatibility.',channel='direct',region=obj['region'],sku=obj['sku'],device_id=r['device_id'],use_case=r['use_case']))
                result=assist(store,db,actor,ticket['id'],{'version':ticket['version'],'as_of':start.isoformat()},persist=False)
                # Rule regression also verifies citations bind the new published source.
                check('rule_'+str(i)+'_citation',any(e['id']==src['id'] for e in result['evidence_refs']),True)
                check('rule_'+str(i)+'_result',result['status']=='draft' and ('is '+r['result']+'.') in result['reply_draft'] and all(c in result['reply_draft'] for c in (r.get('conditions_en') or r['conditions'])),True)
        return checks,src,obj
    finally:
        db.execute('ROLLBACK TO catalog_regression');db.execute('RELEASE catalog_regression')

def install(app,store,identity,envelope):
    @app.get('/api/catalog-releases')
    def inventory(actor=Depends(identity)):
        svc.role(actor,{'supervisor'})
        with store.transaction() as db:return envelope(store.all(db,'catalog_releases'))

    @app.post('/api/catalog-releases')
    def draft(payload:Draft,actor=Depends(identity)):
        svc.role(actor,{'supervisor'})
        try:obj=normalize(payload.kind,payload.object);facts=normalize(payload.kind,payload.source_facts)
        except ValueError as e:raise DomainError('VALIDATION_ERROR',str(e))
        if facts!=obj:raise DomainError('NO_EVIDENCE','来源结构化事实与发布对象不一致')
        with store.transaction() as db:
            row=dict(id=uid('CAT'),kind=payload.kind,object=obj,source_facts=facts,source_text=payload.source_text,source_id=uid('SRC'),expected_version=payload.expected_version,version=1,state='draft',review_mode='manual_review',reason=payload.reason,source_type='local',created_at=now())
            validate(store,db,row);store.put(db,'catalog_releases',row['id'],row);store.audit(db,actor,'catalog_drafted',row['id']);return envelope(row)

    @app.post('/api/catalog-releases/{key}/{action}')
    def act(key:str,action:Literal['review','regress','publish'],payload:Action,actor=Depends(identity)):
        svc.role(actor,{'supervisor'})
        with store.transaction() as db:
            row=store.get(db,'catalog_releases',key);svc.version(row,payload.version);validate(store,db,row)
            if action=='review':
                if row['state']!='draft':raise DomainError('INVALID_STATE','只有草稿可审核',409)
                row.update(state='reviewed',review_reason=payload.reason,reviewed_by=actor['id'])
            elif action=='regress':
                if row['state'] not in {'reviewed','regressed'}:raise DomainError('INVALID_STATE','先审核来源',409)
                checks,_,_=regression(store,db,row)
                row.update(state='regressed',checks=checks,passed=all(x['passed'] for x in checks),regression_fingerprint=fingerprint(store,db),validation_type='generated_local_regression')
            else:
                if row['state']!='regressed' or not row['passed']:raise DomainError('INVALID_STATE','回归未通过',409)
                if row['regression_fingerprint']!=fingerprint(store,db):raise DomainError('VERSION_CONFLICT','回归后资料发生变化，请重跑',409)
                checks,src,obj=regression(store,db,row)
                if not all(x['passed'] for x in checks):raise DomainError('INVALID_STATE','发布复核失败',409)
                src.update(captured_at=now(),origin='商家提交资料，经主管核对发布',review_status='manually_approved')
                old=next((x for x in store.all(db,row['kind']) if x['id']==obj['id']),None)
                if old and timestamp(obj['valid_from'])>timestamp(old['valid_from']):
                    historical=deepcopy(old);historical['id']=uid('HIST')
                    if not historical.get('valid_to') or timestamp(historical['valid_to'])>timestamp(obj['valid_from']):historical['valid_to']=obj['valid_from']
                    store.put(db,row['kind'],historical['id'],historical)
                store.put(db,'sources',src['id'],src);store.put(db,row['kind'],obj['id'],obj)
                row.update(state='published',published_at=now())
            row['version']+=1;store.put(db,'catalog_releases',key,row);store.audit(db,actor,'catalog_'+action,key,{'reason':payload.reason,'review_mode':'manual_review'});return envelope(row)
