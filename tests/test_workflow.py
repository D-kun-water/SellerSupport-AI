"""Workflow contracts across reply completion, approvals and task ownership."""
import pytest
from fastapi.testclient import TestClient
from app.main import create_app
from app.model_config import ModelConfig
from app.domain import now

A={'Authorization':'Bearer test-agent-token'}
S={'Authorization':'Bearer test-supervisor-token'}
@pytest.fixture
def client(tmp_path):
    return TestClient(create_app(tmp_path/'workflow.sqlite3',serve_frontend=False,model_config=ModelConfig()))

def post(c,path,body,actor=A):
    r=c.post('/api'+path,headers=actor,json=body)
    assert r.status_code==200,r.text
    return r.json()['data']

def get(c,path,actor=A):
    r=c.get('/api'+path,headers=actor)
    assert r.status_code==200,r.text
    return r.json()['data']

def ticket(c,**extra):
    return post(c,'/tickets',dict(message='Can TEST-L1 charge TEST-D1?',source_type='local',sku='TEST-L1',device_id='TEST-D1',use_case='charging',**extra))

def patch(c,t,**fields):
    r=c.patch('/api/tickets/'+t['id'],headers=A,json={'version':t['version'],**fields})
    assert r.status_code==200,r.text
    return r.json()['data']

def task(c,owner='local-agent',kind='process'):
    t=ticket(c)
    f=post(c,'/feedback',dict(ticket_id=t['id'],message_id=t['messages'][0]['id'],quote=t['messages'][0]['text'],label='instructions'))
    issue=post(c,'/issues',dict(title='Separate charging and video',feedback_ids=[f['id']]),S)
    post(c,f"/issues/{issue['id']}/review",dict(version=issue['version'],reason='Checked original question',decisions=[dict(feedback_id=f['id'],decision='accepted')]),S)
    return post(c,'/tasks',dict(issue_id=issue['id'],title='Clarify charging workflow',type=kind,owner=owner,expected_change='Separate charging and video checks',acceptance_criteria='Keep source and handling record'),S)

def test_reply_and_resolve_are_one_atomic_operation(client):
    t=patch(client,ticket(client),draft='Verified charging conditions. No video output.')
    assert t['status']=='in_progress'
    t=post(client,f"/tickets/{t['id']}/sent-confirmation",dict(version=t['version'],receipt='Reviewed and recorded reply',execution_mode='manual_record',resolve=True))
    assert t['status']=='resolved' and len(t['sent_confirmations'])==1
    r=client.post(f"/api/tickets/{t['id']}/sent-confirmation",headers=A,json=dict(version=t['version'],receipt='Cannot repeat after resolving',execution_mode='manual_record',resolve=True))
    assert r.status_code==409
    assert len(get(client,'/tickets/'+t['id'])['sent_confirmations'])==1

def test_aftercare_updates_ticket_and_prevents_early_completion(client):
    t=patch(client,ticket(client,order_id='TEST-O4'),draft='Your request is under review.')
    p=post(client,'/action-proposals',dict(ticket_id=t['id'],amount='1.00',currency='USD',reason='Partial refund request'))
    current=get(client,'/tickets/'+t['id']);assert current['status']=='waiting_approval'
    r=client.post(f"/api/tickets/{t['id']}/sent-confirmation",headers=A,json=dict(version=current['version'],receipt='Attempt before review',execution_mode='manual_record',resolve=True))
    assert r.status_code==409 and not get(client,'/tickets/'+t['id'])['sent_confirmations']
    p=post(client,f"/action-proposals/{p['id']}/approve",dict(version=p['version'],decision='approve',reason='Reviewed order and policy'),S)
    assert get(client,'/tickets/'+t['id'])['status']=='waiting_execution'
    assert client.post(f"/api/action-proposals/{p['id']}/execution-records",headers=S,json=dict(version=p['version'],execution_mode='manual_record',result='success',receipt='Not manager execution',idempotency_key='manager-001')).status_code==403
    post(client,f"/action-proposals/{p['id']}/execution-records",dict(version=p['version'],execution_mode='manual_record',result='success',receipt='REF-WORKFLOW-001',idempotency_key='workflow-001'))
    assert get(client,'/tickets/'+t['id'])['status']=='in_progress'
    context=get(client,f"/action-proposals/{p['id']}/context",S)
    assert context['refundable']=='18.99' and context['ticket']['id']==t['id'] and context['policy']['evidence_refs']

def test_rejected_refund_restores_work_in_progress(client):
    t=ticket(client,order_id='TEST-O4')
    p=post(client,'/action-proposals',dict(ticket_id=t['id'],amount='1.00',currency='USD',reason='Review request'))
    post(client,f"/action-proposals/{p['id']}/approve",dict(version=p['version'],decision='reject',reason='Insufficient purchase evidence'),S)
    assert get(client,'/tickets/'+t['id'])['status']=='in_progress'

def test_order_can_be_linked_before_aftercare_but_not_changed_after(client):
    t=patch(client,ticket(client),order_id='TEST-O4')
    assert t['order_id']=='TEST-O4'
    post(client,'/action-proposals',dict(ticket_id=t['id'],amount='1.00',currency='USD',reason='Review request'))
    t=get(client,'/tickets/'+t['id'])
    assert client.patch('/api/tickets/'+t['id'],headers=A,json=dict(version=t['version'],order_id='TEST-O3')).status_code==409

def test_task_owner_executes_and_supervisor_accepts_results(client):
    t=task(client)
    assert t['owner_id']=='local-agent' and len(get(client,'/tasks'))==1
    r=client.post(f"/api/tasks/{t['id']}/state",headers=S,json=dict(version=t['version'],state='accepted',reason='Manager cannot accept for owner'))
    assert r.status_code==403
    t=post(client,f"/tasks/{t['id']}/state",dict(version=t['version'],state='accepted',reason='Owner accepts'))
    t=post(client,f"/tasks/{t['id']}/state",dict(version=t['version'],state='in_progress',reason='Owner starts'))
    t=post(client,f"/tasks/{t['id']}/deliver",dict(version=t['version'],reference='FLOW-WORKFLOW-001',result='Verified charging and video steps'))
    assert client.post(f"/api/tasks/{t['id']}/activate",headers=A,json=dict(version=t['version'],effective_at=now(),evidence='FLOW-WORKFLOW-001')).status_code==403
    t=post(client,f"/tasks/{t['id']}/activate",dict(version=t['version'],effective_at=now(),evidence='FLOW-WORKFLOW-001'),S)
    assert t['state']=='observing'
    assert client.post(f"/api/tasks/{t['id']}/state",headers=A,json=dict(version=t['version'],state='done',reason='Owner cannot approve own delivery')).status_code==403

def test_tasks_owned_by_other_member_are_not_disclosed(client):
    t=task(client,'local-supervisor')
    assert get(client,'/tasks')==[]
    assert client.post(f"/api/tasks/{t['id']}/deliver",headers=A,json=dict(version=t['version'],reference='Unauthorized',result='Cannot modify')).status_code==403
    t=post(client,f"/tasks/{t['id']}/assign",dict(version=t['version'],owner='local-agent',reason='Assign work to customer service'),S)
    assert get(client,'/tasks')[0]['owner_id']=='local-agent'
    assert client.get('/api/task-members',headers=A).status_code==403

def test_suggestions_extract_exact_facts_without_guessing_device_compatibility(client):
    result=post(client,'/consultation-suggestions',dict(message='Can the 1m 60W USB-C cable charge TEST-D1?'))
    assert result['sku']=='TEST-L1' and result['device_id']=='TEST-D1' and result['needs_confirmation']
    result=post(client,'/consultation-suggestions',dict(message='Does this cable work with my device?'))
    assert result['sku'] is None and result['device_id'] is None
    result=post(client,'/consultation-suggestions',dict(message='Does TEST-L10 support my TEST-D1-OTHER device?'))
    assert result['sku'] is None and result['device_id'] is None

def test_model_control_requires_manager_and_configured_key(client):
    assert client.post('/api/model-settings',headers=A,json={'enabled':True}).status_code==403
    assert client.post('/api/model-settings',headers=S,json={'enabled':True}).status_code==409


def test_agent_content_delivery_can_be_reviewed_and_published_by_manager(client):
    t=task(client,kind='faq')
    for state in ('accepted','in_progress'):
        t=post(client,f"/tasks/{t['id']}/state",dict(version=t['version'],state=state,reason='Owner handles content task'))
    t=post(client,f"/tasks/{t['id']}/content-draft",dict(version=t['version'],text='This cable does not provide video output.'))
    draft=t['content_draft']
    rev=post(client,'/knowledge-revisions',dict(kind='products',object_id=t['sku'],expected_version=draft['product_version'],changes={},source_text=draft['after_text'],reason='Review quoted facts',task_id=t['id']),S)
    t=post(client,f"/tasks/{t['id']}/deliver",dict(version=t['version'],reference=rev['id'],result='Verified FAQ content is ready for review'))
    rev=post(client,f"/knowledge-revisions/{rev['id']}/regression",dict(version=rev['version']),S)
    assert rev['regression']['passed']==rev['regression']['planned']
    rev=post(client,f"/knowledge-revisions/{rev['id']}/publish",dict(version=rev['version'],reason='Reviewed facts and passing tests'),S)
    t=post(client,f"/tasks/{t['id']}/activate",dict(version=t['version'],effective_at=now(),revision_id=rev['id']),S)
    assert t['state']=='observing' and t['revision_id']==rev['id']
