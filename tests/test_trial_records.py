"""Measurements and historical authorization tests use only sample fixtures."""
from datetime import timedelta
from tests.test_trial import client,published,case,reviewed,stage,header
from app.domain import now,timestamp


def observe(c,row,**changes):
    body=dict(version=row['version'],manual_seconds=90,wait_seconds=5,review_seconds=10,rework_seconds=5,maintenance_seconds=15,baseline_error=False,assisted_error=False,adopted=False)
    body.update(changes)
    return c.post('/api/s3/cases/'+row['id']+'/observation',headers=c.sup,json=body)


def plans(c):return c.get('/api/s3/plans',headers=c.sup).json()['data']


def test_measurement_freeze_math_and_invalid_values(client):
    c=client;published(c);row=reviewed(c,case(c))
    r=observe(c,row);assert r.status_code==200,r.text
    assert observe(c,row).json()['data']['id']==r.json()['data']['id']
    assert observe(c,row,manual_seconds=91).status_code==409
    m=c.get('/api/s3/workspace',headers=c.sup).json()['data']['metrics']
    assert (m['manual_seconds'],m['assisted_seconds'],m['net_saved_seconds'])==(90,35,55)
    other=reviewed(c,case(c,'OTHER'))
    assert observe(c,other,manual_seconds=-1).status_code==422
    assert observe(c,other,assisted_error=True).status_code==422
    assert observe(c,other,adopted=True,original_channel_receipt='SAMPLE RECEIPT').status_code==409
    assert observe(c,other,original_channel_receipt='UNADOPTED').status_code==422


def test_missing_measurements_block_stage(client):
    c=client;published(c);row=reviewed(c,case(c))
    r=c.post('/api/s3/stage',headers=c.sup,json=dict(version=0,stage='shadow',reason='Sample review'))
    assert r.status_code==409 and r.json()['errors'][0]['code']=='OBSERVATION_GATE_FAILED'
    assert observe(c,row).status_code==200
    assert c.post('/api/s3/stage',headers=c.sup,json=dict(version=0,stage='shadow',reason='Sample review')).status_code==200


def test_negative_value_can_stop_but_not_continue(client):
    c=client;published(c);reviewed(c,case(c));stage(c,'shadow');reviewed(c,case(c,'SHADOW'));stage(c,'assisted')
    row=reviewed(c,case(c,'ASSISTED'));assert observe(c,row,manual_seconds=1).status_code==200
    body=dict(version=2,stage='completed',reason='Sample closing review')
    assert c.post('/api/s3/stage',headers=c.sup,json=body).status_code==422
    r=c.post('/api/s3/stage',headers=c.sup,json=dict(body,decision='continue'))
    assert r.status_code==409 and r.json()['errors'][0]['code']=='VALUE_GATE_FAILED'
    r=c.post('/api/s3/stage',headers=c.sup,json=dict(body,decision='stop'));assert r.status_code==200,r.text
    assert r.json()['data']['closing_review']['metrics']['net_saved_seconds']==-34


def test_failed_case_allows_adjustment(client):
    c=client;published(c);reviewed(c,case(c));stage(c,'shadow');reviewed(c,case(c,'SHADOW'));stage(c,'assisted')
    row=reviewed(c,case(c,'FAILED'),'fail');assert observe(c,row,assisted_error=True).status_code==200
    r=c.post('/api/s3/stage',headers=c.sup,json=dict(version=2,stage='completed',reason='Sample quality problem',decision='adjust'))
    assert r.status_code==200 and r.json()['data']['closing_review']['decision']=='adjust'


def test_historical_plan_retention_preview_and_revoke(client):
    c=client;published(c);row=reviewed(c,case(c));assert observe(c,row).status_code==200
    old=plans(c)[0];cfg=c.get('/api/pilot',headers=c.sup).json()['data']['control']
    assert c.put('/api/pilot/plan',headers=c.sup,json=dict(c.plan,version=cfg['version'],retention_days=1,authorization_reference='NEW-SAMPLE-AUTH')).status_code==200
    histories=plans(c);assert len(histories)==2
    assert next(p for p in histories if p['id']==old['id'])['plan']['retention_days']==30
    payload=dict(confirm_plan_id=old['id'],plan_version=old['version'],reason='provider_requested',dry_run=True)
    preview=c.post('/api/s3/purge',headers=c.sup,json=payload).json()['data'];assert preview['selected']==3
    assert c.post('/api/s3/purge',headers=c.sup,json=dict(payload,dry_run=False)).status_code==409
    assert c.post('/api/s3/plans/'+old['id']+'/revoke',headers=c.sup,json=dict(version=old['version'])).status_code==200
    assert c.post('/api/s3/purge',headers=c.sup,json=dict(payload,dry_run=False,preview_token=preview['preview_token'])).status_code==409
    cfg=c.get('/api/pilot',headers=c.sup).json()['data']['control'];assert not cfg['revoked']
    payload.update(plan_version=2,reason='authorization_revoked')
    preview=c.post('/api/s3/purge',headers=c.sup,json=payload).json()['data']
    assert c.post('/api/s3/purge',headers=c.sup,json=dict(payload,dry_run=False,preview_token=preview['preview_token'])).status_code==200
    with c.app.state.store.transaction() as db:
        tomb=c.app.state.store.get(db,'s3_cases',row['id']);assert tomb['state']=='deleted' and 'message' not in tomb


def test_expired_source_cascades_and_preview_changes(client):
    c=client;src=published(c);row=reviewed(c,case(c));assert observe(c,row).status_code==200
    with c.app.state.store.transaction() as db:
        src=c.app.state.store.get(db,'s3_sources',src['id']);src['created_at']=(timestamp(now())-timedelta(days=31)).isoformat();c.app.state.store.put(db,'s3_sources',src['id'],src)
    p=plans(c)[0];body=dict(confirm_plan_id=p['id'],plan_version=p['version'],reason='retention_expired',dry_run=True)
    preview=c.post('/api/s3/purge',headers=c.sup,json=body).json()['data']
    assert preview['counts']==dict(s3_sources=1,s3_cases=1,s3_observations=1)
    second=case(c,'NEW-COPY')
    assert c.post('/api/s3/purge',headers=c.sup,json=dict(body,dry_run=False,preview_token=preview['preview_token'])).status_code==409
    preview=c.post('/api/s3/purge',headers=c.sup,json=body).json()['data'];assert preview['selected']==4
    assert c.post('/api/s3/purge',headers=c.sup,json=dict(body,dry_run=False,preview_token=preview['preview_token'])).status_code==200
    with c.app.state.store.transaction() as db:
        assert 'evidence_snapshot' not in c.app.state.store.get(db,'s3_cases',second['id'])


def test_history_permissions_and_legacy_exports(client):
    c=client;published(c);row=reviewed(c,case(c));observe(c,row)
    assert c.get('/api/s3/plans',headers=c.agent).status_code==403
    if c.mode=='merchant_authorized':
        assert c.get('/api/s3/plans',headers=header('outsider')).json()['data']==[]
        p=plans(c)[0]
        assert c.post('/api/s3/purge',headers=header('outsider'),json=dict(confirm_plan_id=p['id'],reason='provider_requested')).status_code==403
    export=c.get('/api/export',headers=c.sup).json()['data'];assert export['s3_plans']==export['s3_observations']==[]


def test_shadow_duration_enforced(client):
    c=client;cfg=c.get('/api/pilot',headers=c.sup).json()['data']['control']
    assert c.put('/api/pilot/plan',headers=c.sup,json=dict(c.plan,version=cfg['version'],minimum_shadow_days=1)).status_code==200
    published(c);reviewed(c,case(c));stage(c,'shadow');reviewed(c,case(c,'SHADOW'));stage(c,'assisted',409)


def test_continue_is_only_sample_review_and_real_requires_adoption(client):
    c=client;published(c);reviewed(c,case(c));stage(c,'shadow');reviewed(c,case(c,'SHADOW'));stage(c,'assisted')
    row=reviewed(c,case(c,'ASSISTED'));assert observe(c,row).status_code==200
    r=c.post('/api/s3/stage',headers=c.sup,json=dict(version=2,stage='completed',reason='Sample sample review',decision='continue'))
    if c.mode=='merchant_authorized':
        assert r.status_code==409 and r.json()['errors'][0]['code']=='VALUE_GATE_FAILED'
    else:
        assert r.status_code==200 and r.json()['data']['closing_review']['real_effect']=='unverified'


def test_schema_upgrade_captures_current_plan_once(client):
    from app.store import Store
    import sqlite3
    c=client;p=plans(c)[0];path=c.app.state.store.path
    with sqlite3.connect(path) as db:
        db.execute('DROP TABLE s3_plans');db.execute('DROP TABLE s3_observations');db.execute('DELETE FROM schema_version WHERE version>=8')
    upgraded=Store(path);upgraded=Store(path)
    with upgraded.transaction() as db:
        rows=upgraded.all(db,'s3_plans');assert len(rows)==1 and rows[0]['id']==p['id'] and rows[0]['plan']==p['plan']
        assert db.execute('SELECT max(version) FROM schema_version').fetchone()[0]==9
