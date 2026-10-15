import json
from copy import deepcopy
from datetime import timedelta
from pathlib import Path
import pytest
from fastapi.testclient import TestClient
from app.main import create_app
from app.model_config import ModelConfig
from app.domain import timestamp
from scripts.verify_second_merchant import acceptance

F=json.loads(Path('tests/fixtures/second-merchant.json').read_text(encoding='utf-8'))
HEAD={'Authorization':'Bearer beta-manager-local-local'}
BOOT={'Authorization':'Bearer test-supervisor-token'}

@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(tmp_path/'merchant.sqlite3',model_config=ModelConfig(),seed_mode='empty')) as c:
        assert c.post('/api/merchant/initialize',json=F['initialize'],headers=BOOT).status_code==200
        yield c

def draft(c,obj,kind='products',expected=None):
    r=c.post('/api/catalog-releases',json=dict(kind=kind,object=obj,source_facts=obj,source_text=F['source_text'],expected_version=expected,reason='测试测试发布'),headers=HEAD)
    assert r.status_code==200,r.text
    return r.json()['data']

def advance(c,row,actions=('review','regress','publish')):
    for action in actions:
        r=c.post(f"/api/catalog-releases/{row['id']}/{action}",json=dict(version=row['version'],reason='测试本地来源回归'),headers=HEAD)
        assert r.status_code==200,r.text
        row=r.json()['data']
    return row

def test_second_merchant_complete(tmp_path):
    result=acceptance(tmp_path/'acceptance.sqlite3')
    assert result['passed']==44 and result['model_calls']==0

def test_uninitialized_restart_and_creation(tmp_path):
    path=tmp_path/'empty.sqlite3'
    create_app(path,model_config=ModelConfig(),seed_mode='empty')
    with TestClient(create_app(path,model_config=ModelConfig())) as c:
        assert c.get('/api/merchant/setup',headers=BOOT).json()['data']['counts']['products']==0
        r=c.post('/api/tickets',json=dict(message='Hello',source_type='local'),headers=BOOT)
        assert r.status_code==409
        assert c.post('/api/merchant/initialize',json=F['initialize'],headers=BOOT).status_code==200

@pytest.mark.parametrize('field,value',[('paid','-1'),('paid','1.001'),('currency','EUR'),('source_type','real'),('region','CN'),('max_age_seconds',0),('delivered_at',None),('ordered_at','2027-01-01T00:00:00+00:00'),('observed_at','2020-01-01T00:00:00+00:00'),('last_tracking_at','2027-01-01T00:00:00+00:00')])
def test_bad_orders(client,field,value):
    order=deepcopy(F['orders'][0]);order[field]=value
    r=client.post('/api/order-imports/preview',json=dict(format='json',content=json.dumps([order])),headers=HEAD)
    assert r.status_code==422 and client.get('/api/orders',headers=HEAD).json()['data']==[]

@pytest.mark.parametrize('lines',[[dict(id='LINE',sku='BETA-CABLE-01',quantity=True)],[dict(id='LINE',sku='BETA-CABLE-01',quantity=0)],[dict(id='LINE',sku='BETA-CABLE-01',quantity=1)]*2,[]])
def test_invalid_order_lines(client,lines):
    order=dict(F['orders'][0],lines=lines)
    assert client.post('/api/order-imports/preview',json=dict(format='json',content=json.dumps([order])),headers=HEAD).status_code==422

def test_catalog_publish_stale_data_and_competing_versions(client):
    obj=F['products'][0];a=advance(client,draft(client,obj),('review','regress'))
    b=advance(client,draft(client,F['products'][1]))
    assert b['state']=='published'
    r=client.post(f"/api/catalog-releases/{a['id']}/publish",json=dict(version=a['version'],reason='拒绝过期回归结果'),headers=HEAD)
    assert r.status_code==409
    a=advance(client,a,('regress','publish'));assert a['state']=='published'
    update=dict(obj,version='2.0');one=draft(client,update,expected='1.0');two=draft(client,dict(obj,version='3.0'),expected='1.0')
    advance(client,one)
    assert client.post(f"/api/catalog-releases/{two['id']}/review",json=dict(version=1,reason='拒绝过期对象版本'),headers=HEAD).status_code==409

def test_future_policy_preserves_previous_effective_version(client):
    old=F['policies'][0];advance(client,draft(client,old,'policies'))
    new=dict(old,version='2.0',valid_from='2027-01-01T00:00:00+00:00',window_days=30)
    advance(client,draft(client,new,'policies','1.0'))
    from app import services as svc
    store=client.app.state.store
    with store.transaction() as db:
        before=svc.active(store,db,'policies','US','2026-10-01T00:00:00+00:00',kind='returns')
        after=svc.active(store,db,'policies','US','2027-01-01T00:00:00+00:00',kind='returns')
        assert len(before)==len(after)==1 and before[0]['window_days']==21 and after[0]['window_days']==30
        assert len(store.all(db,'catalog_releases'))==2

@pytest.mark.parametrize('mutation',[dict(shop_id='OTHER-SHOP'),dict(id='BETA-DIFFERENT'),dict(valid_to='2025-01-01T00:00:00+00:00'),dict(rules=[F['products'][0]['rules'][0]]*2),dict(power_w=9999)])
def test_invalid_product(client,mutation):
    obj=dict(F['products'][0],**mutation)
    assert client.post('/api/catalog-releases',json=dict(kind='products',object=obj,source_facts=obj,source_text=F['source_text'],reason='无效字段反例'),headers=HEAD).status_code==422

def test_credential_hash_only_and_no_audit_token(client):
    dump=client.get('/api/export',headers=HEAD).json()['data']
    serialized=json.dumps(dump)
    assert 'beta-manager-local-local' not in serialized and 'token_hash' not in serialized
    with client.app.state.store.transaction() as db:
        rows=client.app.state.store.all(db,'members')
        assert all('token_hash' in m and 'token' not in m for m in rows)
