import hashlib
from datetime import timedelta
from decimal import Decimal
from .domain import DomainError, timestamp, money, now, TRANSITIONS
from .store import uid, encode

SHOP = 'TEST-SHOP'


def shop(store, db):
    settings = store.all(db, 'merchant_settings')
    if settings and not settings[0].get('initialized'):raise DomainError('NOT_INITIALIZED','先初始化商家',409)
    return settings[0]['shop_id'] if settings else SHOP


def role(actor, allowed):
    if actor['role'] not in allowed:
        raise DomainError('FORBIDDEN', '当前角色没有此操作权限', 403)


def version(obj, expected):
    if obj['version'] != expected:
        raise DomainError('VERSION_CONFLICT', '记录已变化，请刷新后重试', 409)


def order_for(store, db, key, actor):
    # Unauthorized and nonexistent share the same response. Scope is server-owned.
    if key not in actor['orders']:
        raise DomainError('FORBIDDEN', '无法查询此订单，请核查授权', 403)
    try:
        return store.get(db, 'orders', key)
    except DomainError:
        raise DomainError('FORBIDDEN', '无法查询此订单，请核查授权', 403)


def can_view_ticket(t, actor):
    if t.get('order_id') and t['order_id'] not in actor['orders']:
        return False
    return (t['owner'] == actor['id'] or actor['role'] == 'supervisor' or
            (t.get('handoff_assignee') == actor['id'] and
             (not t.get('order_id') or t['order_id'] in actor['orders'])))


def ticket_for(store, db, key, actor):
    t = store.get(db, 'tickets', key)
    if not can_view_ticket(t, actor):
        raise DomainError('FORBIDDEN', '无权查看此工单', 403)
    return t


def check_can_resolve(store, db, t):
    if any(p['ticket_id']==t['id'] and p['state']=='unknown' for p in store.all(db,'proposals')):
        raise DomainError('UNKNOWN_EXECUTION', '售后执行结果未知，先完成对账再结案', 409)
    if any(p['ticket_id']==t['id'] and p['state'] in {'pending_approval','approved'} for p in store.all(db,'proposals')):
        raise DomainError('PENDING_AFTERCARE', '售后仍待审批或执行，完成后再结案', 409)


def sync_aftercare(store, db, ticket_id, actor):
    t = store.get(db, 'tickets', ticket_id)
    if t['status'] in {'closed', 'resolved'} or active_handoff(store, db, t):
        return
    pending = [p['state'] for p in store.all(db, 'proposals') if p['ticket_id'] == ticket_id]
    target = ('waiting_execution' if any(s in {'approved', 'unknown'} for s in pending)
              else 'waiting_approval' if 'pending_approval' in pending else 'in_progress')
    if t['status'] != target:
        t.update(status=target, version=t['version'] + 1)
        store.put(db, 'tickets', t['id'], t)
        store.audit(db, actor, 'ticket_aftercare_updated', t['id'], {'status': target})


def active_handoff(store, db, t):
    return bool(t.get('handoff_id') and store.get(db,'handoffs',t['handoff_id'])['state']!='closed')


def check_can_generate(store, db, t):
    if t['status'] in {'closed','resolved'}:
        raise DomainError('INVALID_STATE','已结案工单先重开再生成回复',409)
    if active_handoff(store,db,t):
        raise DomainError('INVALID_STATE','负责人正在处理人工交接，暂不生成AI回复',409)


def check_can_reply(store, db, t, actor):
    if t['status'] in {'closed','resolved'}:
        raise DomainError('INVALID_STATE','已结案工单先重开再修改或记录回复',409)
    if active_handoff(store,db,t) and t.get('handoff_assignee')!=actor['id']:
        raise DomainError('FORBIDDEN','人工交接期间仅当前负责人可以修改或记录回复',403)


def active(store, db, table, region, as_of, **filters):
    dt = timestamp(as_of)
    return [x for x in store.all(db, table)
            if x['source_type'] == 'local' and x['shop_id'] == shop(store, db)
            and x['review_status'] == 'published' and x['region'] == region
            and timestamp(x['valid_from']) <= dt
            and (not x.get('valid_to') or dt < timestamp(x['valid_to']))
            and all(x.get(k) == v for k, v in filters.items())]


def evidence(store, db, source_id):
    s = store.get(db, 'sources', source_id)
    if s['status'] != 'published':
        raise DomainError('NO_EVIDENCE', '来源已停用', 409)
    return {k: s[k] for k in ('id', 'version', 'locator', 'text', 'checksum', 'source_type')}


def policy_result(store, db, order, region, as_of):
    ps = active(store, db, 'policies', region, as_of, kind='returns', channel=order.get('channel','direct'))
    if not ps:
        return dict(result='no_evidence', evidence_refs=[])
    refs = [evidence(store, db, p['source_id']) for p in ps]
    if len(ps) != 1:
        return dict(result='conflict', evidence_refs=refs)
    p = ps[0]
    if not order.get('delivered_at'):
        return dict(result='missing_delivery', policy_id=p['id'], evidence_refs=refs)
    elapsed = timestamp(as_of) - timestamp(order['delivered_at'])
    eligible = timedelta(0) <= elapsed <= timedelta(days=p['window_days'])
    return dict(result='eligible' if eligible else 'outside_window', policy_id=p['id'],
                elapsed_seconds=int(elapsed.total_seconds()), window_days=p['window_days'],
                anchor='delivered_at', as_of=as_of, evidence_refs=refs,
                note='仅判断退货时间窗口；商品状态与凭据仍须人工复核，不构成批准')


def create_ticket(store, db, actor, payload):
    role(actor, {'agent', 'supervisor'})
    if payload.get('order_id'):
        order_for(store, db, payload['order_id'], actor)
    external = payload.get('external_id')
    if external:
        for t in store.all(db, 'tickets'):
            if t['external_id'] == external and t['channel'] == payload['channel']:
                ticket_for(store, db, t['id'], actor)
                return t
    t = dict(id=uid('TKT'), shop_id=shop(store, db), source_type='local', owner=actor['id'],
             status='new', version=1, context_version=1, external_id=external,
             channel=payload['channel'], order_id=payload.get('order_id'),
             sku=payload.get('sku'), device_id=payload.get('device_id'), use_case=payload.get('use_case'),
             region=payload['region'], messages=[dict(id=uid('MSG'), role='customer', text=payload['message'], received_at=now())],
             draft='', copied=False, clarification_rounds=0, sent_confirmations=[], resolution=None, created_at=now())
    store.put(db, 'tickets', t['id'], t)
    store.audit(db, actor, 'ticket_created', t['id'])
    return t


def change_ticket(store, db, actor, key, payload):
    t = ticket_for(store, db, key, actor)
    version(t, payload['version'])
    target = payload.get('status')
    if target=='reopened' and t.get('handoff_id') and not active_handoff(store,db,t):
        raise DomainError('INVALID_STATE','此工单有关联交接，请从人工交接队列重开并保留原处理轮次',409)
    if target in {'resolved','closed'}:
        check_can_resolve(store,db,t)
        if active_handoff(store,db,t):
            raise DomainError('INVALID_STATE','人工交接尚未完成，请由接单负责人记录结果后结案',409)
    if target and target != t['status']:
        if target not in TRANSITIONS[t['status']]:
            raise DomainError('VALIDATION_ERROR', '不允许此状态转换')
        if target in {'resolved', 'closed'} and not payload.get('resolution'):
            raise DomainError('VALIDATION_ERROR', '处理完成必须记录实际结果与凭据')
        t['status'] = target
        if payload.get('resolution'):
            t['resolution'] = payload['resolution']
    context_changed = False
    if 'order_id' in payload and payload['order_id'] != t.get('order_id'):
        if any(p['ticket_id'] == key for p in store.all(db, 'proposals')) or active_handoff(store, db, t):
            raise DomainError('INVALID_STATE', '已有售后或交接记录，不能更换关联订单', 409)
        if payload['order_id']:
            order_for(store, db, payload['order_id'], actor)
    for k in ('sku', 'device_id', 'use_case', 'region', 'order_id'):
        if k in payload and payload[k] != t[k]:
            store.audit(db, actor, 'fact_superseded', key, {k: {'old': t[k], 'new': payload[k]}})
            t[k] = payload[k]
            context_changed = True
    if payload.get('message'):
        t['messages'].append(dict(id=uid('MSG'), role='customer', text=payload['message'], received_at=now()))
        context_changed = True
    if context_changed:
        t['context_version'] += 1
        t['clarification_rounds'] = 0
        t['draft'] = ''
        t['copied'] = False
    if 'draft' in payload:
        check_can_reply(store,db,t,actor)
        t['draft'] = payload['draft']
        t['copied'] = False
        if payload['draft'].strip() and t['status'] in {'new','reopened'}:
            t['status'] = 'in_progress'
    t['version'] += 1
    store.put(db, 'tickets', key, t)
    store.audit(db, actor, 'ticket_updated', key, dict(version=t['version'], status=t['status']))
    return t


def proposal_order(store, db, actor, t):
    if not t.get('order_id'):
        raise DomainError('VALIDATION_ERROR', '退款提案必须关联已授权订单')
    o = order_for(store, db, t['order_id'], actor)
    if o['status'] in {'cancelled', 'refunded'}:
        raise DomainError('STALE_DATA', '订单当前状态不允许该提案', 409)
    return o


def check_amount(o, amount, currency):
    amount = money(amount)
    if currency != o['currency'] or Decimal(amount) <= 0 or Decimal(amount) > Decimal(o['paid']) - Decimal(o['refunded']):
        raise DomainError('VALIDATION_ERROR', '币种或可退金额不合法')
    return amount


def create_proposal(store, db, actor, payload):
    role(actor, {'agent', 'supervisor'})
    if payload.get('type')=='reship':
        from .reships import create
        return create(store,db,actor,payload)
    t = ticket_for(store, db, payload['ticket_id'], actor)
    o = proposal_order(store, db, actor, t)
    amount = check_amount(o, payload['amount'], payload['currency'])
    basis = proposal_policy(store, db, t)
    p = dict(id=uid('PROP'), ticket_id=t['id'], order_id=o['id'], type='refund', source_type='local',
             execution_mode='manual_record', version=1, state='pending_approval', amount=amount,
             currency=o['currency'], reason=payload['reason'], created_by=actor['id'], approvals=[], executions=[],
             approved_version=None, order_version=o['version'], policy_basis=basis, created_at=now())
    store.put(db, 'proposals', p['id'], p)
    store.audit(db, actor, 'proposal_created', p['id'])
    sync_aftercare(store, db, t['id'], actor)
    return p


def proposal_for(store, db, actor, key):
    p = store.get(db, 'proposals', key)
    ticket_for(store, db, p['ticket_id'], actor)
    return p


def proposal_policy(store, db, t, kind='refund'):
    ps = active(store, db, 'policies', t['region'], now(), kind='reships' if kind=='reship' else 'returns', channel=t['channel'])
    if len(ps) != 1:
        raise DomainError('STALE_DATA', '提案需要唯一适用政策；缺失或冲突须先人工核查', 409)
    p = ps[0]
    ref = evidence(store, db, p['source_id'])
    return dict(policy_id=p['id'], version=p['version'],
                policy_hash=hashlib.sha256(encode(p).encode()).hexdigest(), source_checksum=ref['checksum'])


def check_policy(store, db, t, p):
    if proposal_policy(store, db, t, p.get('type','refund')) != p.get('policy_basis'):
        raise DomainError('STALE_DATA', '政策版本变化，提案需修改后重新审核', 409)


def update_proposal(store, db, actor, key, payload):
    p = proposal_for(store, db, actor, key)
    version(p, payload['version'])
    if p['state'] in {'unknown', 'executed'}:
        raise DomainError('UNKNOWN_EXECUTION', '未知结果须先对账；已执行记录不可改写', 409)
    if p.get('type')=='reship':
        from .reships import update
        return update(store,db,actor,p,payload)
    if payload.get('items'):
        raise DomainError('VALIDATION_ERROR','退款提案不能改为补发')
    t = ticket_for(store, db, p['ticket_id'], actor)
    o = proposal_order(store, db, actor, t)
    p['amount'] = check_amount(o, payload['amount'], p['currency'])
    p['reason'] = payload['reason']
    p['version'] += 1
    p['state'], p['approved_version'] = 'pending_approval', None
    p['order_version'] = o['version']
    p['policy_basis'] = proposal_policy(store, db, t)
    store.put(db, 'proposals', key, p)
    store.audit(db, actor, 'approval_invalidated', key, dict(version=p['version']))
    sync_aftercare(store, db, p['ticket_id'], actor)
    return p


def approve(store, db, actor, key, payload):
    role(actor, {'supervisor'})
    p = proposal_for(store, db, actor, key)
    version(p, payload['version'])
    if p['state'] != 'pending_approval':
        raise DomainError('VERSION_CONFLICT', '提案不在待审批状态', 409)
    if p['created_by'] == actor['id']:
        raise DomainError('FORBIDDEN', '申请人与审批人必须分开', 403)
    t = ticket_for(store, db, p['ticket_id'], actor)
    o = proposal_order(store, db, actor, t)
    if p['order_version'] != o['version']:
        raise DomainError('STALE_DATA', '订单版本变化，需修改后重新审核', 409)
    check_policy(store, db, t, p)
    check_proposal_limits(store,db,o,p)
    p['state'] = 'approved' if payload['decision'] == 'approve' else 'rejected'
    p['approved_version'] = p['version'] if p['state'] == 'approved' else None
    p['approvals'].append(dict(actor=actor['id'], proposal_version=p['version'], decision=payload['decision'], reason=payload['reason'], at=now()))
    store.put(db, 'proposals', key, p)
    store.audit(db, actor, 'proposal_reviewed', key, dict(state=p['state']))
    sync_aftercare(store, db, p['ticket_id'], actor)
    return p


def record_execution(store, db, actor, key, payload):
    role(actor, {'agent'})
    p = proposal_for(store, db, actor, key)
    version(p, payload['version'])
    for e in p['executions']:
        if e['idempotency_key'] == payload['idempotency_key']:
            if e['request_hash'] != hashlib.sha256(encode(payload).encode()).hexdigest():
                raise DomainError('VERSION_CONFLICT', '幂等键不能用于不同凭据', 409)
            return p
    if p['state'] != 'approved' or p['approved_version'] != p['version']:
        raise DomainError('UNKNOWN_EXECUTION' if p['state'] == 'unknown' else 'VERSION_CONFLICT', '仅有效批准可以记录执行；未知结果必须先对账', 409)
    t = ticket_for(store, db, p['ticket_id'], actor)
    o = proposal_order(store, db, actor, t)
    if o['version'] != p['order_version']:
        raise DomainError('STALE_DATA', '订单已变化，批准不能继续使用', 409)
    check_policy(store, db, t, p)
    check_proposal_limits(store,db,o,p)
    e = dict(**payload, actor=actor['id'], observed_at=now(), request_hash=hashlib.sha256(encode(payload).encode()).hexdigest())
    p['executions'].append(e)
    p['state'] = {'success': 'executed', 'failed': 'failed', 'unknown': 'unknown'}[payload['result']]
    if payload['result'] == 'success':
        if p.get('type')=='reship':
            from .reships import apply_success
            apply_success(store,db,o,p)
        else:
            o['refunded'] = money(Decimal(o['refunded']) + Decimal(p['amount']))
            o['version'] += 1
            if Decimal(o['refunded']) == Decimal(o['paid']):
                o['status'] = 'refunded'
            store.put(db, 'orders', o['id'], o)
    store.put(db, 'proposals', key, p)
    store.audit(db, actor, 'manual_execution_recorded', key, dict(result=payload['result']))
    sync_aftercare(store, db, p['ticket_id'], actor)
    return p


def check_proposal_limits(store,db,order,proposal):
    if proposal.get('type')=='reship':
        from .reships import check_items
        check_items(store,db,order,proposal['items'],proposal['id'])
    else:check_amount(order,proposal['amount'],proposal['currency'])
