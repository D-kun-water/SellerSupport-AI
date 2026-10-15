from fastapi.testclient import TestClient
from app.main import create_app
from app.merchant import digest
import pytest

HEAD={'Authorization':'Bearer test-supervisor-token'}

def test_only_two_local_identities_and_retired_login_denied(tmp_path):
    with TestClient(create_app(tmp_path/'roles.sqlite3',serve_frontend=False)) as client:
        members=client.get('/api/merchant/members',headers=HEAD).json()['data']
        assert {m['id'] for m in members}=={'local-agent','local-supervisor'}
        assert {m['role'] for m in members}=={'agent','supervisor'}
        assert client.get('/api/session',headers={'Authorization':'Bearer local-restricted-local'}).status_code==403

def test_existing_local_login_retired_without_erasing_authorship(tmp_path):
    path=tmp_path/'upgrade.sqlite3'
    app=create_app(path,serve_frontend=False)
    store=app.state.store
    with store.transaction() as db:
        store.put(db,'members','local-restricted',dict(id='local-restricted',label='legacy',
            role='agent',enabled=True,token_hash=digest('local-restricted-local')))
        store.put(db,'order_grants','local-restricted:TEST-O1',dict(id='local-restricted:TEST-O1',
            member_id='local-restricted',order_id='TEST-O1',enabled=True,version=1))
    with TestClient(app) as old:
        ticket=old.post('/api/tickets',headers={'Authorization':'Bearer local-restricted-local'},
            json=dict(message='Legacy customer question',source_type='local')).json()['data']
    with TestClient(create_app(path,serve_frontend=False)) as current:
        assert current.get('/api/session',headers={'Authorization':'Bearer local-restricted-local'}).status_code==403
        assert 'local-restricted' not in {m['id'] for m in current.get('/api/merchant/members',headers=HEAD).json()['data']}
        preserved=current.get('/api/tickets/'+ticket['id'],headers=HEAD).json()['data']
        assert preserved['owner']=='local-restricted' and preserved['messages']==ticket['messages']
    with store.transaction() as db:
        row=store.get(db,'members','local-restricted')
        assert row['enabled'] is False and row['retired_legacy_member'] is True and row['version']==1

def test_custom_identity_not_disabled_by_legacy_migration(tmp_path):
    path=tmp_path/'custom.sqlite3';app=create_app(path,serve_frontend=False)
    with app.state.store.transaction() as db:
        app.state.store.put(db,'members','local-restricted',dict(id='local-restricted',label='Custom agent',
            role='agent',enabled=True,token_hash=digest('custom-local-credential')))
    with TestClient(create_app(path,serve_frontend=False)) as client:
        assert client.get('/api/session',headers={'Authorization':'Bearer custom-local-credential'}).status_code==200

def test_available_reship_quantity_and_filtered_handoff_team(tmp_path):
    app=create_app(tmp_path/'scope.sqlite3',serve_frontend=False)
    store=app.state.store
    with store.transaction() as db:
        store.put(db,'members','other-agent',dict(id='other-agent',label='Other',role='agent',enabled=True,token_hash=digest('other-agent-token')))
        order=store.get(db,'orders','TEST-O1')
        store.put(db,'proposals','RESERVED',dict(id='RESERVED',type='reship',order_id=order['id'],state='unknown',items=[dict(line_id='TEST-O1-L1',sku=order['sku'],quantity=1)]))
    with TestClient(app) as client:
        t=client.post('/api/tickets',headers={'Authorization':'Bearer test-agent-token'},json=dict(message='Order assistance',order_id='TEST-O1',source_type='local')).json()['data']
        available=client.get('/api/orders/TEST-O1/reship-availability',headers=HEAD).json()['data']
        assert available['TEST-O1-L1']==0
        assert client.get('/api/orders/TEST-O1/reship-availability',headers={'Authorization':'Bearer other-agent-token'}).status_code==403
        team=client.get('/api/handoff-team?ticket_id='+t['id'],headers=HEAD).json()['data']
        assert 'other-agent' not in {m['id'] for m in team}
        assert {m['id'] for m in team}=={'local-agent','local-supervisor'}
        assert all('orders' not in m for m in team)
        assert client.get('/api/handoff-team?ticket_id='+t['id'],headers={'Authorization':'Bearer other-agent-token'}).status_code==403

@pytest.mark.parametrize('status',['resolved','closed'])
def test_closed_reply_guard_and_active_handoff_owner(tmp_path,status):
    app=create_app(tmp_path/'reply.sqlite3',serve_frontend=False)
    agent={'Authorization':'Bearer test-agent-token'}
    with TestClient(app) as client:
        t=client.post('/api/tickets',headers=agent,json=dict(message='Please help',source_type='local')).json()['data']
        endpoint='/api/tickets/'+t['id']
        with app.state.store.transaction() as db:
            t.update(status=status,draft='Old reply');app.state.store.put(db,'tickets',t['id'],t)
        for actor in [agent,HEAD]:
            assert client.patch(endpoint,headers=actor,json=dict(version=t['version'],draft='New reply')).status_code==409
            assert client.post(endpoint+'/copy-record',headers=actor,json=dict(version=t['version'],as_of='2026-10-11T00:00:00+00:00')).status_code==409
            assert client.post(endpoint+'/sent-confirmation',headers=actor,json=dict(version=t['version'],receipt='test receipt',execution_mode='manual_record')).status_code==409
        t=client.patch(endpoint,headers=agent,json=dict(version=t['version'],status='reopened')).json()['data']
        h=client.post(endpoint+'/handoffs',headers=agent,json=dict(version=t['version'],assignee='local-supervisor',reason='Need supervisor help',attempted_steps='Checked source',pending_checks='Review answer')).json()['data']
        t=client.get(endpoint,headers=agent).json()['data']
        assert client.patch(endpoint,headers=agent,json=dict(version=t['version'],draft='Not the owner')).status_code==403
        t=client.patch(endpoint,headers=HEAD,json=dict(version=t['version'],draft='Assigned owner reply')).json()['data']
        assert client.post(endpoint+'/copy-record',headers=agent,json=dict(version=t['version'],as_of='2026-10-11T00:00:00+00:00')).status_code==403
        assert client.post(endpoint+'/copy-record',headers=HEAD,json=dict(version=t['version'],as_of='2026-10-11T00:00:00+00:00')).status_code==200
