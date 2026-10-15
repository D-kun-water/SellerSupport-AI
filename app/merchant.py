"""Single local merchant, database-backed identities and atomic order imports."""
import csv
import hashlib
import io
import json
import re
import secrets
from pydantic import BaseModel, ConfigDict, Field, model_validator
from typing import Literal
from fastapi import Depends
from fastapi.responses import FileResponse, Response
from .store import uid, encode, ROOT
from .domain import DomainError, now, timestamp, money
from . import services as svc

class Input(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)

class Member(Input):
    id: str = Field(pattern=r'^[A-Za-z0-9_-]{3,80}$')
    label: str = Field(min_length=1,max_length=100)
    role: Literal['agent','supervisor']
    token: str = Field(min_length=12,max_length=200)

class Initialize(Input):
    shop_id: str = Field(pattern=r'^[A-Z0-9_-]{3,80}$')
    name: str = Field(min_length=1,max_length=100)
    source_type: Literal['local']
    members: list[Member] = Field(min_length=2,max_length=20)
    @model_validator(mode='after')
    def check(self):
        if self.shop_id=='TEST-SHOP':raise ValueError('新商家不能使用保留的测试保留标识')
        if not any(m.role=='supervisor' for m in self.members): raise ValueError('至少一名主管')
        if len({m.id for m in self.members})!=len(self.members) or len({m.token for m in self.members})!=len(self.members): raise ValueError('人员和凭据必须唯一')
        return self

class Grant(Input):
    member_id: str
    order_id: str
    version: int = Field(ge=0)
    enabled: bool
    reason: str = Field(min_length=3,max_length=1000)

class Line(Input):
    id: str = Field(min_length=1,max_length=100)
    sku: str = Field(min_length=1,max_length=100)
    quantity: int = Field(strict=True,ge=1,le=1000)

class Order(Input):
    id: str = Field(pattern=r'^[A-Z0-9_-]{3,100}$')
    shop_id: str
    source_type: Literal['local']
    customer_ref: str = Field(min_length=1,max_length=200)
    region: Literal['US','EU']
    channel: Literal['direct'] = 'direct'
    currency: Literal['USD'] = 'USD'
    paid: str
    status: Literal['ordered','in_transit','delivered','cancelled']
    ordered_at: str
    shipped_at: str|None = None
    delivered_at: str|None = None
    observed_at: str
    last_tracking_at: str|None = None
    max_age_seconds: int = Field(strict=True,ge=1,le=604800)
    carrier: str = Field(min_length=1,max_length=200)
    tracking_ref: str = Field(min_length=1,max_length=200)
    lines: list[Line] = Field(min_length=1,max_length=100)
    @model_validator(mode='after')
    def check(self):
        self.paid=money(self.paid)
        if len({x.id for x in self.lines})!=len(self.lines):raise ValueError('订单行重复')
        times=[timestamp(x) for x in (self.ordered_at,self.shipped_at,self.delivered_at) if x]
        if times!=sorted(times) or timestamp(self.observed_at)<max(times):raise ValueError('订单时间顺序错误')
        if self.last_tracking_at and (timestamp(self.last_tracking_at)<times[0] or timestamp(self.last_tracking_at)>timestamp(self.observed_at)):raise ValueError('物流快照时间错误')
        if self.status=='delivered' and not self.delivered_at:raise ValueError('签收订单需要签收时间')
        if self.status=='in_transit' and (not self.shipped_at or self.delivered_at):raise ValueError('运输中订单需要发货时间且不能已签收')
        if self.delivered_at and self.status!='delivered':raise ValueError('状态与签收时间不一致')
        return self

class OrderFile(Input):
    format: Literal['json','csv']
    content: str = Field(min_length=2,max_length=2000000)

class Commit(Input):
    version: int = Field(ge=1)


def digest(value):return hashlib.sha256(value.encode()).hexdigest()

def actors(store,db):
    grants=store.all(db,'order_grants')
    return {m['id']:dict(id=m['id'],role=m['role'],label=({'local-agent':'客服','local-supervisor':'主管'}.get(m['id'],m['label']) if m['label']==m['id'] else m['label']),orders=[g['order_id'] for g in grants if g['member_id']==m['id'] and g['enabled']]) for m in store.all(db,'members') if m.get('enabled',True)}

def team(store):
    with store.transaction() as db:return actors(store,db)

def seed_members(store,identities,empty=False):
    with store.transaction() as db:
        if store.all(db,'members'):return
        for token,m in identities:
            store.put(db,'members',m['id'],dict(id=m['id'],label=m['id'],role=m['role'],token_hash=digest(token),enabled=True))
            if not empty:
                for o in m['orders']:
                    key=m['id']+':'+o
                    store.put(db,'order_grants',key,dict(id=key,member_id=m['id'],order_id=o,version=1,enabled=True))
        if empty:store.put(db,'merchant_settings','merchant',dict(id='merchant',initialized=False,shop_id=None,source_type='local',version=0))
        if not empty:store.put(db,'merchant_settings','merchant',dict(id='merchant',initialized=True,shop_id='TEST-SHOP',name='回归测试商家',source_type='local',version=1))

def authenticate(store,token):
    with store.transaction() as db:
        current=actors(store,db)
        for m in store.all(db,'members'):
            if m['id'] in current and secrets.compare_digest(digest(token),m['token_hash']):return current[m['id']]
    raise DomainError('FORBIDDEN','需要有效本地凭据',403)

def retire_legacy_member(store):
    """Retire the old scope-test login without deleting historical authorship."""
    with store.transaction() as db:
        row=next((m for m in store.all(db,'members') if m['id']=='local-restricted'),None)
        if row and row.get('token_hash')==digest('local-restricted-local') and not row.get('retired_legacy_member'):
            row.update(enabled=False,retired_legacy_member=True,version=row.get('version',0)+1)
            store.put(db,'members',row['id'],row)

def configured(store,db):
    rows=store.all(db,'merchant_settings')
    if not rows or not rows[0].get('initialized'):raise DomainError('NOT_INITIALIZED','先初始化商家',409)
    return rows[0]

def parse_orders(payload):
    try:
        if payload.format=='json':raw=json.loads(payload.content)
        else:
            reader=csv.DictReader(io.StringIO(payload.content))
            if not reader.fieldnames or len(reader.fieldnames)!=len(set(reader.fieldnames)):raise ValueError('CSV表头重复或缺失')
            raw=[]
            for row in reader:
                if None in row:raise ValueError('CSV列数不匹配')
                row['lines']=json.loads(row['lines'])
                row['max_age_seconds']=int(row['max_age_seconds'])
                for key in ('shipped_at','delivered_at','last_tracking_at'):
                    if not row.get(key):row[key]=None
                raw.append(row)
        if not isinstance(raw,list) or not 1<=len(raw)<=1000:raise ValueError('必须为1至1000个订单')
        rows=[Order.model_validate(x).model_dump() for x in raw]
        if len({x['id'] for x in rows})!=len(rows):raise ValueError('批次订单重复')
        return rows
    except (ValueError,KeyError,TypeError,csv.Error) as error:
        raise DomainError('VALIDATION_ERROR',str(error))

def validate_orders(store,db,rows):
    shop=configured(store,db)['shop_id']
    products={p['sku']:p for p in store.all(db,'products') if p['shop_id']==shop and p['review_status']=='published'}
    existing={o['id']:o for o in store.all(db,'orders')}
    for row in rows:
        if row['shop_id']!=shop:raise DomainError('VALIDATION_ERROR','订单商家不匹配')
        for line in row['lines']:
            if line['sku'] not in products or products[line['sku']]['region']!=row['region']:raise DomainError('NO_EVIDENCE','订单SKU未发布或区域不匹配')
        if row['id'] in existing and existing[row['id']].get('import_fingerprint')!=digest(encode(row)):raise DomainError('VERSION_CONFLICT','已有订单禁止覆盖',409)

def install(app,store,identity,envelope):
    @app.get('/api/merchant/setup')
    def setup(actor=Depends(identity)):
        svc.role(actor,{'supervisor'})
        with store.transaction() as db:
            cfg=store.all(db,'merchant_settings')
            return envelope(dict(merchant=cfg[0] if cfg and cfg[0].get('initialized') else None,members=list(actors(store,db).values()),grants=store.all(db,'order_grants'),counts={t:len(store.all(db,t)) for t in ('products','policies','orders','catalog_releases')},source_type='local'))

    @app.get('/api/orders')
    def orders(actor=Depends(identity)):
        with store.transaction() as db:return envelope([o for o in store.all(db,'orders') if o['id'] in actor['orders']])

    @app.post('/api/merchant/initialize')
    def initialize(payload:Initialize,actor=Depends(identity)):
        svc.role(actor,{'supervisor'})
        with store.transaction() as db:
            if any(x.get('initialized') for x in store.all(db,'merchant_settings')) or any(store.all(db,t) for t in ('orders','products','policies','tickets','proposals')):raise DomainError('INVALID_STATE','初始化仅允许空业务库',409)
            cfg=dict(id='merchant',initialized=True,shop_id=payload.shop_id,name=payload.name,source_type='local',version=1)
            db.execute('DELETE FROM members');db.execute('DELETE FROM order_grants')
            for m in payload.members:
                row=m.model_dump(exclude={'token'});row.update(token_hash=digest(m.token),enabled=True)
                store.put(db,'members',m.id,row)
            store.put(db,'merchant_settings','merchant',cfg)
            store.audit(db,actor,'merchant_initialized','merchant',{'shop_id':payload.shop_id,'member_ids':[m.id for m in payload.members]})
            return envelope(cfg)

    @app.post('/api/merchant/grants')
    def grant(payload:Grant,actor=Depends(identity)):
        svc.role(actor,{'supervisor'})
        with store.transaction() as db:
            configured(store,db);store.get(db,'orders',payload.order_id);store.get(db,'members',payload.member_id)
            key=payload.member_id+':'+payload.order_id
            old=next((g for g in store.all(db,'order_grants') if g['id']==key),None)
            if payload.version!=(old['version'] if old else 0):raise DomainError('VERSION_CONFLICT','授权记录已变化',409)
            row=dict(id=key,member_id=payload.member_id,order_id=payload.order_id,enabled=payload.enabled,version=payload.version+1)
            store.put(db,'order_grants',key,row);store.audit(db,actor,'order_grant_changed',key,{'enabled':payload.enabled,'reason':payload.reason})
            return envelope(row)

    @app.post('/api/order-imports/preview')
    def preview(payload:OrderFile,actor=Depends(identity)):
        svc.role(actor,{'supervisor'});rows=parse_orders(payload)
        with store.transaction() as db:
            validate_orders(store,db,rows)
            batch=dict(id=uid('OIMP'),kind='orders',actor=actor['id'],version=1,state='preview',rows=rows,checksum=digest(payload.content),created_at=now())
            store.put(db,'imports',batch['id'],batch);store.audit(db,actor,'order_import_preview',batch['id'],{'count':len(rows)})
            return envelope(batch)

    @app.post('/api/order-imports/{key}/commit')
    def commit(key:str,payload:Commit,actor=Depends(identity)):
        svc.role(actor,{'supervisor'})
        with store.transaction() as db:
            batch=store.get(db,'imports',key)
            if batch['kind']!='orders' or batch['actor']!=actor['id']:raise DomainError('FORBIDDEN','无权提交此批次',403)
            if batch['state']=='committed':return envelope(batch)
            svc.version(batch,payload.version);validate_orders(store,db,batch['rows']);existing={x['id'] for x in store.all(db,'orders')};inserted=0
            for raw in batch['rows']:
                if raw['id'] in existing:continue
                row=dict(raw,version=1,refunded='0.00',sku=raw['lines'][0]['sku'],quantity=sum(x['quantity'] for x in raw['lines']),reshipped_by_line={},import_fingerprint=digest(encode(raw)))
                store.put(db,'orders',row['id'],row);inserted+=1
                g=dict(id=actor['id']+':'+row['id'],member_id=actor['id'],order_id=row['id'],enabled=True,version=1)
                store.put(db,'order_grants',g['id'],g)
            batch.update(state='committed',version=2,inserted=inserted,committed_at=now());store.put(db,'imports',key,batch)
            store.audit(db,actor,'order_import_committed',key,{'inserted':inserted});return envelope(batch)

    @app.get('/api/merchant/order-template')
    def template():
        out=io.StringIO();csv.DictWriter(out,fieldnames=list(Order.model_fields)).writeheader()
        return Response(out.getvalue(),media_type='text/csv',headers={'Content-Disposition':'attachment; filename="order-template.csv"'})

    @app.get('/api/merchant/catalog-template')
    def catalog_template():
        from .catalog_api import Product, Policy
        return Response(json.dumps(dict(products=Product.model_json_schema(), policies=Policy.model_json_schema()),ensure_ascii=False),media_type='application/json',headers={'Content-Disposition':'attachment; filename="catalog-fields.json"'})

    @app.get('/merchant-setup')
    def page():return FileResponse(ROOT/'frontend/merchant-setup.html')
