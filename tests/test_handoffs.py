from copy import deepcopy
import pytest
from fastapi.testclient import TestClient
from app.main import create_app
from app.store import Store, TABLES

AGENT={'Authorization':'Bearer test-agent-token'}
SUP={'Authorization':'Bearer test-supervisor-token'}
RESTRICTED={'Authorization':'Bearer test-limited-local'}

@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(tmp_path/'handoff.sqlite3',serve_frontend=False)) as c:yield c

def data(r):
    assert r.status_code==200,r.text
    return r.json()['data']

def post(c,path,body,actor=AGENT):return data(c.post('/api'+path,headers=actor,json=body))
def get(c,path,actor=AGENT):return data(c.get('/api'+path,headers=actor))

def ticket(c,order='TEST-O1'):
    return post(c,'/tickets',dict(message='The charger is smoking. Where is my parcel? Please refund my order.',source_type='local',order_id=order,sku='TEST-C1'))

def handoff(c,t,assignee='local-supervisor'):
    return post(c,f"/tickets/{t['id']}/handoffs",dict(version=t['version'],assignee=assignee,reason='冒烟风险交人工',attempted_steps='未进行故障充电测试',pending_checks='核查安全、物流与退款资格'))

def action(c,h,op,actor=SUP,**kw):
    return post(c,f"/handoffs/{h['id']}/actions",dict(version=h['version'],action=op,note='测试处理操作理由',**kw),actor)

def unknown(c):
    t=ticket(c)
    p=post(c,'/action-proposals',dict(ticket_id=t['id'],amount='9.99',currency='USD',reason='测试退款申请'))
    p=post(c,f"/action-proposals/{p['id']}/approve",dict(version=p['version'],decision='approve',reason='测试主管审批'),SUP)
    p=post(c,f"/action-proposals/{p['id']}/execution-records",dict(version=p['version'],execution_mode='manual_record',result='unknown',receipt='测试执行未知记录',idempotency_key='execution-1'))
    return t,p

def reconciliation(c,p,result='success',**kw):
    o=get(c,f"/orders/{p['order_id']}",SUP)
    return dict(version=p['version'],order_version=o['version'],result=result,amount=p['amount'],currency=p['currency'],receipt='测试平台对账凭据',reason='测试核对执行记录',idempotency_key='reconcile-1',execution_mode='manual_record',**kw)

def test_handoff_complete_history_snapshot_and_reopen(client):
    t=ticket(client)
    run=post(client,f"/tickets/{t['id']}/assist",dict(version=t['version'],as_of='2026-09-29T12:00:00+00:00'))
    t=get(client,f"/tickets/{t['id']}")
    h=handoff(client,t)
    assert h['snapshot']['run']['id']==run['id']
    assert len(h['snapshot']['pending_items'])==3
    original=deepcopy(h['snapshot'])
    h=action(client,h,'accept')
    h=action(client,h,'note',reference='测试安全人工复核凭据')
    current=get(client,f"/tickets/{t['id']}",SUP)
    h=action(client,h,'close',ticket_version=current['version'],reference='测试安全与物流退款处理凭据')
    assert get(client,f"/tickets/{t['id']}")['status']=='closed'
    assert h['snapshot']==original
    closed=get(client,f"/tickets/{t['id']}")
    assert client.patch(f"/api/tickets/{t['id']}",headers=AGENT,json=dict(version=closed['version'],status='reopened')).status_code==409
    h=action(client,h,'reopen',actor=AGENT)
    assert h['state']=='assigned' and len(h['cycles'])==1
    assert h['cycles'][0]['snapshot']==original and h['cycles'][0]['notes']
    assert get(client,f"/tickets/{t['id']}")['resolution'] is None
    assert [x['action'] for x in h['history']]==['assigned','accept','note','close','reopen']

@pytest.mark.parametrize('action_name',['accept','note','close'])
def test_only_assignee_can_process(client,action_name):
    h=handoff(client,ticket(client))
    r=client.post(f"/api/handoffs/{h['id']}/actions",headers=AGENT,json=dict(version=h['version'],action=action_name,note='尝试非负责人操作',reference='测试结果凭据',ticket_version=2))
    assert r.status_code==403

def test_handoff_cannot_expand_order_access(client):
    t=ticket(client,'TEST-O2')
    r=client.post(f"/api/tickets/{t['id']}/handoffs",headers=AGENT,json=dict(version=t['version'],assignee='test-limited',reason='权限边界测试',attempted_steps='未做额外操作',pending_checks='核对订单授权'))
    assert r.status_code==403
    assert get(client,'/handoffs',RESTRICTED)==[]
    assert get(client,'/tickets',RESTRICTED)==[]

def test_assigned_authorized_agent_can_view_and_revocation_is_immediate(client):
    t=ticket(client)
    h=handoff(client,t,'test-limited')
    assert get(client,'/tickets',RESTRICTED)[0]['id']==t['id']
    assert get(client,'/handoffs',RESTRICTED)[0]['id']==h['id']
    h=action(client,h,'accept',actor=RESTRICTED)
    h=action(client,h,'reassign',assignee='local-supervisor')
    assert h['state']=='assigned'
    assert get(client,'/handoffs',RESTRICTED)==[]
    assert client.get(f"/api/tickets/{t['id']}",headers=RESTRICTED).status_code==403
    assert get(client,f"/tickets/{t['id']}")['owner']=='local-agent'

@pytest.mark.parametrize('endpoint',['assist','assist-live'])
def test_active_handoff_blocks_ai_and_direct_close(client,endpoint):
    t=ticket(client);handoff(client,t)
    current=get(client,f"/tickets/{t['id']}")
    assert client.post(f"/api/tickets/{t['id']}/{endpoint}",headers=AGENT,json=dict(version=current['version'],as_of='2026-09-29T12:00:00+00:00')).status_code==409
    assert client.patch(f"/api/tickets/{t['id']}",headers=AGENT,json=dict(version=current['version'],status='closed',resolution='试图跳过人工处理结案')).status_code==409

def test_close_requires_receipt_and_acknowledged_ticket_version(client):
    t=ticket(client);h=action(client,handoff(client,t),'accept')
    current=get(client,f"/tickets/{t['id']}")
    changed=data(client.patch(f"/api/tickets/{t['id']}",headers=AGENT,json=dict(version=current['version'],message='The customer added a new concern.')))
    r=client.post(f"/api/handoffs/{h['id']}/actions",headers=SUP,json=dict(version=h['version'],action='close',note='测试人工处理结果',ticket_version=changed['version']))
    assert r.status_code==422
    r=client.post(f"/api/handoffs/{h['id']}/actions",headers=SUP,json=dict(version=h['version'],action='close',note='测试人工处理结果',ticket_version=current['version'],reference='测试处理凭据'))
    assert r.status_code==409

def test_unknown_refund_blocks_both_close_paths(client):
    t,p=unknown(client)
    h=action(client,handoff(client,get(client,f"/tickets/{t['id']}")),'accept')
    t=get(client,f"/tickets/{t['id']}")
    r=client.post(f"/api/handoffs/{h['id']}/actions",headers=SUP,json=dict(version=h['version'],action='close',note='测试结果结案',reference='测试对账凭据',ticket_version=t['version']))
    assert r.status_code==409 and r.json()['errors'][0]['code']=='UNKNOWN_EXECUTION'
    assert client.patch(f"/api/tickets/{t['id']}",headers=AGENT,json=dict(version=t['version'],status='resolved',resolution='测试人工结案')).status_code==409

@pytest.mark.parametrize('result,state',[('success','executed'),('failed','failed'),('still_unknown','unknown')])
def test_unknown_reconciliation_preserves_history_and_money_idempotent(client,result,state):
    _,p=unknown(client);original=deepcopy(p['executions'])
    body=reconciliation(client,p,result)
    row=post(client,f"/action-proposals/{p['id']}/reconcile",body,SUP)
    assert row['state']==state and row['executions']==original
    assert row['approved_version'] is None
    assert get(client,f"/orders/{p['order_id']}",SUP)['refunded']==('9.99' if result=='success' else '0.00')
    replay=post(client,f"/action-proposals/{p['id']}/reconcile",body,SUP)
    assert replay==row and len(replay['reconciliations'])==1
    body['receipt']='测试不同凭据'
    assert client.post(f"/api/action-proposals/{p['id']}/reconcile",headers=SUP,json=body).status_code==409

def test_reconciliation_requires_supervisor_matching_amount_and_mode(client):
    _,p=unknown(client);body=reconciliation(client,p)
    assert client.post(f"/api/action-proposals/{p['id']}/reconcile",headers=AGENT,json=body).status_code==403
    body['amount']='8.00'
    assert client.post(f"/api/action-proposals/{p['id']}/reconcile",headers=SUP,json=body).status_code==422
    body['amount']=p['amount'];body['execution_mode']='live'
    assert client.post(f"/api/action-proposals/{p['id']}/reconcile",headers=SUP,json=body).status_code==422

def test_changed_order_blocks_success_ledger_reconciliation(client):
    _,p=unknown(client)
    with client.app.state.store.transaction() as db:
        o=client.app.state.store.get(db,'orders',p['order_id']);o['version']+=1
        client.app.state.store.put(db,'orders',o['id'],o)
    body=reconciliation(client,p)
    r=client.post(f"/api/action-proposals/{p['id']}/reconcile",headers=SUP,json=body)
    assert r.status_code==409 and r.json()['errors'][0]['code']=='STALE_DATA'
    assert get(client,'/action-proposals')[0]['state']=='unknown'

def test_reconciled_failure_needs_new_approval(client):
    _,p=unknown(client)
    p=post(client,f"/action-proposals/{p['id']}/reconcile",reconciliation(client,p,'failed'),SUP)
    body=dict(version=p['version'],execution_mode='manual_record',result='success',receipt='测试直接重试',idempotency_key='retry-key-001')
    assert client.post(f"/api/action-proposals/{p['id']}/execution-records",headers=AGENT,json=body).status_code==409
    p=data(client.patch(f"/api/action-proposals/{p['id']}",headers=AGENT,json=dict(version=p['version'],amount=p['amount'],reason='测试失败改版重审')))
    assert p['state']=='pending_approval'

def test_export_and_backup_include_handoff_history(client,tmp_path):
    h=handoff(client,ticket(client))
    dump=get(client,'/export',SUP)
    assert 'handoffs' in dump and dump['handoffs'][0]['id']==h['id']
    target=tmp_path/'restored.sqlite3';client.app.state.store.backup(target)
    restored=Store(target)
    with restored.transaction() as db,client.app.state.store.transaction() as original:
        assert {t:restored.all(db,t) for t in TABLES}=={t:client.app.state.store.all(original,t) for t in TABLES}
