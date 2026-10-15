"""Local feedback and improvement workflow. Human decisions are explicit API actions."""
from copy import deepcopy
from .domain import DomainError, now, timestamp
from .store import uid
from . import services as svc
from .knowledge import digest

LABELS = {'compatibility', 'instructions', 'quality', 'logistics', 'unknown'}
SCOPE = ('sku', 'device_id', 'use_case', 'region', 'channel', 'label', 'source_type')


def require(condition, message, code='VALIDATION_ERROR', status=422):
    if not condition:
        raise DomainError(code, message, status)


def save(store, db, table, row, actor, action):
    row['version'] += 1
    row['updated_at'] = now()
    store.put(db, table, row['id'], row)
    store.audit(db, actor, action, row['id'], {'version': row['version']})
    return row


def editable(store, db, table, key, expected, actor):
    svc.role(actor, {'supervisor'})
    row = store.get(db, table, key)
    svc.version(row, expected)
    if table == 'issues':
        require(row['state'] not in {'merged', 'dismissed'}, '已合并或拒绝的专题需先撤回操作', 'INVALID_STATE', 409)
    return row


def task_for(store, db, actor, key, expected=None, owner_only=False):
    row = store.get(db, 'tasks', key)
    if actor['role'] != 'supervisor' or owner_only:
        require(row.get('owner_id') == actor['id'], '只能处理分派给本人的任务', 'FORBIDDEN', 403)
    if expected is not None:
        svc.version(row, expected)
    return row


def task_member(store, db, key):
    from .merchant import actors
    members = actors(store, db)
    require(key in members, '请选择当前有效成员作为负责人')
    return members[key]


def assign_task(store, db, actor, key, p):
    row = editable(store, db, 'tasks', key, p['version'], actor)
    require(row['state'] not in {'done','cancelled'}, '已结束任务需先重开', 'INVALID_STATE', 409)
    member = task_member(store, db, p['owner'])
    row.setdefault('assignment_history', []).append(dict(previous_owner=row.get('owner_id'),
        owner=member['id'], reason=p['reason'], actor=actor['id'], at=now()))
    row.update(owner_id=member['id'], owner=member['label'])
    return save(store, db, 'tasks', row, actor, 'task_assigned')


def nominate(store, db, actor, p):
    ticket = svc.ticket_for(store, db, p['ticket_id'], actor)
    message = next((m for m in ticket['messages'] if m['id'] == p['message_id']), None)
    require(message is not None, '片段必须来自关联工单消息')
    require(p['quote'].strip() and p['quote'] in message['text'], '反馈必须是客户原文的连续片段')
    require(ticket['sku'], '先核实 SKU 再提名商品反馈')
    product = store.get(db, 'products', ticket['sku'])
    identity = (ticket['id'], p['message_id'], p['quote'], p['label'])
    for f in store.all(db, 'feedback'):
        if (f['ticket_id'], f['message_id'], f['quote'], f['label']) == identity:
            return f
    source = store.get(db, 'sources', product['source_id'])
    row = dict(id=uid('FB'), version=1, state='candidate', ticket_id=ticket['id'], message_id=message['id'],
               quote=p['quote'], label=p['label'], sku=ticket['sku'], device_id=ticket['device_id'],
               use_case=ticket['use_case'], region=ticket['region'], channel=ticket['channel'],
               source_type=ticket['source_type'], observed_at=message['received_at'],
               message_snapshot=deepcopy(message), product_version=product['version'], source_snapshot=source,
               nominated_by=actor['id'], created_at=now())
    store.put(db, 'feedback', row['id'], row)
    store.audit(db, actor, 'feedback_nominated', row['id'])
    return row


def scope(row):
    return tuple(row.get(k) for k in SCOPE)


def suggestion_groups(store, db):
    groups = {}
    for f in store.all(db, 'feedback'):
        if f['state'] == 'candidate':
            groups.setdefault(scope(f), []).append(f)
    return [dict(scope={k: rows[0].get(k) for k in SCOPE}, feedback_ids=[r['id'] for r in rows],
                 ticket_count=len({r['ticket_id'] for r in rows}), grouping='rule_candidate_not_cause')
            for rows in groups.values()]


def create_issue(store, db, actor, p):
    svc.role(actor, {'supervisor'})
    ids = list(dict.fromkeys(p['feedback_ids']))
    rows = [store.get(db, 'feedback', k) for k in ids]
    require(rows, '专题至少关联一条反馈')
    require(len({scope(f) for f in rows}) == 1, '不同 SKU、型号、用途、地区、渠道或标签必须分组处理')
    require(all(f['state'] != 'rejected' for f in rows), '已拒绝反馈不能进入新专题')
    row = dict(id=uid('ISS'), title=p['title'], version=1, state='under_review',
               **{k: rows[0].get(k) for k in SCOPE}, members=[dict(feedback_id=k, decision='pending') for k in ids],
               created_at=now(), created_by=actor['id'], history=[])
    store.put(db, 'issues', row['id'], row)
    store.audit(db, actor, 'issue_created', row['id'])
    return issue_view(store, db, row)


def issue_view(store, db, row):
    result = deepcopy(row)
    refs = [store.get(db, 'feedback', m['feedback_id']) for m in row['members'] if m['decision'] == 'accepted']
    result.update(ticket_count=len({f['ticket_id'] for f in refs}), feedback_count=len(refs),
                  time_range=[min((f['observed_at'] for f in refs), default=None), max((f['observed_at'] for f in refs), default=None)],
                  coverage='仅已导入并人工接受的反馈；没有销售分母', rates=None)
    return result


def review_members(store, db, actor, key, p):
    row = editable(store, db, 'issues', key, p['version'], actor)
    require(row['state'] in {'under_review', 'investigating'}, '已进入任务的专题成员不能直接改写，请创建新专题', 'INVALID_STATE', 409)
    choices = {x['feedback_id']: x['decision'] for x in p['decisions']}
    used_refs = {ref for h in store.all(db, 'hypotheses') if h['issue_id'] == key
                for ref in h['support_refs'] + h['counter_refs']}
    require(not any(x['decision'] == 'rejected' and x['feedback_id'] in used_refs for x in p['decisions']),
            '片段已被待核实原因引用，不能拒绝并破坏证据链；请另建专题保留原审核', 'INVALID_STATE', 409)
    require(len(choices) == len(p['decisions']), '同一反馈不能在一次请求中重复审核')
    require(set(choices) <= {m['feedback_id'] for m in row['members']}, '只能审核专题已有成员')
    for member in row['members']:
        if member['feedback_id'] in choices:
            member.update(decision=choices[member['feedback_id']], reviewer=actor['id'], reason=p['reason'], reviewed_at=now())
            f = store.get(db, 'feedback', member['feedback_id'])
            # Membership is authoritative: rejection in one topic does not remove accepted evidence elsewhere.
            accepted_elsewhere = any(m['feedback_id'] == f['id'] and m['decision'] == 'accepted'
                                     for i in store.all(db, 'issues') if i['id'] != key and i['state'] != 'merged' for m in i['members'])
            f['state'] = 'accepted' if member['decision'] == 'accepted' or accepted_elsewhere else 'rejected'
            save(store, db, 'feedback', f, actor, 'feedback_reviewed')
    row['state'] = 'investigating' if any(m['decision'] == 'accepted' for m in row['members']) else 'under_review'
    save(store, db, 'issues', row, actor, 'issue_members_reviewed')
    return issue_view(store, db, row)


def grouping_operation(store, db, actor, key, p):
    row = editable(store, db, 'issues', key, p['version'], actor)
    require(row['state'] in {'under_review', 'investigating'}, '有下游任务的专题不允许合并拆分', 'INVALID_STATE', 409)
    require(not any(h['issue_id'] == key for h in store.all(db, 'hypotheses')), '已有待核实原因，需保留原专题并单独归集', 'INVALID_STATE', 409)
    before = [deepcopy(row)]
    if p['action'] == 'merge':
        other = editable(store, db, 'issues', p['other_id'], p['other_version'], actor)
        require(other['id'] != key and scope(other) == scope(row), '只能合并同 SKU/型号/用途/市场/渠道/标签的不同专题')
        require(other['state'] in {'under_review', 'investigating'} and
                not any(h['issue_id'] == other['id'] for h in store.all(db, 'hypotheses')), '另一专题已有下游原因或任务', 'INVALID_STATE', 409)
        before.append(deepcopy(other))
        members = {m['feedback_id']: m for m in row['members']}
        for m in other['members']:
            old = members.get(m['feedback_id'])
            require(not old or old['decision'] == m['decision'], '同一反馈审核结果冲突，请先统一审核')
            members[m['feedback_id']] = m
        row['members'] = list(members.values())
        other.update(state='merged', merged_into=key)
        save(store, db, 'issues', other, actor, 'issue_merged')
        changed = [row, other]
    else:
        ids = set(p['feedback_ids'])
        require(ids and ids < {m['feedback_id'] for m in row['members']}, '拆分必须选择部分已有成员')
        other = deepcopy(row)
        other.update(id=uid('ISS'), version=1, title=p['title'], created_at=now(), split_from=key,
                     members=[m for m in row['members'] if m['feedback_id'] in ids], history=[])
        row['members'] = [m for m in row['members'] if m['feedback_id'] not in ids]
        store.put(db, 'issues', other['id'], other)
        changed = [row, other]
    save(store, db, 'issues', row, actor, 'issue_group_changed')
    op = dict(id=uid('GROUP'), version=1, state='applied', action=p['action'], before=before,
              after={i['id']: digest(i) for i in changed}, created_id=other['id'] if p['action'] == 'split' else None,
              reason=p['reason'], actor=actor['id'], source_type='local', created_at=now())
    store.put(db, 'issue_operations', op['id'], op)
    store.audit(db, actor, 'issue_group_operation', key, {'operation_id': op['id'], 'reason': p['reason']})
    return op


def undo_grouping(store, db, actor, key, p):
    op = editable(store, db, 'issue_operations', key, p['version'], actor)
    require(op['state'] == 'applied', '该操作已撤回', 'INVALID_STATE', 409)
    for issue_id, expected_hash in op['after'].items():
        row = store.get(db, 'issues', issue_id)
        require(digest(row) == expected_hash, '操作后专题已变化，不能覆盖后续审核', 'VERSION_CONFLICT', 409)
        require(not any(h['issue_id'] == issue_id for h in store.all(db, 'hypotheses')) and
                not any(t['issue_id'] == issue_id for t in store.all(db, 'tasks')), '专题已有下游记录，不能撤回', 'INVALID_STATE', 409)
    for old in op['before']:
        old['version'] = store.get(db, 'issues', old['id'])['version']
        save(store, db, 'issues', old, actor, 'issue_group_restored')
    if op['created_id']:
        created = store.get(db, 'issues', op['created_id'])
        created.update(state='dismissed', undo_reason=p['reason'])
        save(store, db, 'issues', created, actor, 'split_retracted')
    op.update(state='reverted', undo_reason=p['reason'], reverted_at=now())
    return save(store, db, 'issue_operations', op, actor, 'issue_group_reverted')


def add_hypothesis(store, db, actor, key, p):
    issue = editable(store, db, 'issues', key, p['version'], actor)
    require(issue['state'] == 'investigating', '先接受反馈，再建立待核实原因', 'INVALID_STATE', 409)
    accepted = {m['feedback_id'] for m in issue['members'] if m['decision'] == 'accepted'}
    require(set(p['support_refs'] + p['counter_refs']) <= accepted, '原因证据必须来自本专题已接受反馈')
    require(not set(p['support_refs']) & set(p['counter_refs']), '同一证据不能同时为支持与反对')
    refs = {k: store.get(db, 'feedback', k) for k in set(p['support_refs'] + p['counter_refs'])}
    row = dict(id=uid('CAUSE'), issue_id=key, version=1, state='unconfirmed', source_type='local',
               type=p['type'], description=p['description'], support_refs=p['support_refs'], counter_refs=p['counter_refs'],
               missing_checks=p['missing_checks'], evidence_snapshots=refs, created_by=actor['id'], created_at=now())
    store.put(db, 'hypotheses', row['id'], row)
    save(store, db, 'issues', issue, actor, 'issue_hypothesis_added')
    return row


def confirm_hypothesis(store, db, actor, key, p):
    row = editable(store, db, 'hypotheses', key, p['version'], actor)
    require(row['state'] == 'unconfirmed', '原因已审核', 'INVALID_STATE', 409)
    if p['decision'] == 'confirm':
        require(row['support_refs'], '确认原因必须有关联支持证据')
        if row['type'] == 'instructions':
            f = row['evidence_snapshots'][row['support_refs'][0]]
            require(p['before_source_id'] == f['source_snapshot']['id'], '版本对照必须使用投诉时保存的来源')
            require(p['proposed_text'] and len(p['proposed_text'].strip()) >= 20 and
                    p['proposed_text'] != f['source_snapshot']['text'], '说明缺项确认必须提供修改前后内容')
            row['version_comparison'] = dict(before=f['source_snapshot'], proposed_text=p['proposed_text'])
        elif row['type'] == 'quality':
            require(p['verification_record'] and len(p['verification_record'].strip()) >= 20,
                    '疑似质量原因需商品/供应链检测或确认记录，不能仅凭聊天归集')
        else:
            require(p['verification_record'] and len(p['verification_record'].strip()) >= 10, '原因确认需额外验证记录')
    row.update(state='confirmed' if p['decision'] == 'confirm' else 'rejected', reviewer=actor['id'],
               reviewed_at=now(), reason=p['reason'], verification_record=p['verification_record'])
    return save(store, db, 'hypotheses', row, actor, 'hypothesis_reviewed')


def change_issue(store, db, actor, key, p):
    row = editable(store, db, 'issues', key, p['version'], actor)
    if p['state'] == 'closed':
        tasks = [t for t in store.all(db, 'tasks') if t['issue_id'] == key]
        require(row['state'] == 'observing' and tasks and all(t['state'] in {'done', 'cancelled'} for t in tasks),
                '专题关闭需完成任务验收，保留观察结论', 'INVALID_STATE', 409)
        row['closure'] = dict(reason=p['reason'], actor=actor['id'], at=now(), note='人工关闭，不代表统计收益证明')
    elif p['state'] == 'investigating':
        require(row['state'] == 'closed', '仅已关闭专题可以重开', 'INVALID_STATE', 409)
    else:
        require(row['state'] == 'under_review' and not any(m['decision'] == 'accepted' for m in row['members']), '有已接受证据的专题不能直接拒绝')
    row['state'] = p['state']
    return save(store, db, 'issues', row, actor, 'issue_state_changed')


def create_task(store, db, actor, p):
    svc.role(actor, {'supervisor'})
    member = task_member(store, db, p['owner'])
    issue = store.get(db, 'issues', p['issue_id'])
    require(issue['state'] in {'investigating', 'action_planned'}, '专题未确认反馈或已结束', 'INVALID_STATE', 409)
    if p['cause_id']:
        cause = store.get(db, 'hypotheses', p['cause_id'])
        require(cause['issue_id'] == issue['id'], '原因必须属于当前专题')
        require(cause['state'] != 'rejected', '不能绑定已否定的原因')
    row = dict(id=uid('TASK'), version=1, state='draft', source_type='local', created_at=now(), created_by=actor['id'],
               **{**p,'owner':member['label'],'owner_id':member['id']}, sku=issue['sku'], region=issue['region'], channel=issue['channel'],
               content_draft=None, deliverable=None, revision_id=None, effective_at=None, acceptance=None)
    store.put(db, 'tasks', row['id'], row)
    store.audit(db, actor, 'task_created', row['id'])
    return row


TASK_TRANSITIONS = {'draft': {'accepted', 'cancelled'}, 'accepted': {'in_progress', 'blocked', 'cancelled'},
                    'in_progress': {'blocked', 'cancelled'}, 'blocked': {'in_progress', 'cancelled'},
                    'delivered': {'reopened'}, 'observing': {'done', 'reopened'}, 'done': {'reopened'},
                    'reopened': {'in_progress', 'cancelled'}}


def change_task(store, db, actor, key, p):
    managerial = p['state'] in {'cancelled','reopened','done'}
    if managerial:
        svc.role(actor, {'supervisor'})
    row = task_for(store, db, actor, key, p['version'], owner_only=not managerial)
    target = p['state']
    require(target in TASK_TRANSITIONS.get(row['state'], set()), '不允许此任务状态转换', 'INVALID_STATE', 409)
    if target == 'accepted':
        require(row['owner'].strip(), '接受任务前必须有负责人')
        issue = store.get(db, 'issues', row['issue_id'])
        issue['state'] = 'action_planned'
        save(store, db, 'issues', issue, actor, 'issue_action_planned')
    if target == 'done':
        require(p['reason'].strip(), '验收必须记录结果')
        observations = [o for o in store.all(db, 'observations') if o['task_id'] == key and o['effective_at'] == row['effective_at']]
        require(observations, '验收前记录观察；效果可为数据不足')
        row['acceptance'] = dict(actor=actor['id'], reason=p['reason'], at=now(), observation_id=observations[-1]['id'])
    if target == 'reopened':
        row.setdefault('cycles', []).append(dict(
            ended_at=now(), reason=p['reason'], actor=actor['id'],
            snapshot={k:deepcopy(row.get(k)) for k in ('state','content_draft','deliverable','revision_id','effective_at','effective_evidence','acceptance')},
            observation_ids=[o['id'] for o in store.all(db, 'observations') if o['task_id'] == key and o['effective_at'] == row['effective_at']]))
        row.update(effective_at=None, revision_id=None, acceptance=None, deliverable=None)
        row['content_draft'] = None
        issue = store.get(db, 'issues', row['issue_id'])
        issue['state'] = 'investigating'
        save(store, db, 'issues', issue, actor, 'issue_reopened_with_task')
    row.update(state=target, state_reason=p['reason'])
    return save(store, db, 'tasks', row, actor, 'task_state_changed')


def content_draft(store, db, actor, key, p):
    row = task_for(store, db, actor, key, p['version'], owner_only=True)
    require(row['type'] in {'faq','product_page','manual'}, '流程或调查任务无需生成知识草稿')
    require(row['state'] in {'draft', 'accepted', 'in_progress', 'reopened'}, '此任务不能再编辑交付草稿', 'INVALID_STATE', 409)
    product = store.get(db, 'products', row['sku'])
    source = svc.evidence(store, db, product['source_id'])
    # Quoted source only: no generative specification or unverified selling point.
    proposed = source['text'] + '\n\nlocal product-page clarification / FAQ:\n' + p['text']
    require(p['text'].strip() and p['text'] in source['text'], '自动草稿只能重排已发布来源原文，新增事实须走知识审核')
    row['content_draft'] = dict(type=row['type'], before=source, after_text=proposed, quote=p['text'],
                                product_version=product['version'], generated_by='source_quote_template', human_review='required')
    return save(store, db, 'tasks', row, actor, 'task_content_drafted')


def deliver(store, db, actor, key, p):
    row = task_for(store, db, actor, key, p['version'], owner_only=True)
    require(row['state'] == 'in_progress', '任务须先接受并开始', 'INVALID_STATE', 409)
    row.update(state='delivered', deliverable=dict(reference=p['reference'], result=p['result'], actor=actor['id'], at=now()))
    return save(store, db, 'tasks', row, actor, 'task_delivered')


def activate(store, db, actor, key, p):
    row = editable(store, db, 'tasks', key, p['version'], actor)
    require(row['state'] == 'delivered', '先交付任务再记录生效', 'INVALID_STATE', 409)
    if row['type'] in {'faq', 'product_page', 'manual'}:
        require(p['revision_id'], '内容任务必须关联知识发布')
        rev = store.get(db, 'knowledge_revisions', p['revision_id'])
        require(rev['state'] == 'published' and rev['kind'] == 'products' and rev['object_id'] == row['sku'], '需要当前 SKU 已发布的商品知识版本')
        require(rev.get('task_id') == key, '发布版本必须在创建时绑定此任务')
        require(store.get(db, 'products', row['sku'])['version'] == rev['id'], '该发布已被替代或回滚', 'STALE_DATA', 409)
        require(timestamp(p['effective_at']) >= timestamp(rev['published_at']), '生效时间不能早于发布')
    else:
        require(len(p['evidence'].strip()) >= 10, '流程/质量调查生效需执行证据')
        require(not p['revision_id'], '流程或调查任务不接受无关知识版本')
    require(timestamp(p['effective_at']) >= timestamp(row['deliverable']['at']), '生效时间不能早于任务交付')
    require(timestamp(p['effective_at']) <= timestamp(now()), '不能将未来计划记作实际生效')
    row.update(state='observing', effective_at=p['effective_at'], revision_id=p['revision_id'], effective_evidence=p['evidence'])
    issue = store.get(db, 'issues', row['issue_id'])
    issue['state'] = 'observing'
    save(store, db, 'issues', issue, actor, 'issue_observing')
    return save(store, db, 'tasks', row, actor, 'task_effective')


def observe(store, db, actor, key, p):
    task = editable(store, db, 'tasks', key, p['version'], actor)
    require(task['state'] in {'observing', 'done'}, '任务尚未实际生效', 'INVALID_STATE', 409)
    start, end, as_of = (timestamp(p[k]) for k in ('start_at', 'end_at', 'as_of'))
    require(timestamp(task['effective_at']) <= start < end, '观察窗口须在生效后且结束晚于开始')
    require(start <= as_of, '参考时间不能早于观察开始')
    require(as_of <= timestamp(now()), '观察参考时间不能在未来')
    issue = store.get(db, 'issues', task['issue_id'])
    cohort = [t for t in store.all(db, 'tickets') if t['source_type'] == 'local' and
              all(t.get(k) == issue.get(k) for k in ('sku', 'device_id', 'use_case', 'region', 'channel')) and
              start <= timestamp(t['created_at']) < min(end, as_of)]
    ticket_ids = {t['id'] for t in cohort}
    matching = [f for f in store.all(db, 'feedback') if f['state'] == 'accepted' and f['ticket_id'] in ticket_ids
                and scope(f) == scope(issue) and start <= timestamp(f['observed_at']) < min(end, as_of)]
    adequate = as_of >= end and len(ticket_ids) >= 5 and p['coverage_complete']
    revision_current = not task['revision_id'] or store.get(db, 'products', task['sku'])['version'] == task['revision_id']
    adequate = adequate and revision_current
    if p['conclusion'] != 'insufficient_data':
        require(adequate, '窗口未成熟、少于5条工单、覆盖不完整或版本已替代，只能记录数据不足')
    row = dict(id=uid('OBS'), version=1, task_id=key, source_type='local', effective_at=task['effective_at'],
               **{k:v for k,v in p.items() if k != 'version'}, cohort_ticket_ids=sorted(ticket_ids), related_ticket_ids=sorted({f['ticket_id'] for f in matching}),
               cohort_count=len(ticket_ids), related_count=len({f['ticket_id'] for f in matching}), rates=None,
               sales_denominator=None, return_window_mature=None, adequate=adequate, revision_current=revision_current,
               reviewer=actor['id'], observed_at=now(), note='工单覆盖计数，非销量/退货分母；结论为人工定性观察，不能证明因果收益')
    row['task_version'] = task['version']
    row['revision_id'] = task['revision_id']
    row['evidence_snapshots'] = deepcopy(matching)
    store.put(db, 'observations', row['id'], row)
    save(store, db, 'tasks', task, actor, 'task_observation_added')
    return row
