from datetime import datetime, timedelta, timezone
from fastapi.testclient import TestClient

from app import domain
from app.main import create_app
from app.model_config import ModelConfig

AGENT={'Authorization':'Bearer test-agent-token'}


def test_daily_assist_uses_current_time_and_replay_can_use_history(tmp_path,monkeypatch):
    class Clock(datetime):
        @classmethod
        def now(cls,tz=None):
            return datetime(2026,10,13,12,tzinfo=timezone.utc)
    monkeypatch.setattr(domain,'datetime',Clock)
    app=create_app(tmp_path/'current-time.sqlite3',serve_frontend=False,model_config=ModelConfig())
    client=TestClient(app)
    with app.state.store.transaction() as db:
        policy=next(p for p in app.state.store.all(db,'policies')
                    if p['region']=='US' and p['kind']=='returns' and p['review_status']=='published')
        order=app.state.store.get(db,'orders','TEST-O1')
        order['delivered_at']=(Clock.now()-timedelta(days=policy['window_days']+1)).isoformat()
        app.state.store.put(db,'orders',order['id'],order)
    response=client.post('/api/tickets',headers=AGENT,json=dict(
        message='Can I return this order?',source_type='local',order_id='TEST-O1'))
    assert response.status_code==200
    ticket=response.json()['data']
    path='/api/tickets/'+ticket['id']
    # Daily UI omits as_of; the return window must follow the server clock.
    response=client.post(path+'/assist',headers=AGENT,json=dict(version=ticket['version']))
    assert response.status_code==200,response.text
    current=response.json()['data']
    assert current['as_of']=='2026-10-13T12:00:00+00:00'
    assert current['item_results'][0]['rule_result']=='outside_window'
    latest=client.get(path,headers=AGENT).json()['data']
    copied=client.post(path+'/copy-record',headers=AGENT,json=dict(version=latest['version']))
    assert copied.status_code==200,copied.text
    assert copied.json()['data']['copied'] and copied.json()['data']['sent_confirmations']==[]
    # Historical evaluation keeps its explicit time and gives a different window result.
    latest=copied.json()['data']
    replay=client.post(path+'/assist',headers=AGENT,json=dict(
        version=latest['version'],as_of='2026-10-12T12:00:00+00:00'))
    assert replay.status_code==200,replay.text
    assert replay.json()['data']['item_results'][0]['rule_result']=='eligible'
