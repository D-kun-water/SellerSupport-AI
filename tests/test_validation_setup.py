import csv
import asyncio
import io
import json
from copy import deepcopy
import pytest
from fastapi.testclient import TestClient
from app.main import create_app
from app.model_config import ModelConfig
from scripts.check_backup import verify as restore

HEAD = {'Authorization':'Bearer test-supervisor-token'}
AGENT = {'Authorization':'Bearer test-agent-token'}
PLAN = dict(version=0, owner_id='local-supervisor', source_type='local', authorization_reference='local-fixture',
    authorization_confirmed=False, starts_at='2026-10-01T00:00:00+08:00', ends_at='2026-10-15T00:00:00+08:00', retention_days=30, minimum_sample=20)


def bundle(real=False):
    mode = 'merchant_authorized' if real else 'local'
    row = dict(case_id='C1',source_type=mode,review_mode='human' if real else 'test_review',question='private-customer@example.com question',
        expected_answer='private expected answer',agent_answer='private agent answer',manual_seconds='60',agent_wait_seconds='5',review_seconds='10',
        rework_seconds='5',baseline_error='false',assisted_error='false',semantic_decision='pass',reviewer='private-reviewer-name',
        reviewed_at='2026-10-01T10:00:00+08:00',evidence_ref='policy.md')
    f=io.StringIO(); w=csv.DictWriter(f,fieldnames=row);w.writeheader();w.writerow(row)
    return dict(manifest=dict(source_type=mode,merchant_name='provided offline merchant' if real else '',
        authorization=dict(confirmed=real,approved_by='provider' if real else '',reference='real-authorization' if real else '',scope=['local_offline']),
        files=[dict(path='policy.md',kind='policy')],observations_csv='observations.csv',knowledge_maintenance_seconds=10),
        files={'policy.md':'private policy original text','observations.csv':f.getvalue()})


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(tmp_path/'s3.sqlite3', model_config=ModelConfig(enabled=True, api_key='fixture'))) as c:
        yield c


def save(client, **changes):
    r=client.put('/api/pilot/plan',headers=HEAD,json=dict(PLAN,**changes));assert r.status_code==200,r.text
    return r.json()['data']


@pytest.mark.parametrize('path,method,body',[('/api/pilot','get',None),('/api/pilot/plan','put',PLAN),('/api/pilot/stop','post',dict(version=0,stopped=True,reason='test stop')),
    ('/api/pilot/reports','post',bundle()),('/api/merchant/members','get',None)])
def test_supervisor_boundary(client,path,method,body):
    assert client.request(method,path,headers=AGENT,json=body).status_code==403
    assert client.request(method,path,json=body).status_code==403


def test_plan_versions_and_window(client):
    save(client)
    assert client.put('/api/pilot/plan',headers=HEAD,json=PLAN).status_code==409
    assert client.put('/api/pilot/plan',headers=HEAD,json=dict(PLAN,version=1,ends_at=PLAN['starts_at'])).status_code==422


def test_private_aggregate_idempotency_and_delete(client):
    save(client); b=bundle()
    r=client.post('/api/pilot/reports',headers=HEAD,json=b);assert r.status_code==200,r.text
    report=r.json()['data'];assert report['net_saved_seconds']==30 and report['sample_status']=='insufficient'
    assert report['decision']=='no_real_merchant_conclusion'
    assert client.post('/api/pilot/reports',headers=HEAD,json=b).json()['data']['id']==report['id']
    dump=client.get('/api/export',headers=HEAD).text
    for text in ['private-customer','private expected','private agent','private-reviewer','private policy']:
        assert text not in dump
    url='/api/pilot/reports/'+report['id']+'/delete'
    assert client.post(url,headers=HEAD,json=dict(version=1,reason='retention_expired')).status_code==409
    assert client.post(url,headers=HEAD,json=dict(version=1,reason='authorization_revoked')).status_code==200
    deleted=client.get('/api/pilot',headers=HEAD).json()['data']['reports'][0]
    assert deleted['state']=='deleted' and 'fingerprint' not in deleted and 'sample_size' not in deleted
    assert client.post(url,headers=HEAD,json=dict(version=1,reason='provider_requested')).status_code==409


@pytest.mark.parametrize('mutation',['traversal','mixed','missing','negative','unknown','outside','infinite'])
def test_invalid_bundle_does_not_write(client,mutation):
    save(client); b=bundle()
    if mutation=='traversal':b['files']['../private.txt']='secret'
    if mutation=='mixed':b=bundle(True)
    if mutation=='missing':del b['files']['policy.md']
    if mutation=='negative':b['manifest']['knowledge_maintenance_seconds']=-1
    if mutation=='infinite':b['manifest']['knowledge_maintenance_seconds']='Infinity'
    if mutation=='unknown':b['files']['observations.csv']=b['files']['observations.csv'].replace(',pass,',',automatic_pass,')
    if mutation=='outside':b['files']['observations.csv']=b['files']['observations.csv'].replace('2026-10-01T10:00:00+08:00','2026-10-15T00:00:00+08:00')
    r=client.post('/api/pilot/reports',headers=HEAD,json=b);assert r.status_code==422
    assert 'private' not in r.text
    assert client.get('/api/pilot',headers=HEAD).json()['data']['reports']==[]


def test_real_offline_report_never_starts_real_runtime(client):
    save(client,source_type='merchant_authorized',authorization_confirmed=True,authorization_reference='real-authorization')
    r=client.post('/api/pilot/reports',headers=HEAD,json=bundle(True));assert r.status_code==200,r.text
    assert r.json()['data']['decision']=='collect_more_data'
    state=client.get('/api/pilot',headers=HEAD).json()['data']
    assert state['real_pilot_stage']=='not_started' and state['runtime_mode']=='local_only'


def test_authorization_reference_must_match(client):
    save(client,source_type='merchant_authorized',authorization_confirmed=True,authorization_reference='different-authorization')
    assert client.post('/api/pilot/reports',headers=HEAD,json=bundle(True)).status_code==422


def test_rotation_revocation_no_secret_echo(client):
    new='fixture-secret-long-credential-12345'
    r=client.patch('/api/merchant/members/local-agent',headers=AGENT,json=dict(version=0,token=new));assert r.status_code==200
    assert new not in r.text and 'token_hash' not in r.text
    assert client.get('/api/session',headers=AGENT).status_code==403
    fresh={'Authorization':'Bearer '+new};assert client.get('/api/session',headers=fresh).status_code==200
    assert client.patch('/api/merchant/members/local-supervisor',headers=fresh,json=dict(version=0,token='other-long-fixture-credential-12345')).status_code==403
    assert client.patch('/api/merchant/members/local-agent',headers=HEAD,json=dict(version=1,enabled=False)).status_code==200
    assert client.get('/api/session',headers=fresh).status_code==403
    assert new not in client.get('/api/export',headers=HEAD).text


def test_last_supervisor_and_duplicate_credentials(client):
    assert client.patch('/api/merchant/members/local-supervisor',headers=HEAD,json=dict(version=0,enabled=False)).status_code==409
    new='duplicate-test-long-credential-12345'
    assert client.patch('/api/merchant/members/local-agent',headers=HEAD,json=dict(version=0,token=new)).status_code==200
    assert client.patch('/api/merchant/members/test-limited',headers=HEAD,json=dict(version=0,token=new)).status_code==422


def test_validation_does_not_echo_input(client):
    private='private@example.com'
    r=client.patch('/api/merchant/members/local-agent',headers=HEAD,json=dict(version=0,token=private))
    assert r.status_code==422 and private not in r.text


def test_stop_restart_keeps_manual_available(client):
    r=client.post('/api/pilot/stop',headers=HEAD,json=dict(version=0,stopped=True,reason='fixture emergency'));assert r.status_code==200
    t=client.post('/api/tickets',headers=AGENT,json=dict(message='Does this cable charge?',sku='TEST-L1',device_id='TEST-D1',source_type='local',use_case='charging')).json()['data']
    payload=dict(version=t['version'],as_of='2026-10-01T00:00:00+00:00')
    assert client.post('/api/tickets/'+t['id']+'/assist-live',headers=AGENT,json=payload).status_code==503
    assert client.post('/api/tickets/'+t['id']+'/candidates/fixture/load-for-editing',headers=AGENT,json=dict(version=t['version'])).status_code==503
    assert client.post('/api/tickets/'+t['id']+'/assist',headers=AGENT,json=payload).status_code==200
    assert client.app.state.model_runtime.budget.summary()['unknown_usage_calls']==0
    with TestClient(create_app(client.app.state.store.path,model_config=ModelConfig())) as restarted:
        assert restarted.get('/api/pilot',headers=HEAD).json()['data']['control']['stopped']
    assert client.post('/api/pilot/stop',headers=HEAD,json=dict(version=0,stopped=False,reason='stale fixture')).status_code==409
    assert client.post('/api/pilot/stop',headers=HEAD,json=dict(version=1,stopped=False,reason='fixture restored')).status_code==200


def test_restore_all_tables_and_no_overwrite(client,tmp_path):
    save(client);client.post('/api/pilot/reports',headers=HEAD,json=bundle())
    r=restore(client.app.state.store.path,tmp_path/'backup.sqlite3',tmp_path/'restore.sqlite3')
    from app.store import TABLES
    assert r['state']=='passed' and r['tables']['pilot_reports']['count']==1 and len(r['tables'])==len(TABLES)+1
    with pytest.raises(ValueError):restore(client.app.state.store.path,tmp_path/'backup.sqlite3',tmp_path/'other.sqlite3')


@pytest.mark.parametrize('problem,decision',[('burden','fix_workload'),('quality','fix_quality'),('uncertain','fix_quality')])
def test_real_report_does_not_hide_negative_results(client,problem,decision):
    save(client,source_type='merchant_authorized',authorization_confirmed=True,authorization_reference='real-authorization',minimum_sample=1)
    b=bundle(True)
    if problem=='burden':b['manifest']['knowledge_maintenance_seconds']=100
    if problem=='quality':b['files']['observations.csv']=b['files']['observations.csv'].replace(',false,false,pass,',',false,true,fail,')
    if problem=='uncertain':b['files']['observations.csv']=b['files']['observations.csv'].replace(',pass,',',uncertain,')
    r=client.post('/api/pilot/reports',headers=HEAD,json=b);assert r.status_code==200,r.text
    assert r.json()['data']['decision']==decision


def test_stop_blocks_calls_already_waiting_in_queue(client):
    t=client.post('/api/tickets',headers=AGENT,json=dict(message='Does this cable charge?',sku='TEST-L1',device_id='TEST-D1',source_type='local',use_case='charging')).json()['data']
    runtime=client.app.state.model_runtime
    async def exercise():
        await runtime.slots.acquire();await runtime.slots.acquire()
        task=asyncio.create_task(runtime.run(dict(id='local-agent',role='agent',orders=['TEST-O1']),t['id'],
            dict(version=t['version'],as_of='2026-10-01T00:00:00+00:00')))
        for _ in range(100):
            if runtime.waiting:break
            await asyncio.sleep(0)
        assert runtime.waiting==1
        with client.app.state.store.transaction() as db:
            client.app.state.store.put(db,'pilot_settings','pilot',dict(id='pilot',version=1,stopped=True,plan=None))
        runtime.slots.release()
        result=await task;runtime.slots.release()
        return result
    result=asyncio.run(exercise())
    assert result['error_code']=='PILOT_STOPPED' and result['llm_calls']==0
    with client.app.state.store.transaction() as db:assert not client.app.state.store.all(db,'model_calls')
