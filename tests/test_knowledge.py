import json
import pytest
from fastapi.testclient import TestClient
from app.main import create_app
from app.store import ROOT, encode

SUP={'Authorization':'Bearer test-supervisor-token'}
AGENT={'Authorization':'Bearer test-agent-token'}

@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(tmp_path/'knowledge.sqlite3',serve_frontend=False)) as c:
        yield c

def data(r):
    assert r.status_code==200,r.text
    return r.json()['data']

def draft(c,kind='products',changes=None,text=None):
    obj=data(c.get('/api/'+kind,headers=SUP))[2 if kind=='products' else 0]
    source=data(c.get('/api/sources/'+obj['source_id'],headers=SUP))
    return c.post('/api/knowledge-revisions',headers=SUP,json=dict(kind=kind,object_id=obj['id'],
             expected_version=obj['version'],changes=changes or {},source_text=text or source['text']+' local clarification: contact support for uncertain specifications.',reason='合成资料修订'))

def action(c,r,name):
    return c.post(f"/api/knowledge-revisions/{r['id']}/{name}",headers=SUP,json={'version':r['version'],'reason':'测试验收操作'})

def release(c,r):
    return data(action(c,data(action(c,r,'regression')),'publish'))

def test_release_rollback_preserves_runs_and_rebuilds_version(client):
    c=client
    case=json.loads((ROOT/'tests/fixtures/cases.json').read_text(encoding='utf-8'))[2]
    t=data(c.post('/api/tickets',headers=AGENT,json=case['ticket']))
    run=data(c.post(f"/api/tickets/{t['id']}/assist",headers=AGENT,json=case['assist']))
    before=data(c.get('/api/products/TEST-L1',headers=AGENT))
    r=data(draft(c,changes={'name':'合成线材修订说明'}))
    assert data(c.get('/api/products/TEST-L1',headers=AGENT))==before
    assert action(c,r,'publish').status_code==409
    r=data(action(c,r,'regression'))
    assert r['regression']['passed']==60 and r['regression']['llm_calls']==0
    assert len(data(c.get('/api/tickets',headers=SUP)))==1 # savepoint leaves no eval tickets
    r=data(action(c,r,'publish'))
    assert data(c.get('/api/products/TEST-L1',headers=AGENT))['product']['version']==r['id']
    oldrun=next(x for x in data(c.get('/api/runs',headers=SUP)) if x['id']==run['id'])
    assert oldrun['evidence_refs']==run['evidence_refs']
    rolled=release(c,data(action(c,r,'rollback')))
    restored=data(c.get('/api/products/TEST-L1',headers=AGENT))
    assert restored['product']['name']==before['product']['name']
    assert restored['evidence']['text']==before['evidence']['text']
    assert restored['product']['version']!=before['product']['version']
    assert rolled['rollback_of']==r['id']
    assert action(c,r,'rollback').status_code==409
    assert action(c,rolled,'publish').status_code==409
    assert any(x['action']=='knowledge_published' for x in data(c.get('/api/audit',headers=SUP)))

@pytest.mark.parametrize('action_name',['regression','publish','rollback'])
def test_non_supervisor_cannot_release(client,action_name):
    r=data(draft(client))
    assert client.post(f"/api/knowledge-revisions/{r['id']}/{action_name}",headers=AGENT,json={'version':r['version']}).status_code==403

def test_draft_scope_and_rule_source_validation(client):
    assert draft(client,changes={'window_days':20}).status_code==422
    assert draft(client,text='Sample source with no applicable product rules.').status_code==422
    assert draft(client,changes={'rules':[{'device_id':'TEST-D1','use_case':'video','result':'compatible','conditions':['invented']}]}).status_code==422

def test_regression_blocks_changed_return_boundary(client):
    r=data(draft(client,kind='policies',changes={'window_days':31},text='local US direct policy: return window is 31 days after delivery.'))
    r=data(action(client,r,'regression'))
    assert r['regression']['passed']<60
    assert action(client,r,'publish').status_code==409
    assert data(client.get('/api/policies',headers=SUP))[0]['window_days']==30

@pytest.mark.parametrize('table',['sources','orders','products'])
def test_regression_stale_after_fact_change(client,table):
    r=data(action(client,data(draft(client)),'regression'))
    store=client.app.state.store
    with store.transaction() as db:
        obj=store.all(db,table)[0];obj['version']='external-change';store.put(db,table,obj['id'],obj)
    assert action(client,r,'publish').status_code==409

def test_parallel_drafts_and_stale_revision_version(client):
    a=data(draft(client));b=data(draft(client))
    checked=data(action(client,a,'regression'))
    assert action(client,a,'regression').status_code==409
    release(client,b)
    assert action(client,checked,'publish').status_code==409

def test_policy_release_and_rollback_do_not_revive_approval(client):
    t=data(client.post('/api/tickets',headers=AGENT,json={'source_type':'local','message':'Can I return this order?','order_id':'TEST-O1'}))
    p=data(client.post('/api/action-proposals',headers=AGENT,json={'ticket_id':t['id'],'amount':'9.99','currency':'USD','reason':'测试退款申请'}))
    p=data(client.post(f"/api/action-proposals/{p['id']}/approve",headers=SUP,json={'version':p['version'],'decision':'approve','reason':'测试批准'}))
    r=release(client,data(draft(client,kind='policies')))
    def execute():
        return client.post(f"/api/action-proposals/{p['id']}/execution-records",headers=AGENT,json={'version':p['version'],'execution_mode':'manual_record','result':'success','receipt':'测试凭据','idempotency_key':'knowledge-test-001'})
    assert execute().status_code==409
    release(client,data(action(client,r,'rollback')))
    assert execute().status_code==409

def test_restart_keeps_published_revision(client):
    r=release(client,data(draft(client)))
    client.app.state.store.seed()
    assert data(client.get('/api/products/TEST-L1',headers=SUP))['product']['version']==r['id']

@pytest.mark.parametrize('scenario',['return_plus_tracking','correction_in_message','smoking_and_refund'])
def test_new_test_review_customer_boundaries(client,scenario):
    case=next(x for x in json.loads((ROOT/'tests/fixtures/scenario-merchant-cases.json').read_text(encoding='utf-8')) if x['scenario']==scenario)
    t=data(client.post('/api/tickets',headers=AGENT,json=case['ticket']))
    r=data(client.post(f"/api/tickets/{t['id']}/assist",headers=AGENT,json=case['assist']))
    assert r['status']==case['expected_status']
    assert all(x in r['reply_draft'] for x in case['required_reply_fragments'])
    assert data(client.get('/api/tickets/'+t['id'],headers=AGENT))['device_id']==t['device_id']
    from app.quality import check_draft
    _,quality=check_draft(json.dumps({k:r[k] for k in ('status','reply_draft','summary','missing_fields')}|{'claims':[]}),r)
    assert quality['hard_checks_passed']

def test_catalog_history_cannot_cancel_regression_boundary_fixtures(client):
    from copy import deepcopy
    from tests.catalog_builder import install_catalog
    from app.knowledge import fingerprint
    from app.evaluation import evaluate_cases
    store=client.app.state.store
    install_catalog(store)
    bundle=json.loads((ROOT/'tests/fixtures/edge-cases.json').read_text(encoding='utf-8'))
    cases=[deepcopy(c) for c in bundle['cases'] if c['id'] in {'S2C-006','S2C-007','S2C-009','S2C-010'}]
    with store.transaction() as db:
        before=fingerprint(store,db)
        result=evaluate_cases(store,db,cases)
        assert all(r['status']=='PASS' for r in result),result
        assert fingerprint(store,db)==before
        for case in cases:
            case.update(state='approved',reviewer='test-reviewer')
            store.put(db,'eval_cases',case['id'],case)
    revision=data(draft(client))
    checked=data(action(client,revision,'regression'))
    assert checked['regression']['passed']==checked['regression']['planned']==64
    published=data(action(client,checked,'publish'))
    assert published['state']=='published'
