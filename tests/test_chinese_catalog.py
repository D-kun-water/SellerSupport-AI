from copy import deepcopy
from hashlib import sha256
import json
import re
import pytest
from fastapi.testclient import TestClient
from app.main import create_app
from tests.catalog_builder import install_catalog,RELEASE
from app import services as svc
from app.assist import assist
from app.catalog_api import Product
from app.domain import DomainError
from app.store import ROOT,encode

AT='2026-10-13T12:00:00+00:00'
AGENT=dict(id='local-agent',role='agent',orders=['TEST-O1'])

@pytest.fixture
def store(tmp_path,monkeypatch):
    monkeypatch.setattr('tests.catalog_builder.now',lambda:AT)
    return create_app(tmp_path/'catalog.sqlite3',serve_frontend=False).state.store

def current(store,db,table):return svc.active(store,db,table,'US',AT)+svc.active(store,db,table,'EU',AT)

def test_complete_chinese_catalog_and_idempotent_release(store):
    result=install_catalog(store)
    assert result['products']==30 and result['policies']==2
    with store.transaction() as db:
        ps=current(store,db,'products');pols=current(store,db,'policies')
        assert len(ps)==30 and len({p['category'] for p in ps})==7
        assert len({p['sku'] for p in ps})==30
        for p in ps+pols:
            source=store.get(db,'sources',p['source_id'])
            assert re.search('[一-龥]',source['text'])
            assert not any(word in source['text'] for word in ('Rule for','Maximum rated','This cable','local','sample product','Structured facts'))
            assert source['checksum']==sha256(source['text'].encode()).hexdigest()
            if 'rules' in p:
                for r in p['rules']:
                    assert all(re.search('[一-龥]',c) for c in r['conditions'])
                    assert len(r['conditions_en'])==len(r['conditions'])
        before={t:encode(store.all(db,t)) for t in ('products','sources','policies','catalog_releases','audit')}
    assert install_catalog(store)['applied'] is False
    with store.transaction() as db:
        assert before=={t:encode(store.all(db,t)) for t in before}

def test_old_sources_runs_and_historical_decisions_are_unchanged(store):
    with store.transaction() as db:
        sources=deepcopy(store.all(db,'sources'))
        ticket=svc.create_ticket(store,db,AGENT,dict(source_type='local',message='Can I charge TEST-D1?',region='US',channel='direct',sku='TEST-L1',device_id='TEST-D1',use_case='charging'))
        before=assist(store,db,AGENT,ticket['id'],dict(version=ticket['version'],as_of='2026-09-29T12:00:00+00:00'))
    install_catalog(store)
    with store.transaction() as db:
        assert store.get(db,'runs',before['id'])==before
        for s in sources:assert store.get(db,'sources',s['id'])==s
        ticket=store.get(db,'tickets',ticket['id'])
        replay=assist(store,db,AGENT,ticket['id'],dict(version=ticket['version'],as_of=before['as_of']),persist=False)
        assert replay['reply_draft']==before['reply_draft'] and replay['evidence_refs']==before['evidence_refs']

@pytest.mark.parametrize('sku,use,required',[('TEST-H2','video','DP Alt Mode'),('TEST-P2','charging','65W'),('TEST-U3','data','1TB')])
def test_new_category_rules_produce_english_draft_with_chinese_evidence(store,sku,use,required):
    install_catalog(store)
    with store.transaction() as db:
        ticket=svc.create_ticket(store,db,AGENT,dict(source_type='local',message='Please confirm compatibility.',region='US',channel='direct',sku=sku,device_id='TEST-D1',use_case=use))
        result=assist(store,db,AGENT,ticket['id'],dict(version=ticket['version'],as_of=AT),persist=False)
        assert result['status']=='draft' and required in result['reply_draft']
        assert not re.search('[一-龥]',result['reply_draft'])
        assert re.search('[一-龥]',result['evidence_refs'][0]['text'])
        assert 'test product' not in result['reply_draft']

def test_unknown_device_and_policy_boundaries_are_not_invented(store):
    install_catalog(store)
    with store.transaction() as db:
        p=store.get(db,'policies','TEST-RET-US-v1');assert p['window_days']==30
        assert svc.policy_result(store,db,dict(delivered_at='2026-09-13T12:00:00+00:00'),'US',AT)['result']=='eligible'
        assert svc.policy_result(store,db,dict(delivered_at='2026-09-13T11:59:59+00:00'),'US',AT)['result']=='outside_window'
        t=svc.create_ticket(store,db,AGENT,dict(source_type='local',message='Please confirm compatibility.',region='US',channel='direct',sku='TEST-H1',device_id='TEST-UNKNOWN',use_case='data'))
        assert assist(store,db,AGENT,t['id'],dict(version=t['version'],as_of=AT),persist=False)['status']=='escalated'

def test_unknown_translation_rolls_back_every_catalog_change(store):
    with store.transaction() as db:
        p=store.get(db,'products','TEST-L1');p['rules'][0]['conditions'].append('Unreviewed new condition.');store.put(db,'products',p['id'],p)
        before={t:encode(store.all(db,t)) for t in ('products','sources','policies','catalog_releases','audit')}
    with pytest.raises(DomainError):install_catalog(store)
    with store.transaction() as db:
        assert before=={t:encode(store.all(db,t)) for t in before}

def test_other_merchant_does_not_receive_default_products(store):
    with store.transaction() as db:
        settings=store.all(db,'merchant_settings')[0];settings['shop_id']='OTHER-SHOP';store.put(db,'merchant_settings',settings['id'],settings)
    assert install_catalog(store)['applied'] is False
    with store.transaction() as db:assert not any(p['id']=='TEST-H1' for p in store.all(db,'products'))

def test_release_preserves_live_custom_conditions_and_rule_pair_validation(store):
    with store.transaction() as db:
        p=store.get(db,'products','TEST-L1');p['rules'][0]['conditions'].append('Data rate: 480 Mbps.');store.put(db,'products',p['id'],p)
    install_catalog(store)
    with store.transaction() as db:
        p=store.get(db,'products','TEST-L1')
        assert '数据传输速率上限为 480 Mbps。' in p['rules'][0]['conditions']
        obj={k:v for k,v in p.items() if k not in ('review_status','source_id')}
        obj['rules'][0]['conditions_en']=['wrong count']
        with pytest.raises(ValueError):Product.model_validate(obj)

def test_category_schema_and_http_catalog(store):
    install_catalog(store)
    client=TestClient(create_app(store.path,serve_frontend=False))
    response=client.get('/api/products/TEST-U3',headers={'Authorization':'Bearer test-agent-token'})
    assert response.status_code==200
    assert response.json()['data']['product']['specifications']['标称容量']=='1TB'
    assert '出厂为 exFAT' in response.json()['data']['evidence']['text']
