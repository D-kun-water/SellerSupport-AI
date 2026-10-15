"""local line-level reshipments share approval, execution and reconciliation."""
from copy import deepcopy
from .domain import DomainError, now
from .store import uid, encode
from . import services as svc


def lines(order):
    return order.get('lines') or [dict(id=order['id']+'-L1',sku=order['sku'],quantity=order['quantity'])]


def availability(store,db,order,exclude=None):
    reserved={}
    for proposal in store.all(db,'proposals'):
        if proposal['id']!=exclude and proposal.get('type')=='reship' and proposal['order_id']==order['id'] and proposal['state'] in {'pending_approval','approved','unknown'}:
            for i in proposal['items']:reserved[i['line_id']]=reserved.get(i['line_id'],0)+i['quantity']
    return {line['id']:max(0,line['quantity']-order.get('reshipped_by_line',{}).get(line['id'],0)-reserved.get(line['id'],0)) for line in lines(order)}


def check_items(store,db,order,items,exclude=None):
    if not items or len({i['line_id'] for i in items})!=len(items):
        raise DomainError('VALIDATION_ERROR','补发必须有不重复的订单行')
    known={i['id']:i for i in lines(order)}
    available=availability(store,db,order,exclude)
    for i in items:
        line=known.get(i['line_id'])
        if not line or i['sku']!=line['sku'] or i['quantity']<=0:
            raise DomainError('VALIDATION_ERROR','补发SKU、订单行或数量不合法')
        if i['quantity']>available[i['line_id']]:
            raise DomainError('VALIDATION_ERROR','补发数量超过剩余可补数量（含待批、已批及未知预留）')
    return deepcopy(items)


def create(store,db,actor,payload):
    t=svc.ticket_for(store,db,payload['ticket_id'],actor)
    o=svc.proposal_order(store,db,actor,t)
    key=payload.get('idempotency_key')
    if not key:raise DomainError('VALIDATION_ERROR','补发提案须提供幂等键')
    for old in store.all(db,'proposals'):
        if old.get('type')=='reship' and old.get('request_key')==key and old['created_by']==actor['id']:
            if old['create_request']!=payload:raise DomainError('VERSION_CONFLICT','提案幂等键内容已变化',409)
            return old
    items=check_items(store,db,o,payload['items'])
    row=dict(id=uid('PROP'),ticket_id=t['id'],order_id=o['id'],type='reship',source_type='local',
             execution_mode='manual_record',version=1,state='pending_approval',items=items,amount=None,currency=o['currency'],
             reason=payload['reason'],created_by=actor['id'],approvals=[],executions=[],approved_version=None,
             order_version=o['version'],policy_basis=svc.proposal_policy(store,db,t,'reship'),created_at=now(),
             request_key=key,create_request=deepcopy(payload))
    store.put(db,'proposals',row['id'],row)
    store.audit(db,actor,'reship_proposal_created',row['id'])
    svc.sync_aftercare(store,db,t['id'],actor)
    return row


def update(store,db,actor,row,payload):
    t=svc.ticket_for(store,db,row['ticket_id'],actor)
    o=svc.proposal_order(store,db,actor,t)
    if payload.get('amount') is not None:raise DomainError('VALIDATION_ERROR','补发使用订单行数量，不使用退款金额')
    row['items']=check_items(store,db,o,payload.get('items'),row['id'])
    row.update(reason=payload['reason'],version=row['version']+1,state='pending_approval',approved_version=None,
               order_version=o['version'],policy_basis=svc.proposal_policy(store,db,t,'reship'))
    store.put(db,'proposals',row['id'],row)
    store.audit(db,actor,'reship_approval_invalidated',row['id'])
    svc.sync_aftercare(store,db,t['id'],actor)
    return row


def apply_success(store,db,order,proposal):
    check_items(store,db,order,proposal['items'],proposal['id'])
    quantities=order.setdefault('reshipped_by_line',{})
    for i in proposal['items']:quantities[i['line_id']]=quantities.get(i['line_id'],0)+i['quantity']
    order['version']+=1
    store.put(db,'orders',order['id'],order)


def seed_policy(store):
    # Insert-only, separate policy; return eligibility is not reship eligibility.
    text='local reship policy: only the original order SKU and line may be reshipped; cumulative quantity including pending, approved and unknown reservations cannot exceed original line quantity. Cancelled or fully refunded orders require manual exception review. Supervisor approval is required. A manually recorded shipment reference alone does not verify delivery. Never claim dispatched before an execution record.'
    from hashlib import sha256
    with store.transaction() as db:
        if not any(p.get('kind')=='reships' for p in store.all(db,'policies')):
            src=dict(id='SRC-RESHIP-US',version='1.0',source_type='local',status='published',locator='local/reship-policy',text=text,checksum=sha256(text.encode()).hexdigest())
            p=dict(id='TEST-RESHIP-US-v1',shop_id=svc.SHOP,source_type='local',region='US',channel='direct',kind='reships',version='1.0',valid_from='2026-01-01T00:00:00+00:00',valid_to=None,review_status='published',source_id=src['id'])
            store.put(db,'sources',src['id'],src);store.put(db,'policies',p['id'],p)
