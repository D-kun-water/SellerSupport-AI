"""Reviewed offline datasets and bad-case promotion, independent from model judges."""
from copy import deepcopy
from . import services as svc
from .assist import assist
from .store import ROOT, uid
from .domain import now, DomainError
from .knowledge import digest
from .improvement import require, save


def reviewed_cases(store, db):
    return [c for c in store.all(db, 'eval_cases') if c['state'] == 'approved']


def dataset_hash(store, db):
    return digest(reviewed_cases(store, db))


def apply_case_fixtures(store, db, case):
    for fixture in case.get('fixtures', []):
        require(fixture['table'] in {'orders','products','policies','sources'}, '不允许此案例夹具类型')
        if fixture.get('current_source_for_sku'):
            require(fixture['table']=='sources', '当前来源夹具只能作用于来源表')
            product=store.get(db,'products',fixture['current_source_for_sku'])
            require(set(fixture['changes']) <= {'status'}, '当前来源夹具只允许修改可用状态')
            versions=svc.active(store,db,'products',product['region'],case['as_of'],sku=product['sku'])
            # Historical cases must affect the version effective at their own time,
            # as well as the current canonical record. Each case rolls back below.
            for source_id in {p['source_id'] for p in versions}|{product['source_id']}:
                obj=deepcopy(store.get(db,'sources',source_id))
                require(obj['source_type']=='local', '案例夹具只能使用合成数据')
                obj.update(fixture['changes'])
                store.put(db,'sources',obj['id'],obj)
            continue
        else:
            obj=fixture['object']
            if fixture['table'] in {'products','policies'}:
                # The Chinese catalog archives the previous canonical record under
                # this alias. A full replacement fixture replaces that identity's
                # history too, without suppressing unrelated conflict fixtures.
                prefix='HIST-ZH-'+obj['id']+'-'
                for old in store.all(db,fixture['table']):
                    if old['id'].startswith(prefix):
                        old.update(review_status='draft')
                        store.put(db,fixture['table'],old['id'],old)
        require(obj['source_type']=='local', '案例夹具只能使用合成数据')
        store.put(db,fixture['table'],obj['id'],obj)


def evaluate_cases(store, db, cases):
    results = []
    db.execute('SAVEPOINT evaluation_cases')
    try:
        for c in cases:
            try:
                actor = dict(id='offline-evaluator', **c.get('actor_scope', dict(role='supervisor',orders=['TEST-O1','TEST-O2','TEST-O3','TEST-O4'])))
                # Each scenario starts clean, so one case cannot change another's fixture.
                db.execute('SAVEPOINT evaluation_case_fixture')
                apply_case_fixtures(store,db,c)
                ticket = svc.create_ticket(store, db, actor, c['ticket'])
                ticket['clarification_rounds']=c.get('prior_clarification_rounds',0)
                store.put(db,'tickets',ticket['id'],ticket)
                for update in c.get('follow_ups', []):
                    ticket=svc.change_ticket(store,db,actor,ticket['id'],dict(version=ticket['version'],**update))
                run = assist(store, db, actor, ticket['id'], dict(version=ticket['version'], as_of=c['as_of']), persist=False)
                checks = c['expected']
                issues = []
                if checks.get('error_code'):
                    issues.append('EXPECTED_ERROR_NOT_RAISED')
                if run['status'] != checks['status']:
                    issues.append('WRONG_STATUS')
                if any(fragment not in run['reply_draft'] for fragment in checks['required_fragments']):
                    issues.append('MISSING_REQUIRED_FACT')
                if any(fragment.casefold() in run['reply_draft'].casefold() for fragment in checks['forbidden_fragments']):
                    issues.append('FORBIDDEN_PROMISE')
                results.append(dict(case_id=c['id'], split=c['split'], status='FAIL' if issues else 'PASS', issues=issues,
                                    actual_status=run['status'], reply_draft=run['reply_draft'], evidence_refs=run['evidence_refs']))
            except DomainError as exc:
                expected_error=c['expected'].get('error_code')
                results.append(dict(case_id=c['id'], split=c['split'], status='PASS' if expected_error==exc.code else 'ERROR', issues=[] if expected_error==exc.code else [exc.code], observed_error=exc.code))
            finally:
                db.execute('ROLLBACK TO evaluation_case_fixture')
                db.execute('RELEASE evaluation_case_fixture')
    finally:
        db.execute('ROLLBACK TO evaluation_cases')
        db.execute('RELEASE evaluation_cases')
    return results


def bad_case(store, db, actor, p):
    ticket = svc.ticket_for(store, db, p['ticket_id'], actor)
    run = store.get(db, 'runs', p['run_id'])
    require(run['ticket_id'] == ticket['id'], '运行必须属于关联工单')
    require(run.get('ticket_snapshot') or run['context_version'] == ticket['context_version'], '旧运行缺少上下文快照，不能用新事实替代历史', 'STALE_DATA', 409)
    basis = run.get('ticket_snapshot', ticket)
    evidence = run['evidence_refs'] or [dict(id='SPEC-BR-02',version='v1.2',source_type='local',
                 locator='docs/产品说明.md',text='缺少准确型号与用途须澄清；缺少或冲突的依据交人工；不得补造商品事实或执行状态。',
                 evidence_type='behavior_specification_not_product_fact')]
    for row in store.all(db, 'eval_cases'):
        if row.get('run_id') == p['run_id'] and row.get('correction') == p['correction']:
            return row
    row = dict(id=uid('CASE'), version=1, state='pending', source_type='local',
               ticket_id=ticket['id'], run_id=run['id'], run_snapshot=deepcopy(run),
               ticket={k:basis.get(k) for k in ('source_type','channel','region','sku','device_id','use_case','order_id')} |
                      {'message':next((m['text'] for m in reversed(basis['messages']) if m['role']=='customer'),basis['messages'][-1]['text'])},
               as_of=run['as_of'], error_type=p['error_type'], correction=p['correction'],
               evidence_refs=evidence, actor_scope=run.get('actor_scope', dict(role=actor['role'],orders=actor['orders'])),
               fixtures=([dict(table='orders',object=run['order_snapshot'])] if run.get('order_snapshot') else []),
               prior_clarification_rounds=run.get('prior_clarification_rounds',0),
               expected=p['expected'], split='regression',
               nominated_by=actor['id'], created_at=now(), reviewer=None)
    store.put(db, 'eval_cases', row['id'], row)
    store.audit(db, actor, 'bad_case_nominated_not_gold', row['id'])
    return row


def review_case(store, db, actor, key, p):
    svc.role(actor, {'supervisor'})
    row = store.get(db, 'eval_cases', key)
    svc.version(row, p['version'])
    require(row['state'] == 'pending', '案例已审核；修改需另建候选', 'INVALID_STATE', 409)
    require(row['evidence_refs'], '审核需保留来源依据')
    require(p['decision'] != 'approve' or p['expected']['required_fragments'] or p['expected']['forbidden_fragments'] or p['expected'].get('error_code'),
            '金标必须有事实或禁止承诺断言，不能只检查出口状态')
    if row.get('supersedes') and p['decision'] == 'approve':
        previous = store.get(db,'eval_cases',row['supersedes'])
        require(digest(previous) == row['parent_hash'], '原金标已变化，需重新创建修订候选', 'VERSION_CONFLICT',409)
        previous.update(state='superseded', superseded_by=key)
        save(store,db,'eval_cases',previous,actor,'eval_case_superseded')
    # Reviewer can correct assertions, but cannot overwrite original run/source snapshots.
    row.update(state='approved' if p['decision'] == 'approve' else 'rejected', expected=p['expected'],
               review_reason=p['reason'], reviewer=actor['id'], reviewed_at=now())
    return save(store, db, 'eval_cases', row, actor, 'eval_case_reviewed')


def revise_case(store, db, actor, key, p):
    svc.role(actor, {'supervisor'})
    previous=store.get(db,'eval_cases',key)
    svc.version(previous,p['version'])
    require(previous['state'] in {'approved','rejected'}, '仅已审核案例可以创建修订', 'INVALID_STATE',409)
    require(not any(c.get('supersedes')==key and c['state']=='pending' for c in store.all(db,'eval_cases')),
            '已有待审核修订，先审核该候选', 'VERSION_CONFLICT',409)
    row=deepcopy(previous)
    for field in ('review_reason','reviewed_at','updated_at','superseded_by','parent_hash'):
        row.pop(field,None)
    row.update(id=uid('CASE'),version=1,state='pending',supersedes=key,parent_hash=digest(previous),
               expected=p['expected'],revision_reason=p['reason'],reviewer=None,created_at=now(),nominated_by=actor['id'])
    store.put(db,'eval_cases',row['id'],row)
    store.audit(db,actor,'eval_case_revision_pending',row['id'],{'supersedes':key})
    return row


def run_dataset(store, db, actor):
    svc.role(actor, {'supervisor'})
    from .knowledge import fingerprint, checker_hash
    cases = reviewed_cases(store, db)
    require(cases, '还没有人工审核通过的案例')
    results = evaluate_cases(store, db, cases)
    row = dict(id=uid('EVAL'), source_type='local', observed_at=now(), actor=actor['id'],
               kind='reviewed_offline_rules', case_ids=[c['id'] for c in cases], cases_snapshot=cases,
               results=results, planned=len(cases), passed=sum(r['status'] == 'PASS' for r in results),
               failed=sum(r['status'] == 'FAIL' for r in results), errors=sum(r['status'] == 'ERROR' for r in results),
               dataset_hash=dataset_hash(store, db), baseline_hash=fingerprint(store, db), checker_hash=checker_hash(),
               llm_calls=0, note='离线规则和事实断言结果，不替代真实模型或商家试点')
    store.put(db, 'eval_runs', row['id'], row)
    store.audit(db, actor, 'reviewed_dataset_evaluated', row['id'], {'passed':row['passed'], 'planned':row['planned']})
    return row


def seed_dataset(store, db, actor):
    svc.role(actor, {'supervisor'})
    import json
    bundle = json.loads((ROOT/'tests/fixtures/evaluation-cases.json').read_text(encoding='utf-8'))
    inserted = 0
    for row in bundle['cases']:
        if db.execute('SELECT 1 FROM eval_cases WHERE id=?', (row['id'],)).fetchone():
            continue
        store.put(db, 'eval_cases', row['id'], row)
        inserted += 1
    store.audit(db, actor, 'pending_dataset_imported', bundle['dataset_version'], {'inserted':inserted})
    return dict(inserted=inserted, total=len(bundle['cases']), review_status='pending', independent_scenarios=20,
                note='100条为20个基础场景的受控措辞变体；同场景不跨数据集。未自动成为人工金标。')


def seed_complex_dataset(store, db, actor):
    svc.role(actor,{'supervisor'})
    import json
    bundle=json.loads((ROOT/'tests/fixtures/edge-cases.json').read_text(encoding='utf-8'))
    inserted=0
    for row in bundle['cases']:
        if not db.execute('SELECT 1 FROM eval_cases WHERE id=?',(row['id'],)).fetchone():
            store.put(db,'eval_cases',row['id'],row)
            inserted+=1
    store.audit(db,actor,'complex_dataset_pending',bundle['dataset_version'],{'inserted':inserted})
    return dict(inserted=inserted,total=len(bundle['cases']),independent_scenarios=len(bundle['cases']),human_review='pending')
