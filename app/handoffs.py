"""Local local human handoff records; assignment never grants order access."""
from copy import deepcopy
from . import services as svc
from .store import uid
from .domain import DomainError, now


def require(condition, message, code='INVALID_STATE', status=409):
    if not condition:
        raise DomainError(code,message,status)


def check_assignee(store, db, t, assignee, team):
    require(assignee in team,'请选择现有成员','VALIDATION_ERROR',422)
    member=team[assignee]
    require(not t.get('order_id') or t['order_id'] in member['orders'],
            '负责人没有此订单授权，不能通过指派扩大访问范围','FORBIDDEN',403)


def snapshot(store, db, t, actor):
    order=svc.order_for(store,db,t['order_id'],actor) if t.get('order_id') else None
    runs=[r for r in store.all(db,'runs') if r['ticket_id']==t['id'] and r['context_version']==t['context_version']]
    run=runs[-1] if runs else None
    return dict(ticket=deepcopy(t),order=deepcopy(order),run=deepcopy(run),
                attachments=[{k:v for k,v in store.get(db,'attachments',key).items() if k!='original_base64'} for key in t.get('attachment_ids',[])],
                evidence_refs=deepcopy(run['evidence_refs']) if run else [],
                missing_fields=deepcopy(run['missing_fields']) if run else [],
                pending_items=deepcopy(run['item_results']) if run else [],captured_at=now())


def create(store, db, actor, key, p, team):
    t=svc.ticket_for(store,db,key,actor)
    svc.version(t,p['version'])
    require(t['status'] not in {'closed','resolved'},'已结案工单先重开再交接')
    require(not t.get('handoff_id'),'已有交接记录，请重开或重新指派，保留原链路')
    check_assignee(store,db,t,p['assignee'],team)
    row=dict(id=uid('HAND'),ticket_id=key,source_type='local',version=1,state='assigned',
             assignee=p['assignee'],created_by=actor['id'],reason=p['reason'],attempted_steps=p['attempted_steps'],
             pending_checks=p['pending_checks'],snapshot=snapshot(store,db,t,actor),
             history=[],notes=[],cycles=[],created_at=now(),resolution=None)
    event(row,actor,'assigned',p['reason'])
    store.put(db,'handoffs',row['id'],row)
    t.update(handoff_id=row['id'],handoff_assignee=row['assignee'],status='escalated',draft='',copied=False,version=t['version']+1)
    store.put(db,'tickets',key,t)
    store.audit(db,actor,'human_handoff_assigned',row['id'],{'assignee':row['assignee']})
    return row


def event(row, actor, action, note):
    row['history'].append(dict(action=action,actor=actor['id'],at=now(),note=note,assignee=row['assignee'],state=row['state']))


def action(store, db, actor, key, p, team):
    row=store.get(db,'handoffs',key)
    t=svc.ticket_for(store,db,row['ticket_id'],actor)
    svc.version(row,p['version'])
    op=p['action']
    if op in {'accept','note','close'}:
        require(actor['id']==row['assignee'],'仅当前负责人可以接单、记录处理和结案','FORBIDDEN',403)
    if op=='accept':
        require(row['state']=='assigned','仅待接单交接可以接受')
        row['state']='in_progress'
    elif op=='note':
        require(row['state']=='in_progress','接单后才能记录处理')
        row['notes'].append(dict(actor=actor['id'],text=p['note'],reference=p.get('reference'),at=now(),ticket_context_version=t['context_version']))
    elif op=='close':
        require(row['state']=='in_progress','接单后才能结案')
        require(p.get('reference') and len(p['reference'].strip())>=3,'结案必须填写处理凭据','VALIDATION_ERROR',422)
        svc.version(t,p['ticket_version'])
        svc.check_can_resolve(store,db,t)
        row.update(state='closed',resolution=dict(result=p['note'],reference=p['reference'],actor=actor['id'],at=now(),
                    ticket_snapshot=deepcopy(t)))
        t.update(status='closed',resolution=p['note']+'；'+p['reference'],draft='',copied=False)
    elif op=='reassign':
        svc.role(actor,{'supervisor'})
        require(row['state']!='closed','已结案交接先重开')
        check_assignee(store,db,t,p.get('assignee'),team)
        row.update(assignee=p['assignee'],state='assigned')
        t['handoff_assignee']=p['assignee']
    elif op=='reopen':
        require(row['state']=='closed','只有已结案交接可以重开')
        row['cycles'].append(dict(resolution=deepcopy(row['resolution']),snapshot=deepcopy(row['snapshot']),
                                  notes=deepcopy(row['notes']),assignee=row['assignee']))
        row.update(state='assigned',resolution=None,notes=[],snapshot=snapshot(store,db,t,actor))
        t.update(status='reopened',resolution=None,draft='',copied=False)
    event(row,actor,op,p['note'])
    row['version']+=1
    row['updated_at']=now()
    t['version']+=1
    store.put(db,'handoffs',key,row)
    store.put(db,'tickets',t['id'],t)
    store.audit(db,actor,'human_handoff_'+op,key,{'assignee':row['assignee']})
    return row


def reconcile(store, db, actor, key, p):
    svc.role(actor,{'supervisor'})
    row=svc.proposal_for(store,db,actor,key)
    history=row.setdefault('reconciliations',[])
    from hashlib import sha256
    from .store import encode
    request_hash=sha256(encode(p).encode()).hexdigest()
    for old in history:
        if old['idempotency_key']==p['idempotency_key']:
            require(old['request_hash']==request_hash,'对账幂等键不能重复用于其他内容','VERSION_CONFLICT')
            return row
    svc.version(row,p['version'])
    require(row['state']=='unknown','只允许对未知执行结果对账')
    from decimal import Decimal
    from .domain import money
    if row.get('type')=='reship':
        require(p.get('items')==row['items'] and p.get('amount') is None,'对账订单行必须匹配原补发提案','VALIDATION_ERROR',422)
    else:require(money(p['amount'])==row['amount'] and p['currency']==row['currency'],'对账金额与币种必须匹配原提案','VALIDATION_ERROR',422)
    order=svc.order_for(store,db,row['order_id'],actor)
    svc.version(order,p['order_version'])
    # Historical success may be recorded after a policy change, but an order
    # change requires investigation: don't double count an outside refund.
    if p['result']=='success':
        require(order['version']==row['order_version'],'未知期间订单已变化，先核实余额和其他退款，不能自动入账','STALE_DATA')
        if row.get('type')=='reship':
            from .reships import apply_success
            apply_success(store,db,order,row)
        else:
            svc.check_amount(order,row['amount'],row['currency'])
            order['refunded']=money(Decimal(order['refunded'])+Decimal(row['amount']))
            order['version']+=1
            if Decimal(order['refunded'])==Decimal(order['paid']):order['status']='refunded'
            store.put(db,'orders',order['id'],order)
    history.append(dict(**p,actor=actor['id'],at=now(),request_hash=request_hash))
    row['state']={'success':'executed','failed':'failed','still_unknown':'unknown'}[p['result']]
    row['approved_version']=None
    row['version']+=1
    store.put(db,'proposals',key,row)
    store.audit(db,actor,'test_review_execution_reconciled',key,{'result':p['result'],'receipt':p['receipt']})
    svc.sync_aftercare(store,db,row['ticket_id'],actor)
    return row
