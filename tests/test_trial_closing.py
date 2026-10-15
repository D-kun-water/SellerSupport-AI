"""Early termination and invalid evidence tests use artificial fixtures only."""
from tests.test_trial import client,published,case,reviewed,stage
from tests.test_trial_records import observe,plans


def test_stop_without_samples_from_replay(client):
    c=client
    r=c.post('/api/s3/stage',headers=c.sup,json=dict(version=0,stage='completed',decision='stop',reason='Artificial no data, stop'))
    assert r.status_code==200,r.text
    closing=r.json()['data']['closing_review'];assert closing['decision']=='stop' and not closing['evidence_complete']
    assert closing['metrics']['case_count']==0


def test_paused_unmeasured_trial_can_adjust_and_close(client):
    c=client;published(c);row=case(c);stage(c,'paused')
    r=c.post('/api/s3/stage',headers=c.sup,json=dict(version=1,stage='completed',decision='adjust',reason='Artificial missing review and measures'))
    assert r.status_code==200,r.text
    closing=r.json()['data']['closing_review'];assert closing['closed_from']=='paused' and closing['measured_stage']=='replay'
    assert closing['metrics']['case_count']==1 and closing['metrics']['missing_measurements']==1


def test_revoked_can_close_metadata_but_not_continue_or_read_originals(client):
    c=client;published(c);case(c);p=plans(c)[0]
    assert c.post('/api/s3/plans/'+p['id']+'/revoke',headers=c.sup,json=dict(version=1)).status_code==200
    body=dict(version=2,session_version=0,decision='stop',reason='Artificial revoked, stop')
    r=c.post('/api/s3/plans/'+p['id']+'/close',headers=c.sup,json=body);assert r.status_code==200,r.text
    assert 'private-fixture@example.com' not in r.text and 'source_text' not in r.text
    assert c.get('/api/s3/workspace',headers=c.sup).status_code==403
    assert c.post('/api/s3/plans/'+p['id']+'/close',headers=c.sup,json=dict(body,decision='continue')).status_code==422
    assert c.post('/api/s3/plans/'+p['id']+'/close',headers=c.sup,json=body).status_code==409


def test_close_requires_supervisor_and_matching_versions(client):
    c=client;p=plans(c)[0];path='/api/s3/plans/'+p['id']+'/close'
    body=dict(version=1,session_version=0,decision='stop',reason='Artificial metadata stop')
    assert c.post(path,headers=c.agent,json=body).status_code==403
    assert c.post(path,headers=c.sup,json=dict(body,session_version=5)).status_code==409
    assert c.post(path,headers=c.sup,json=dict(body,version=5)).status_code==409
    assert c.post(path,headers=c.sup,json=body).status_code==200
    assert c.post('/api/s3/cases',headers=c.agent,json=dict(source_type=c.mode,external_id='STOPPED',message='Artificial',kind='compatibility',sku='FIXTURE-CABLE',region='US',as_of=c.as_of)).status_code==409


def test_early_continue_is_forbidden(client):
    c=client
    assert c.post('/api/s3/stage',headers=c.sup,json=dict(version=0,stage='completed',decision='continue',reason='Artificial premature continue')).status_code==409


def test_withdrawn_source_blocks_continue_but_not_stop(client):
    c=client;src=published(c);reviewed(c,case(c));stage(c,'shadow');reviewed(c,case(c,'SHADOW'));stage(c,'assisted')
    row=reviewed(c,case(c,'ASSISTED'));observe(c,row)
    assert c.post('/api/s3/sources/'+src['id']+'/withdraw',headers=c.sup,json=dict(version=src['version'],reason='Artificial source invalid')).status_code==200
    r=c.post('/api/s3/stage',headers=c.sup,json=dict(version=2,stage='completed',decision='continue',reason='Artificial stale source review'))
    assert r.status_code==409 and r.json()['errors'][0]['code']=='STALE_EVIDENCE'
    r=c.post('/api/s3/stage',headers=c.sup,json=dict(version=2,stage='completed',decision='stop',reason='Artificial source invalid stop'))
    assert r.status_code==200 and r.json()['data']['closing_review']['real_effect']=='unverified'


def test_expired_authorization_allows_only_metadata_close(client):
    from datetime import timedelta
    from app.domain import timestamp,now
    c=client;cfg=c.get('/api/pilot',headers=c.sup).json()['data']['control']
    assert c.put('/api/pilot/plan',headers=c.sup,json=dict(c.plan,version=cfg['version'],ends_at=(timestamp(now())-timedelta(hours=1)).isoformat())).status_code==200
    p=next(p for p in plans(c) if p['current'])
    if c.mode=='merchant_authorized':assert c.get('/api/s3/workspace',headers=c.sup).status_code==403
    r=c.post('/api/s3/plans/'+p['id']+'/close',headers=c.sup,json=dict(version=1,session_version=0,decision='stop',reason='Artificial expired window stop'))
    assert r.status_code==200 and not r.json()['data']['closing_review']['evidence_complete']


def test_historical_close_does_not_close_new_plan(client):
    c=client;p=plans(c)[0];cfg=c.get('/api/pilot',headers=c.sup).json()['data']['control']
    assert c.put('/api/pilot/plan',headers=c.sup,json=dict(c.plan,version=cfg['version'],authorization_reference='NEW-SAMPLE-AUTH')).status_code==200
    r=c.post('/api/s3/plans/'+p['id']+'/close',headers=c.sup,json=dict(version=1,session_version=0,decision='adjust',reason='Artificial old plan adjusted'))
    assert r.status_code==200
    current=c.get('/api/s3/workspace',headers=c.sup).json()['data'];assert current['session']['stage']=='replay' and current['session']['version']==0


def test_closed_plan_cannot_add_source_review_or_measurement(client):
    from tests.test_trial import source
    c=client;published(c);row=case(c);checked=reviewed(c,case(c,'CHECKED'))
    draft=c.post('/api/s3/sources',headers=c.agent,json=source(c)).json()['data']
    assert c.post('/api/s3/stage',headers=c.sup,json=dict(version=0,stage='completed',decision='stop',reason='Artificial freeze on closure')).status_code==200
    assert c.post('/api/s3/sources',headers=c.agent,json=source(c)).status_code==409
    assert c.post('/api/s3/sources/'+draft['id']+'/review',headers=c.sup,json=dict(version=1,decision='pass',reason='Artificial after close')).status_code==409
    assert c.post('/api/s3/cases/'+row['id']+'/review',headers=c.sup,json=dict(version=1,decision='pass',reason='Artificial after close')).status_code==409
    assert observe(c,checked).status_code==409
