"""Deterministic, evidence-bound baseline. Rules run without calling a model."""
import re
import time
import hashlib
from copy import deepcopy
from .store import encode
from .domain import now, timestamp, DomainError
from .services import active, evidence, order_for, policy_result, ticket_for, version, check_can_generate
from .store import uid

SAFETY = re.compile(r'burnt|burning|smok(?:e|ing)|very hot|overheat|fire|injur|焦味|冒烟|烫|起火', re.I)


def assist(store, db, actor, key, payload, persist=True):
    started = time.perf_counter()
    t = ticket_for(store, db, key, actor)
    version(t, payload['version'])
    check_can_generate(store,db,t)
    prior_clarification_rounds=t.get('clarification_rounds',0)
    text = next((m['text'] for m in reversed(t['messages']) if m['role']=='customer'),t['messages'][-1]['text'])
    items, refs, missing = [], [], []
    order_intent = bool(re.search(r'order|shipment|tracking|delivery|parcel|where|物流', text, re.I))
    tracking_intent = bool(re.search(r'shipment|tracking|parcel|delivery status|where\s+(?:is|are)|物流', text, re.I))
    refund_intent = bool(re.search(r'return|refund|退货|退款', text, re.I))
    if SAFETY.search(text):
        status, draft = 'escalated', 'Please stop using the affected product and contact human support for safety review. Please do not test it by charging again or disassemble it.'
        items.append(dict(intent='safety', state='handoff', next_action='人工安全队列'))
        if tracking_intent:
            items.append(dict(intent='order_status', state='deferred', next_action='安全交接后由人工查询'))
            draft += ' Your tracking question remains pending for human review after the safety review.'
        if refund_intent:
            items.append(dict(intent='returns', state='deferred', next_action='安全交接后由人工核查退货或退款'))
            draft += ' Your return or refund question remains pending for human review after the safety review.'
        summary = '识别到安全信号；停止普通排查。其他事项保留待处理。'
    elif any(store.get(db,'attachments',key)['review_status']=='pending' for key in t.get('attachment_ids',[])):
        status,draft,summary='escalated','Attachments need human review; no product or order conclusion has been verified.','附件待人工核对，不根据未核对内容生成事实结论'
        items.append(dict(intent='attachments',state='handoff',next_action='人工核对原件、解析文本与客户诉求'))
        if tracking_intent:items.append(dict(intent='order_status',state='deferred',next_action='附件核对后由人工查询'))
        if refund_intent:items.append(dict(intent='returns',state='deferred',next_action='附件核对后由人工核查'))
    elif (t.get('device_id') and re.search(r'correction|actually|not\s+TEST-D|纠正|不是', text, re.I)
          and any(x.upper() != t['device_id'].upper() for x in re.findall(r'\bTEST-D\d+\b', text, re.I))):
        status, draft, summary = 'escalated', 'The device model in your latest message conflicts with the confirmed record. Please ask human support to verify the exact model before checking compatibility.', '最新消息纠正型号与已确认字段冲突；不沿用旧型号或自动覆盖事实。'
        items.append(dict(intent='compatibility', state='handoff', next_action='人工核实型号冲突'))
    elif refund_intent or order_intent:
        intent = 'returns' if refund_intent else 'order_status'
        if not t['order_id']:
            status, draft, summary = 'need_clarification', 'Please provide the order reference so we can check the authorized order record.', '缺少订单引用；未查询订单。'
            missing = ['order_id']
            result = 'missing_input'
        else:
            try:
                o = order_for(store, db, t['order_id'], actor)
                if refund_intent:
                    r = policy_result(store, db, o, t['region'], payload['as_of'])
                    refs = r['evidence_refs']
                    result = r['result']
                    if result == 'missing_delivery':
                        status, draft, summary = 'need_clarification', 'Please provide the delivery date, exact time, and time zone (or UTC timestamp) so we can check the return window.', '可靠签收时间缺失，不计算退货期；需完整日期、时分秒与时区。'
                        missing = ['delivered_at']
                    elif result in {'no_evidence', 'conflict'}:
                        status, draft, summary = 'escalated', 'The applicable return policy needs human verification before we can confirm eligibility.', '政策缺失或冲突，需人工确认。'
                    else:
                        status = 'draft'
                        draft = ('The request falls within the return time window. Item condition and refund eligibility still need human review; no refund has been approved or executed.'
                                 if result == 'eligible' else 'The request is outside the return time window. A human must review any exception; no refund has been approved or executed.')
                        summary = r['note']
                else:
                    age = (timestamp(payload['as_of']) - timestamp(o['observed_at'])).total_seconds()
                    days = (timestamp(payload['as_of']) - timestamp(o['last_tracking_at'])).total_seconds() / 86400
                    if age < 0 or days < 0:
                        status, draft, summary, result = 'escalated', 'The snapshot timing needs human verification.', '参考时间早于快照，不能用未来快照推断过去。', 'invalid_time'
                    elif age > o['max_age_seconds']:
                        status, draft, summary, result = 'escalated', 'The imported tracking snapshot is stale. Please have a human verify current tracking.', '旧快照，未查询承运商。', 'stale'
                    else:
                        status, result = 'draft', 'snapshot'
                        draft = f"The imported order snapshot shows {o['status']} as of {o['observed_at']}. Its last tracking event was {days:.2f} days before the reference time. This is not a live carrier check."
                        # A cited snapshot is redacted to authorized tracking facts only.
                        # It is immutable run evidence, not a newly queried carrier response.
                        facts={field:o[field] for field in ('version','status','observed_at','last_tracking_at','max_age_seconds')}
                        source_text=encode(facts)
                        digest=hashlib.sha256(source_text.encode()).hexdigest()
                        refs=[dict(id='TRACKING-SNAPSHOT-'+digest[:16],version=str(o['version']),
                                   locator='authorized-import/tracking-snapshot/'+digest,
                                   text=source_text,checksum=digest,source_type=o['source_type'])]
                        summary = '状态来自授权导入快照；未外联承运商。'
            except DomainError as exc:
                if exc.code != 'FORBIDDEN':
                    raise
                status, result, draft, summary = 'escalated', 'unauthorized', 'We cannot query this order. Please ask a human to verify access.', '授权失败，不披露订单存在性。'
        items.append(dict(intent=intent, state='needs_information' if missing else ('handoff' if status == 'escalated' else 'answered'), rule_result=result, evidence_refs=refs))
        if refund_intent and tracking_intent:
            items.append(dict(intent='order_status', state='deferred', next_action='人工继续核查物流快照'))
            draft += ' Your tracking question remains pending for human review; this reply has not checked current tracking.'
    elif not t['sku'] and not re.search(r'work|compatib|charge|charging|cable|usb|iPad|device|video|适配|充电', text, re.I):
        status, draft, summary = 'escalated', 'Please ask human support to clarify this request before proceeding.', '未识别业务意图，保留原文交人工。'
        items.append(dict(intent='other', state='handoff', next_action='人工确认业务诉求'))
    else:
        ps = active(store, db, 'products', t['region'], payload['as_of'], sku=t['sku']) if t['sku'] else []
        if not t['sku']:
            missing.append('sku')
        if re.search(r'not turn on|does not work|broken|fault|故障', text, re.I):
            status, draft, summary = 'escalated', 'Please contact human support for this product. No verified troubleshooting steps are available in the current manual.', '无可用型号级排查步骤；不生成通用重置建议。'
            if len(ps) == 1:
                refs = [evidence(store, db, ps[0]['source_id'])]
        else:
            if not t['device_id']:
                missing.append('device_id')
            if not t['use_case']:
                missing.append('use_case')
            if missing:
                questions=[]
                if 'sku' in missing:questions.append('Please confirm the exact product model or SKU.')
                if 'device_id' in missing:questions.append('Please confirm the exact device model.')
                if 'use_case' in missing:questions.append('Please confirm whether you need charging, data transfer or video output.')
                status, draft, summary = 'need_clarification', ' '.join(questions), '关键型号或用途缺失；接口同形不能证明适配。'
            elif len(ps) != 1:
                status, draft, summary = 'escalated', 'We do not have an unambiguous published specification for this exact product and market. A human needs to verify it.', '精确 SKU/市场无证据或版本冲突。'
                refs = [evidence(store, db, p['source_id']) for p in ps]
            else:
                p = ps[0]
                refs = [evidence(store, db, p['source_id'])]
                matches = [r for r in p['rules'] if r['device_id'] == t['device_id'] and r['use_case'] == t['use_case']]
                if len(matches) != 1:
                    status, draft, summary = 'escalated', 'Compatibility with this exact device and use case has not been verified. Please ask human support to check the specifications.', '无准确设备级规则，保持未知。'
                else:
                    r = matches[0]
                    status = 'draft'
                    # Chinese knowledge remains the operator's primary content; the
                    # source-bound paired wording keeps the customer draft in English.
                    localized = bool(r.get('conditions_en'))
                    prefix = 'For product'
                    device_prefix = 'device'
                    draft = f"{prefix} {p['sku']} and {device_prefix} {t['device_id']}, the recorded result for {t['use_case']} is {r['result']}. " + ' '.join(r.get('conditions_en') or r['conditions'])
                    summary = '依据准确型号级合成规则；保留全部条件。'
        items.append(dict(intent='compatibility', state='needs_information' if missing else ('handoff' if status == 'escalated' else 'answered'), evidence_refs=refs))
    if missing:
        t['clarification_rounds'] = t.get('clarification_rounds', 0) + 1
        if t['clarification_rounds'] >= 2:
            status = 'escalated'
            summary += ' 连续两轮仍缺关键字段，建议交人工；可继续补充。'
    else:
        t['clarification_rounds'] = 0
    # Immutable evidence snapshots belong to the run, never resolve history to new versions.
    run = dict(id=uid('RUN'), ticket_id=key, source_type='local', execution_mode='rules_only',
               ticket_snapshot=deepcopy(t), actor_scope=dict(role=actor['role'], orders=list(actor['orders'])),
               prior_clarification_rounds=prior_clarification_rounds,
               order_snapshot=(deepcopy(store.get(db,'orders',t['order_id'])) if t.get('order_id') in actor['orders'] else None),
               provider='deterministic', requested_model=None, resolved_model=None, llm_calls=0,
               input_tokens=None, output_tokens=None, cost=None, observed_at=now(), as_of=payload['as_of'],
               context_version=t['context_version'], status=status, reply_draft=draft, summary=summary,
               missing_fields=missing, evidence_refs=refs, item_results=items,
               partial=any(i['state'] == 'deferred' for i in items), total_ms=round((time.perf_counter()-started)*1000, 3),
               schema_version='s1-rules-v1', human_review='required')
    if persist:
        store.put(db, 'runs', run['id'], run)
        t['draft'], t['copied'] = draft, False
        t['version'] += 1
        store.put(db, 'tickets', key, t)
        store.audit(db, actor, 'rules_draft_created', key, dict(run_id=run['id']))
    return run
