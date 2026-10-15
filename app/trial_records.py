"""Plan authorization snapshots and aggregate paired workload from declared measurements."""
from copy import deepcopy
from .domain import DomainError,now,timestamp


def archive(store,db,cfg):
    if not cfg.get('plan') or not cfg.get('plan_id'):return None
    old=next((r for r in store.all(db,'s3_plans') if r['id']==cfg['plan_id']),None)
    if old:return old
    row=dict(id=cfg['plan_id'],plan=deepcopy(cfg['plan']),version=1,revoked=cfg.get('revoked',False),
        snapshot_captured_at=now(),source_type=cfg['plan']['source_type'])
    store.put(db,'s3_plans',row['id'],row);return row


def permitted(plan,actor,authorization):
    if actor['role']!='supervisor':return False
    if plan['source_type']=='local':return True
    return (not actor['id'].startswith('local-') and len(authorization.removeprefix('Bearer '))>=24
        and actor['id'] in set(plan.get('participant_ids',[]))|{plan['owner_id']})


def plan_for_management(store,db,key,actor,authorization):
    row=store.get(db,'s3_plans',key)
    if not permitted(row['plan'],actor,authorization):raise DomainError('FORBIDDEN','无权管理此计划',403)
    return row


def migrate_cycles(store,db):
    sessions={s['id']:s for s in store.all(db,'s3_sessions')}
    for s in sessions.values():
        if 'cycle' not in s:
            s['cycle']=sum(h.get('from_stage')=='paused' and h.get('to_stage')=='replay' for h in s.get('history',[]))
            store.put(db,'s3_sessions',s['id'],s)
    for c in store.all(db,'s3_cases'):
        if 'cycle' in c or c.get('state')=='deleted':continue
        restarts=[h for h in sessions.get(c['plan_id'],{}).get('history',[]) if h.get('from_stage')=='paused' and h.get('to_stage')=='replay']
        c['cycle']=sum(timestamp(h['at'])<=timestamp(c['created_at']) for h in restarts)
        store.put(db,'s3_cases',c['id'],c)


def metrics(store,db,plan_id,stage=None,cycle=None):
    cases=[c for c in store.all(db,'s3_cases') if c['plan_id']==plan_id and c.get('state')!='deleted' and (stage is None or c['stage']==stage) and (cycle is None or c.get('cycle',0)==cycle)]
    ids={c['id'] for c in cases}
    rows=[r for r in store.all(db,'s3_observations') if r['plan_id']==plan_id and r.get('state')!='deleted' and r['case_id'] in ids]
    baseline=sum(r['manual_seconds'] for r in rows)
    assisted=sum(sum(r[k] for k in ('wait_seconds','review_seconds','rework_seconds','maintenance_seconds')) for r in rows)
    previous=sum(r['baseline_error'] for r in rows);errors=sum(r['assisted_error'] for r in rows)
    uncertain=sum(c.get('review',{}).get('decision')=='uncertain' for c in cases if c.get('review'))
    source=rows[0]['source_type'] if rows else (cases[0]['source_type'] if cases else 'unknown')
    return dict(case_count=len(cases),measured_cases=len(rows),missing_measurements=len(cases)-len(rows),
        manual_seconds=baseline,assisted_seconds=assisted,net_saved_seconds=baseline-assisted,
        net_saved_ratio=(baseline-assisted)/baseline if baseline else None,baseline_errors=previous,assisted_errors=errors,
        semantic_uncertain=uncertain,adopted_cases=sum(r['adopted'] for r in rows),source_type=source,
        reviewed_cases=sum(bool(c.get('review')) for c in cases),passed_cases=sum(c.get('review',{}).get('decision')=='pass' for c in cases if c.get('review')),
        decision='no_real_merchant_conclusion' if source=='local' else 'collect_measurements' if not rows or len(rows)<len(cases)
            else 'fix_quality' if errors>previous or uncertain else 'fix_workload' if baseline-assisted<=0 else 'review_observed_value',
        scope='逐案例配对耗时，包含等待/审核/返工/维护；原渠道采纳与耗时均为提交者声明，不证明因果或发送真实性')


def summary(store,db,archived):
    key=archived['id'];cases=[c for c in store.all(db,'s3_cases') if c['plan_id']==key and c.get('state')!='deleted']
    stages=[]
    for cycle in sorted({c.get('cycle',0) for c in cases}):
        for stage in ('replay','shadow','assisted'):
            m=metrics(store,db,key,stage,cycle)
            if m['case_count']:stages.append(dict(cycle=cycle,stage=stage,metrics=m))
    s=next((s for s in store.all(db,'s3_sessions') if s['id']==key),{})
    closing=s.get('closing_review')
    # Summaries intentionally omit all free text, original-channel locators and person identifiers.
    return dict(plan_id=key,source_type=archived['source_type'],revoked=archived['revoked'],observed_at=now(),
        state=s.get('stage','replay'),cycle=s.get('cycle',0),metrics=metrics(store,db,key),stages=stages,
        closing_review=dict(decision=closing['decision'],at=closing['at'],metrics=closing['metrics'],
            evidence_complete=closing.get('evidence_complete'),evidence_current=closing.get('evidence_current'),
            closed_from=closing.get('closed_from'),measured_stage=closing.get('measured_stage'),cycle=closing.get('cycle')) if closing else None,
        real_merchant_effect='unverified',scope='仅存留记录的汇总复盘；采纳与耗时为人员声明。已清理记录不计入，不证明真实试点完成。')
