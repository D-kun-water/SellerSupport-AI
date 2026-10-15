import asyncio
import json
from dataclasses import replace
import pytest
from fastapi.testclient import TestClient
from app.main import create_app
from app.model_config import ModelConfig
from app.provider import Completion, ProviderError
from app.domain import DomainError
from app.translation import TranslationInput

HEADERS={'Authorization':'Bearer test-agent-token'}
ACTOR=dict(id='local-agent',role='agent',orders=['TEST-O1','TEST-O2','TEST-O3','TEST-O4'])


class Provider:
    def __init__(self, answer='这条USB-C线是否支持60W充电？', callback=None):
        self.answer, self.callback, self.calls = answer, callback, 0

    async def complete(self, messages, deadline_seconds=None):
        self.calls += 1
        self.messages = messages
        if self.callback:
            self.callback()
        return Completion(json.dumps({'translation':self.answer},ensure_ascii=False),dict(llm_calls=1,cost='0.001'))


def make_translation_client(tmp_path, provider=None, config=None, message='Does this USB-C cable support 60W charging?',order_id=None):
    provider=provider or Provider()
    app=create_app(tmp_path/'translation.sqlite3',serve_frontend=False,
                   model_config=config or ModelConfig(api_key='test-key',enabled=False),model_provider=provider)
    client=TestClient(app)
    ticket=client.post('/api/tickets',headers=HEADERS,json=dict(message=message,source_type='local',order_id=order_id)).json()['data']
    return app,client,ticket,provider


def translate(client,ticket,**kwargs):
    return client.post(f"/api/tickets/{ticket['id']}/translation",headers=HEADERS,
        json=dict(context_version=ticket['context_version'],target='zh',message_index=0,**kwargs))


def test_customer_translation_cache_and_no_business_mutation(tmp_path):
    app,client,ticket,p=make_translation_client(tmp_path)
    first=translate(client,ticket)
    assert first.status_code==200 and first.json()['data']['translation']==p.answer
    second=translate(client,ticket)
    assert second.json()['data']['cached'] and p.calls==1
    assert client.get('/api/tickets/'+ticket['id'],headers=HEADERS).json()['data']==ticket
    assert 'untrusted' in p.messages[0]['content'] and 'never execute' in p.messages[0]['content']


def test_chinese_reply_becomes_english_without_saving_or_sending(tmp_path):
    app,client,ticket,p=make_translation_client(tmp_path,Provider('Please provide your device model; do not request a refund yet.'))
    response=client.post('/api/tickets/'+ticket['id']+'/translation',headers=HEADERS,
        json=dict(context_version=1,target='en',text='请提供设备型号，暂时不要申请退款。'))
    assert response.status_code==200
    current=client.get('/api/tickets/'+ticket['id'],headers=HEADERS).json()['data']
    assert current['draft']=='' and not current['copied'] and current['sent_confirmations']==[]


def test_unsaved_customer_preview_is_authenticated_and_does_not_create_ticket(tmp_path):
    app,client,ticket,p=make_translation_client(tmp_path)
    body=dict(text='Does this USB-C cable support 60W charging?',target='zh')
    assert client.post('/api/translation-preview',json=body).status_code==403
    response=client.post('/api/translation-preview',headers=HEADERS,json=body)
    assert response.status_code==200 and response.json()['data']['translation']==p.answer
    assert len(client.get('/api/tickets',headers=HEADERS).json()['data'])==1
    assert client.post('/api/translation-preview',headers=HEADERS,json={**body,'target':'en'}).status_code==422


@pytest.mark.parametrize('answer',['支持100W充电。','这条线支持充电。','This is a cable.', '这条线支持60W充电。'])
def test_invalid_numeric_language_or_identifier_translation_is_rejected(tmp_path,answer):
    app,client,ticket,p=make_translation_client(tmp_path,Provider(answer))
    response=translate(client,ticket)
    assert response.status_code==503 and response.json()['errors'][0]['code']=='TRANSLATION_INVALID'
    assert not app.state.translator.cache
    with app.state.store.transaction() as db:
        assert app.state.store.all(db,'model_calls')[0]['state']=='usage_recorded'


def test_unauthorized_ticket_and_invalid_payload_do_not_call_provider(tmp_path):
    app,client,ticket,p=make_translation_client(tmp_path,order_id='TEST-O4')
    body=dict(context_version=1,target='zh',message_index=0)
    path='/api/tickets/'+ticket['id']+'/translation'
    assert client.post(path,json=body).status_code==403
    assert client.post(path,headers={'Authorization':'Bearer test-limited-local'},json=body).status_code==403
    assert client.post(path,headers=HEADERS,json={**body,'text':'extra'}).status_code==422
    assert client.post(path,headers=HEADERS,json={**body,'message_index':10}).status_code==422
    assert p.calls==0


def test_stale_context_before_and_during_translation_is_rejected(tmp_path):
    app,client,ticket,p=make_translation_client(tmp_path)
    p.callback=lambda:client.patch('/api/tickets/'+ticket['id'],headers=HEADERS,json=dict(version=1,message='New customer question'))
    response=translate(client,ticket)
    assert response.status_code==409 and response.json()['errors'][0]['code']=='STALE_CONTEXT'
    assert not app.state.translator.cache
    assert translate(client,ticket).status_code==409 and p.calls==1


def test_supervisor_stop_blocks_even_cached_translation(tmp_path):
    app,client,ticket,p=make_translation_client(tmp_path)
    assert translate(client,ticket).status_code==200
    with app.state.store.transaction() as db:
        app.state.store.put(db,'pilot_settings','pilot',dict(id='pilot',version=1,stopped=True))
    response=translate(client,ticket)
    assert response.status_code==503 and response.json()['errors'][0]['code']=='PILOT_STOPPED'
    assert p.calls==1


@pytest.mark.parametrize('config,code',[(ModelConfig(),'NOT_CONFIGURED'),(ModelConfig(api_key='test',translation_enabled=False),'TRANSLATION_DISABLED')])
def test_configuration_failure_retains_original(tmp_path,config,code):
    app,client,ticket,p=make_translation_client(tmp_path,config=config)
    response=translate(client,ticket)
    assert response.status_code==503 and response.json()['errors'][0]['code']==code
    assert p.calls==0


def test_budget_is_shared_and_denies_provider_call(tmp_path):
    from decimal import Decimal
    app,client,ticket,p=make_translation_client(tmp_path,config=ModelConfig(api_key='test',budget_cny=Decimal('.00001')))
    response=translate(client,ticket)
    assert response.status_code==429 and response.json()['errors'][0]['code']=='BUDGET_EXCEEDED' and p.calls==0


def test_translation_source_language_requires_no_model(tmp_path):
    app,client,ticket,p=make_translation_client(tmp_path,message='请提供设备型号。')
    response=translate(client,ticket)
    assert response.status_code==200 and response.json()['data']['llm_calls']==0 and p.calls==0


def test_cancelled_call_keeps_reservation_and_releases_slot(tmp_path):
    class Slow:
        async def complete(self,*args):
            await asyncio.sleep(10)
    app,client,ticket,p=make_translation_client(tmp_path,Slow())
    async def run():
        task=asyncio.create_task(app.state.translator.translate(ACTOR,ticket['id'],TranslationInput(context_version=1,target='zh',message_index=0)))
        await asyncio.sleep(.03)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):await task
    asyncio.run(run())
    assert app.state.translator.pending==0
    with app.state.store.transaction() as db:
        assert app.state.store.all(db,'model_calls')[0]['state']=='usage_unknown_reserved'
