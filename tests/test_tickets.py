import json
import hashlib
from datetime import timedelta
from decimal import Decimal
import pytest
from fastapi.testclient import TestClient
from app.main import create_app
from app.domain import timestamp, money, DomainError
from app.store import ROOT, Store, TABLES, encode

AGENT = {'Authorization': 'Bearer test-agent-token'}
SUP = {'Authorization': 'Bearer test-supervisor-token'}
RESTRICTED = {'Authorization': 'Bearer test-limited-local'}
AS_OF = '2026-09-29T12:00:00+00:00'
CASES = json.loads((ROOT / 'tests/fixtures/cases.json').read_text(encoding='utf-8'))


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(tmp_path / 'test.sqlite3', serve_frontend=False)) as c:
        yield c


def data(resp):
    assert resp.status_code == 200, resp.text
    return resp.json()['data']


def ticket(c, **extra):
    payload = dict(message='Can I return this order?', source_type='local', order_id='TEST-O1')
    payload.update(extra)
    return data(c.post('/api/tickets', headers=AGENT, json=payload))


def proposal(c, amount='9.99'):
    t = ticket(c)
    p = data(c.post('/api/action-proposals', headers=AGENT, json=dict(ticket_id=t['id'], amount=amount, currency='USD', reason='测试申请')))
    return p


def approve(c, p):
    return data(c.post(f"/api/action-proposals/{p['id']}/approve", headers=SUP, json=dict(version=p['version'], decision='approve', reason='审核测试规则及订单')))


def execution_payload(p, result='success', key='local-idempotent-001'):
    return dict(version=p['version'], execution_mode='manual_record', result=result, receipt='测试平台凭据 TEST-R1', idempotency_key=key)


@pytest.mark.parametrize('case', CASES, ids=[c['scenario'] for c in CASES])
@pytest.mark.parametrize('repeat', range(3))
def test_fixed_cases(client, case, repeat):
    t = data(client.post('/api/tickets', headers=AGENT, json=case['ticket']))
    r = data(client.post(f"/api/tickets/{t['id']}/assist", headers=AGENT, json=case['assist']))
    assert r['status'] == case['expected_status']
    assert r['llm_calls'] == 0 and r['requested_model'] is None and r['cost'] is None
    assert r['context_version'] == 1
    if case['scenario'] == 'safety_multi':
        assert r['item_results'] == [dict(intent='safety', state='handoff', next_action='人工安全队列'),dict(intent='order_status', state='deferred', next_action='安全交接后由人工查询')]
        assert 'tracking question remains pending' in r['reply_draft']
    if case['scenario'] == 'conditional_match':
        assert 'verified USB-C PD' in r['reply_draft']
        assert 'does not provide video' in r['reply_draft']
        assert r['evidence_refs'][0]['id'] == 'SRC-TEST-L1'
    if case['scenario'] == 'boundary_plus_second':
        assert r['item_results'][0]['rule_result'] == 'outside_window'
    if case['scenario'] == 'boundary_equal':
        assert r['item_results'][0]['rule_result'] == 'eligible'
    if case['scenario'] == 'injection':
        assert not data(client.get('/api/action-proposals', headers=AGENT))
        assert 'no refund has been approved or executed' in r['reply_draft']


def test_auth_never_frontend_role(client):
    assert client.get('/api/tickets').status_code == 403
    p = proposal(client)
    assert client.post(f"/api/action-proposals/{p['id']}/approve", headers=AGENT, json=dict(version=1, decision='approve', reason='request')).status_code == 403
    assert client.post('/api/tickets', headers=AGENT, json=dict(message='x', source_type='local', role='supervisor')).status_code == 422


def test_safety_preserves_return_and_tracking_without_querying_order(client):
    t=ticket(client,message='The charger is smoking. I want a refund and where is my shipment?',order_id=None)
    r=data(client.post(f"/api/tickets/{t['id']}/assist",headers=AGENT,json=dict(version=1,as_of=AS_OF)))
    assert r['status']=='escalated' and r['partial']
    assert [i['intent'] for i in r['item_results']]==['safety','order_status','returns']
    assert 'tracking question remains pending' in r['reply_draft']
    assert 'return or refund question remains pending' in r['reply_draft']
    assert r['missing_fields']==[] and r['evidence_refs']==[]


def test_delivery_clarification_preserves_exact_time_and_zone(client):
    t=ticket(client,order_id='TEST-O2')
    r=data(client.post(f"/api/tickets/{t['id']}/assist",headers=AGENT,json=dict(version=1,as_of=AS_OF)))
    assert r['missing_fields']==['delivered_at']
    assert 'exact time' in r['reply_draft'] and 'time zone' in r['reply_draft']


def test_safety_refund_order_reference_does_not_invent_tracking_intent(client):
    t=ticket(client,message='The charger is smoking. I want a refund for this order.',order_id=None)
    r=data(client.post(f"/api/tickets/{t['id']}/assist",headers=AGENT,json=dict(version=1,as_of=AS_OF)))
    assert [item['intent'] for item in r['item_results']]==['safety','returns']


def test_order_existence_not_leaked(client):
    a = client.get('/api/orders/TEST-O2', headers=RESTRICTED)
    b = client.get('/api/orders/NOT-REAL', headers=RESTRICTED)
    assert a.status_code == b.status_code == 403
    assert a.json()['errors'] == b.json()['errors']
    assert client.post('/api/tickets', headers=RESTRICTED, json=dict(message='show order',source_type='local',order_id='TEST-O2')).status_code == 403


def test_approval_is_not_refund(client):
    p = approve(client, proposal(client))
    assert p['state'] == 'approved' and not p['executions']
    assert data(client.get('/api/orders/TEST-O1', headers=AGENT))['refunded'] == '0.00'


def test_modified_amount_invalidates_approval(client):
    p = approve(client, proposal(client))
    old = execution_payload(p)
    revised = data(client.patch(f"/api/action-proposals/{p['id']}", headers=AGENT, json=dict(version=1, amount='8.99', reason='修改申请金额')))
    assert revised['approved_version'] is None and revised['version'] == 2
    assert client.post(f"/api/action-proposals/{p['id']}/execution-records", headers=AGENT, json=old).status_code == 409


def test_unknown_blocks_retries_and_edits(client):
    p = approve(client, proposal(client))
    endpoint = f"/api/action-proposals/{p['id']}/execution-records"
    unknown = execution_payload(p, 'unknown')
    assert data(client.post(endpoint, headers=AGENT, json=unknown))['state'] == 'unknown'
    assert data(client.post(endpoint, headers=AGENT, json=unknown))['state'] == 'unknown'
    assert client.post(endpoint, headers=AGENT, json=execution_payload(p, 'success', 'another-key')).status_code == 409
    assert client.patch(f"/api/action-proposals/{p['id']}", headers=AGENT, json=dict(version=1, amount='3.99', reason='try again')).status_code == 409


def test_execution_idempotency_and_amount(client):
    p = approve(client, proposal(client, '19.99'))
    endpoint = f"/api/action-proposals/{p['id']}/execution-records"
    payload = execution_payload(p)
    a = data(client.post(endpoint, headers=AGENT, json=payload))
    b = data(client.post(endpoint, headers=AGENT, json=payload))
    assert a == b and len(b['executions']) == 1
    o = data(client.get('/api/orders/TEST-O1', headers=AGENT))
    assert o['refunded'] == '19.99' and o['status'] == 'refunded'
    assert client.post(endpoint, headers=AGENT, json={**payload,'receipt':'different receipt'}).status_code == 409


@pytest.mark.parametrize('amount', ['20.00','-1','0','NaN','Infinity','0.001'])
def test_invalid_money(client, amount):
    t = ticket(client)
    assert client.post('/api/action-proposals', headers=AGENT, json=dict(ticket_id=t['id'],amount=amount,currency='USD',reason='测试金额')).status_code == 422


def test_cumulative_refunds_not_overpaid(client):
    p1, p2 = proposal(client, '12.00'), proposal(client, '12.00')
    approve(client, p1); approve(client, p2)
    data(client.post(f"/api/action-proposals/{p1['id']}/execution-records", headers=AGENT, json=execution_payload(p1)))
    assert client.post(f"/api/action-proposals/{p2['id']}/execution-records", headers=AGENT, json=execution_payload(p2, key='second-refund-key')).status_code == 409
    assert data(client.get('/api/orders/TEST-O1', headers=AGENT))['refunded'] == '12.00'


def test_order_change_blocks_approval(client):
    p = proposal(client)
    s = client.app.state.store
    with s.transaction() as db:
        o = s.get(db, 'orders', p['order_id']); o['version'] += 1; o['status'] = 'cancelled'; s.put(db,'orders',o['id'],o)
    assert client.post(f"/api/action-proposals/{p['id']}/approve", headers=SUP, json=dict(version=1,decision='approve',reason='review')).status_code == 409


def test_copy_does_not_resolve_or_send(client):
    t = ticket(client, order_id=None, sku='TEST-L1', device_id='TEST-D1',use_case='charging')
    data(client.post(f"/api/tickets/{t['id']}/assist",headers=AGENT,json=dict(version=1,as_of=AS_OF)))
    t = data(client.post(f"/api/tickets/{t['id']}/copy-record",headers=AGENT,json=dict(version=2,as_of=AS_OF)))
    assert t['copied'] and t['status']=='new' and not t['sent_confirmations']


def test_state_machine_and_resolution(client):
    t = ticket(client)
    endpoint=f"/api/tickets/{t['id']}"
    assert client.patch(endpoint,headers=AGENT,json=dict(version=1,status='closed')).status_code==422
    data(client.patch(endpoint,headers=AGENT,json=dict(version=1,status='in_progress')))
    assert client.patch(endpoint,headers=AGENT,json=dict(version=2,status='resolved')).status_code==422
    data(client.patch(endpoint,headers=AGENT,json=dict(version=2,status='resolved',resolution='测试处理凭据')))
    r=data(client.patch(endpoint,headers=AGENT,json=dict(version=3,status='reopened')))
    assert r['status']=='reopened'


def test_model_correction_clears_old_draft_keeps_history(client):
    t=ticket(client,message='Can I charge TEST-D1?',sku='TEST-L1',device_id='TEST-D1',use_case='charging',order_id=None)
    r=data(client.post(f"/api/tickets/{t['id']}/assist",headers=AGENT,json=dict(version=1,as_of=AS_OF)))
    t=data(client.patch(f"/api/tickets/{t['id']}",headers=AGENT,json=dict(version=2,device_id='TEST-D99')))
    assert not t['draft'] and t['context_version']==2
    new=data(client.post(f"/api/tickets/{t['id']}/assist",headers=AGENT,json=dict(version=3,as_of=AS_OF)))
    assert new['status']=='escalated'
    assert data(client.get('/api/runs',headers=AGENT))[0]==r


def test_snapshot_stale(client):
    t=ticket(client,message='Where is my shipment?',order_id='TEST-O2')
    r=data(client.post(f"/api/tickets/{t['id']}/assist",headers=AGENT,json=dict(version=1,as_of='2026-10-01T00:00:00+00:00')))
    assert r['item_results'][0]['rule_result']=='stale'


def test_policy_conflict_and_missing_source(client):
    s=client.app.state.store
    with s.transaction() as db:
        p=s.all(db,'policies')[0]; p['id']='conflicting-policy';p['window_days']=60;s.put(db,'policies',p['id'],p)
    t=ticket(client)
    r=data(client.post(f"/api/tickets/{t['id']}/assist",headers=AGENT,json=dict(version=1,as_of=AS_OF)))
    assert r['status']=='escalated' and len(r['evidence_refs'])==2


def test_history_snapshot_survives_source_disable(client):
    t=ticket(client,message='Can I charge TEST-D1?',sku='TEST-L1',device_id='TEST-D1',use_case='charging',order_id=None)
    r=data(client.post(f"/api/tickets/{t['id']}/assist",headers=AGENT,json=dict(version=1,as_of=AS_OF)))
    s=client.app.state.store
    with s.transaction() as db:
        src=s.get(db,'sources','SRC-TEST-L1');src['status']='disabled';s.put(db,'sources',src['id'],src)
    assert client.post(f"/api/tickets/{t['id']}/assist",headers=AGENT,json=dict(version=2,as_of=AS_OF)).status_code==409
    assert data(client.get('/api/runs',headers=AGENT))[0]['evidence_refs']==r['evidence_refs']


def test_import_preview_dedup_atomic_and_commit(client):
    rows=[dict(external_id='EXT-1',message='Does this work?',source_type='local')]*2
    b=data(client.post('/api/imports/preview',headers=AGENT,json=dict(kind='tickets',rows=rows)))
    assert not data(client.get('/api/tickets',headers=AGENT))
    assert [r['status'] for r in b['details']]==['valid','duplicate_in_file']
    endpoint=f"/api/imports/{b['id']}/commit"
    assert data(client.post(endpoint,headers=AGENT))['inserted']==1
    assert data(client.post(endpoint,headers=AGENT))['inserted']==1
    assert len(data(client.get('/api/tickets',headers=AGENT)))==1
    repeat=data(client.post('/api/imports/preview',headers=AGENT,json=dict(kind='tickets',rows=rows)))
    assert repeat['details'][0]['status']=='duplicate'
    bad=data(client.post('/api/imports/preview',headers=AGENT,json=dict(kind='tickets',rows=[dict(external_id='EXT-2',message='x',source_type='local'),dict(message='x',source_type='authorized')])))
    assert client.post(f"/api/imports/{bad['id']}/commit",headers=AGENT).status_code==422
    assert len(data(client.get('/api/tickets',headers=AGENT)))==1


def test_duplicate_products_no_overwrite(client):
    products=data(client.get('/api/products',headers=AGENT))
    b=data(client.post('/api/imports/preview',headers=AGENT,json=dict(kind='products',rows=products)))
    assert all(d['status']=='duplicate' for d in b['details'])
    assert data(client.post(f"/api/imports/{b['id']}/commit",headers=AGENT))['inserted']==0
    conflict={**products[0],'power_w':999}
    b=data(client.post('/api/imports/preview',headers=AGENT,json=dict(kind='products',rows=[conflict])))
    assert b['details'][0]['status']=='error'


def test_fixture_integrity():
    bundle=json.loads((ROOT/'tests/fixtures/catalog.json').read_text(encoding='utf-8'))
    assert len(bundle['products'])==12 and len(CASES)==20
    sources={s['id']:s for s in bundle['sources']}
    for p in bundle['products']:
        assert p['source_type']=='local' and p['id'].startswith('TEST-')
        s=sources[p['source_id']]
        assert s['checksum']==hashlib.sha256(s['text'].encode()).hexdigest()
        assert (ROOT / s['locator'].split('#')[0]).is_file()
    for o in bundle['orders']:
        assert timestamp(o['ordered_at']) < timestamp(o['shipped_at']) <= timestamp(o['observed_at'])
        if o['delivered_at']:
            assert timestamp(o['shipped_at']) <= timestamp(o['delivered_at']) <= timestamp(o['observed_at'])
        assert Decimal(o['refunded'])<=Decimal(o['paid'])


def test_restore_retains_audit_versions_and_evidence(client,tmp_path):
    p=approve(client,proposal(client))
    data(client.post(f"/api/action-proposals/{p['id']}/execution-records",headers=AGENT,json=execution_payload(p,'unknown')))
    original=client.app.state.store
    backup=tmp_path/'backup.sqlite3'
    original.backup(backup)
    restored=Store(backup)
    with original.transaction() as a, restored.transaction() as b:
        for table in TABLES:
            assert encode(original.all(a,table))==encode(restored.all(b,table))
    with TestClient(create_app(backup)) as c:
        assert data(c.get('/api/action-proposals',headers=SUP))[0]['state']=='unknown'


def test_timezones_and_invalid_time():
    assert timestamp('2026-09-29T20:00:00+08:00')==timestamp(AS_OF)
    with pytest.raises(DomainError):timestamp('2026-09-29T20:00:00')


def test_two_clarifications_recommend_handoff(client):
    t=ticket(client,message='Does this work with my iPad?',sku='TEST-L1',order_id=None)
    a=data(client.post(f"/api/tickets/{t['id']}/assist",headers=AGENT,json=dict(version=1,as_of=AS_OF)))
    b=data(client.post(f"/api/tickets/{t['id']}/assist",headers=AGENT,json=dict(version=2,as_of=AS_OF)))
    assert a['status']=='need_clarification' and b['status']=='escalated'


def test_policy_changed_invalidates_approval(client):
    p=approve(client,proposal(client))
    s=client.app.state.store
    with s.transaction() as db:
        policy=s.get(db,'policies','TEST-RET-US-v1');policy['window_days']=15;s.put(db,'policies',policy['id'],policy)
    assert client.post(f"/api/action-proposals/{p['id']}/execution-records",headers=AGENT,json=execution_payload(p)).status_code==409
