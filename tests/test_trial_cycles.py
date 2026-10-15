"""Artificial replay cycles must not borrow prior samples or adopted replies."""
from tests.test_trial import client,published,case,reviewed,stage,header
from tests.test_trial_records import observe,plans


def test_restart_does_not_reuse_prior_stage_samples(client):
    c=client;published(c);reviewed(c,case(c));stage(c,'shadow');stage(c,'paused');stage(c,'replay')
    workspace=c.get('/api/s3/workspace',headers=c.sup).json()['data']
    r=c.post('/api/s3/stage',headers=c.sup,json=dict(version=workspace['session']['version'],stage='shadow',reason='Artificial new cycle review'))
    assert r.status_code==409,'Old replay samples incorrectly passed a new cycle gate'


def test_restart_cannot_copy_or_adopt_prior_cycle_reply(client):
    c=client;published(c);reviewed(c,case(c));stage(c,'shadow');reviewed(c,case(c,'SHADOW'));stage(c,'assisted')
    old=reviewed(c,case(c,'OLD-ASSISTED'))
    copied=c.post('/api/s3/cases/'+old['id']+'/copy',headers=c.agent,json=dict(version=old['version']));assert copied.status_code==200
    old=copied.json()['data']['case']
    stage(c,'paused');stage(c,'replay');reviewed(c,case(c,'NEW-REPLAY'));stage(c,'shadow');reviewed(c,case(c,'NEW-SHADOW'));stage(c,'assisted')
    assert c.post('/api/s3/cases/'+old['id']+'/copy',headers=c.agent,json=dict(version=old['version'])).status_code==409
    assert observe(c,old,adopted=True,original_channel_receipt='SAMPLE RECEIPT').status_code==409


def test_historical_summary_has_metrics_without_originals(client):
    c=client;published(c);row=reviewed(c,case(c));observe(c,row)
    old=plans(c)[0];cfg=c.get('/api/pilot',headers=c.sup).json()['data']['control']
    c.put('/api/pilot/plan',headers=c.sup,json=dict(c.plan,version=cfg['version'],authorization_reference='NEW-SAMPLE-AUTH'))
    r=c.get('/api/s3/plans/'+old['id']+'/summary',headers=c.sup);assert r.status_code==200,r.text
    data=r.json()['data'];assert data['metrics']['net_saved_seconds']==55
    assert data['stages'][0]['metrics']['measured_cases']==1
    assert 'private-fixture@example.com' not in r.text and 'source_text' not in r.text and 'original_channel_receipt' not in r.text
    assert c.get('/api/s3/plans/'+old['id']+'/summary',headers=c.agent).status_code==403
    if c.mode=='merchant_authorized':assert c.get('/api/s3/plans/'+old['id']+'/summary',headers=header('outsider')).status_code==403


def test_revoked_plan_summary_remains_metadata_only_and_purge_removes_metrics(client):
    c=client;published(c);row=reviewed(c,case(c));observe(c,row);p=plans(c)[0]
    assert c.post('/api/s3/plans/'+p['id']+'/revoke',headers=c.sup,json=dict(version=1)).status_code==200
    r=c.get('/api/s3/plans/'+p['id']+'/summary',headers=c.sup);assert r.status_code==200
    assert r.json()['data']['revoked'] and r.json()['data']['metrics']['case_count']==1
    assert c.get('/api/s3/workspace',headers=c.sup).status_code==403
    body=dict(confirm_plan_id=p['id'],reason='authorization_revoked',plan_version=2,dry_run=True)
    preview=c.post('/api/s3/purge',headers=c.sup,json=body).json()['data']
    assert c.post('/api/s3/purge',headers=c.sup,json=dict(body,dry_run=False,preview_token=preview['preview_token'])).status_code==200
    data=c.get('/api/s3/plans/'+p['id']+'/summary',headers=c.sup).json()['data']
    assert data['metrics']['case_count']==data['metrics']['measured_cases']==0 and data['stages']==[]


def test_legacy_cycle_migration_is_time_based_and_idempotent(client):
    from app.store import Store
    c=client;published(c);old=reviewed(c,case(c));stage(c,'shadow');stage(c,'paused');stage(c,'replay');new=case(c,'NEW-CYCLE')
    store=c.app.state.store
    with store.transaction() as db:
        for table in ('s3_cases','s3_sessions'):
            for row in store.all(db,table):
                row.pop('cycle',None);store.put(db,table,row['id'],row)
    Store(store.path);Store(store.path)
    with store.transaction() as db:
        assert store.get(db,'s3_cases',old['id'])['cycle']==0
        assert store.get(db,'s3_cases',new['id'])['cycle']==1
        assert store.all(db,'s3_sessions')[0]['cycle']==1
