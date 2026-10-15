"""local knowledge releases. Immutable revisions; no human approval is invented."""
import hashlib
import json
from copy import deepcopy
from .store import ROOT, encode, uid
from .domain import DomainError, now
from . import services as svc
from .assist import assist


def digest(value):
    return hashlib.sha256(encode(value).encode()).hexdigest()


def fingerprint(store, db):
    return digest({t: store.all(db, t) for t in ('products', 'sources', 'policies', 'orders')})


def checker_hash():
    return hashlib.sha256(b''.join(p.read_bytes() for p in sorted((ROOT/'app').glob('*.py'))) +
                          (ROOT/'tests/fixtures/cases.json').read_bytes()).hexdigest()


def create_revision(store, db, actor, kind, key, expected_version, changes, source_text, reason, rollback_of=None):
    svc.role(actor, {'supervisor'})
    current = store.get(db, kind, key)
    if kind == 'policies' and current.get('kind') != 'returns':
        raise DomainError('VALIDATION_ERROR', '当前版本编辑仅支持退货政策；补发规则需专用流程')
    if current['version'] != expected_version:
        raise DomainError('VERSION_CONFLICT', '知识版本已变化', 409)
    source = store.get(db, 'sources', current['source_id'])
    if current['source_type'] != 'local' or source['source_type'] != 'local':
        raise DomainError('VALIDATION_ERROR', '本流程只处理本地资料')
    revision_id = uid('KNOW')
    candidate = deepcopy(current)
    candidate.update(changes)
    candidate.update(version=revision_id, source_id='SRC-'+revision_id,
                     review_status='published', validation='local_release_pending_human')
    if kind == 'products':
        for rule in candidate['rules']:
            header = f"Rule for {rule['device_id']}/{rule['use_case']}: {rule['result']}."
            from .catalog_text import chinese_rule_header
            if (header not in source_text and chinese_rule_header(rule) not in source_text) or any(not x.strip() or x not in source_text for x in rule['conditions']):
                raise DomainError('VALIDATION_ERROR', '来源必须包含全部生效规则与条件')
    elif str(candidate['window_days']) not in source_text:
        raise DomainError('VALIDATION_ERROR', '政策来源必须包含退货天数')
    new_source = deepcopy(source)
    new_source.update(id=candidate['source_id'], version=revision_id, text=source_text,
                      checksum=hashlib.sha256(source_text.encode()).hexdigest(), status='published',
                      locator='local/knowledge-revisions/'+revision_id)
    row = dict(id=revision_id, kind=kind, object_id=key, version=1, state='draft',
               source_type='local', created_by=actor['id'], created_at=now(), reason=reason,
               before=current, before_source=source, candidate=candidate, candidate_source=new_source,
               base_hash=digest(current), base_source_hash=digest(source), rollback_of=rollback_of,
               human_review='pending', regression=None)
    store.put(db, 'knowledge_revisions', revision_id, row)
    store.audit(db, actor, 'knowledge_draft_created', revision_id, dict(kind=kind, object_id=key, rollback_of=rollback_of))
    return row


def current_revision(store, db, key, expected_version):
    row = store.get(db, 'knowledge_revisions', key)
    svc.version(row, expected_version)
    if row['state'] != 'draft':
        raise DomainError('INVALID_STATE', '发布记录不可重复发布或编辑', 409)
    if (digest(store.get(db, row['kind'], row['object_id'])) != row['base_hash'] or
            digest(store.get(db, 'sources', row['before']['source_id'])) != row['base_source_hash']):
        raise DomainError('STALE_EVIDENCE', '草稿创建后的对象或来源已变化，请重新创建', 409)
    return row


def regression(store, db, actor, key, expected_version):
    svc.role(actor, {'supervisor'})
    row = current_revision(store, db, key, expected_version)
    baseline_hash = fingerprint(store, db)
    results = []
    from .evaluation import reviewed_cases, dataset_hash, evaluate_cases
    cases_for_release = reviewed_cases(store, db)
    reviewed_hash = dataset_hash(store, db)
    db.execute('SAVEPOINT knowledge_regression')
    try:
        store.put(db, row['kind'], row['object_id'], row['candidate'])
        store.put(db, 'sources', row['candidate_source']['id'], row['candidate_source'])
        cases = json.loads((ROOT/'tests/fixtures/cases.json').read_text(encoding='utf-8'))
        evaluator = dict(id='knowledge-regression', role='supervisor', orders=['TEST-O1','TEST-O2','TEST-O3','TEST-O4'])
        for case in cases:
            for repeat in range(3):
                try:
                    ticket = svc.create_ticket(store, db, evaluator, case['ticket'])
                    run = assist(store, db, evaluator, ticket['id'], case['assist'], persist=False)
                    issues = []
                    if run['status'] != case['expected_status']: issues.append('WRONG_STATUS')
                    text = run['reply_draft']
                    required = {
                        'conditional_match':['verified USB-C PD','does not provide video'],
                        'video_unsupported':['no video capability'],
                        'power_not_guarantee':['30W','actual device draw can be lower'],
                        'boundary_equal':['within','no refund has been approved or executed'],
                        'boundary_plus_second':['outside','no refund has been approved or executed'],
                        'safety':['stop using','do not test'],
                        'safety_multi':['stop using','tracking question remains pending','after the safety review'],
                        'missing_delivery':['exact time','time zone'],
                        'tracking_snapshot':['2026-09-29T12:00:00+00:00','9.17 days','not a live carrier check'],
                    }.get(case['scenario'], [])
                    if any(x not in text for x in required): issues.append('CRITICAL_FACT_LOST')
                    results.append(dict(case_id=case['id'], repeat=repeat+1, status='FAIL' if issues else 'PASS', issues=issues))
                except DomainError as exc:
                    results.append(dict(case_id=case['id'], repeat=repeat+1, status='ERROR', issues=[exc.code]))
        results.extend(evaluate_cases(store, db, cases_for_release))
    finally:
        db.execute('ROLLBACK TO knowledge_regression')
        db.execute('RELEASE knowledge_regression')
    row['regression'] = dict(kind='deterministic_fixed_cases', observed_at=now(), results=results,
                             passed=sum(x['status']=='PASS' for x in results), planned=len(results),
                             baseline_hash=baseline_hash, checker_hash=checker_hash(),
                             candidate_hash=digest([row['candidate'],row['candidate_source']]),
                             reviewed_dataset_hash=reviewed_hash,
                             human_semantics='unreviewed', llm_calls=0)
    row['version'] += 1
    store.put(db, 'knowledge_revisions', key, row)
    store.audit(db, actor, 'knowledge_regression', key, dict(passed=row['regression']['passed'], planned=len(results)))
    return row


def publish(store, db, actor, key, expected_version, reason):
    svc.role(actor, {'supervisor'})
    row = current_revision(store, db, key, expected_version)
    report = row['regression']
    if not report or report['planned'] < 60 or report['passed'] != report['planned']:
        raise DomainError('REGRESSION_FAILED', '必须通过60条固定回归及全部人工审核案例', 409)
    from .evaluation import dataset_hash
    if report.get('reviewed_dataset_hash') != dataset_hash(store, db):
        raise DomainError('STALE_REGRESSION', '审核数据集已变化，请重新回归', 409)
    if (report['baseline_hash'] != fingerprint(store, db) or report['checker_hash'] != checker_hash() or
            report['candidate_hash'] != digest([row['candidate'],row['candidate_source']])):
        raise DomainError('STALE_REGRESSION', '回归之后资料、订单或检查器已变化，请重新回归', 409)
    store.put(db, 'sources', row['candidate_source']['id'], row['candidate_source'])
    store.put(db, row['kind'], row['object_id'], row['candidate'])
    row.update(state='published', published_at=now(), published_by=actor['id'], publish_reason=reason, version=row['version']+1)
    store.put(db, 'knowledge_revisions', key, row)
    store.audit(db, actor, 'knowledge_published', key, dict(object_id=row['object_id'], rollback_of=row['rollback_of'], human_review='pending'))
    return row


def rollback_draft(store, db, actor, key, expected_version, reason):
    svc.role(actor, {'supervisor'})
    old = store.get(db, 'knowledge_revisions', key)
    svc.version(old, expected_version)
    if old['state'] != 'published':
        raise DomainError('INVALID_STATE', '只能对已发布记录创建回滚草稿', 409)
    current = store.get(db, old['kind'], old['object_id'])
    if current['version'] != old['candidate']['version']:
        raise DomainError('VERSION_CONFLICT', '只能回滚当前生效的发布版本', 409)
    changes = {k:v for k,v in old['before'].items() if k not in {'id','version','source_id'}}
    return create_revision(store, db, actor, old['kind'], old['object_id'], current['version'],
                           changes, old['before_source']['text'], reason, rollback_of=key)
