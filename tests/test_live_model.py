import asyncio
from dataclasses import replace
from decimal import Decimal
import json
import httpx
import pytest
from fastapi.testclient import TestClient
from app.main import create_app
from app.model_config import ModelConfig
from app.provider import DeepSeekProvider,ProviderError,Completion
from app.budget import Budget
from app.store import Store,ROOT
from app.live_assist import LiveAssist
from app import services
from app.quality import check_draft,build_messages,SYSTEM_PROMPT

CFG=ModelConfig(api_key='test-secret',enabled=True)
ACTOR=dict(id='local-agent',role='agent',orders=['TEST-O1','TEST-O2','TEST-O3','TEST-O4'])
CASES=json.loads((ROOT/'tests/fixtures/cases.json').read_text(encoding='utf-8'))


def stream(content='{"ok":true}',finish='stop',done=True,usage=True):
    chunks=[dict(model='deepseek-flash',choices=[dict(delta=dict(content=content),finish_reason=None)]),
            dict(choices=[dict(delta={},finish_reason=finish)])]
    if usage:chunks.append(dict(choices=[],usage=dict(prompt_tokens=100,completion_tokens=10)))
    return '\n\n'.join('data: '+json.dumps(x) for x in chunks)+ ('\n\ndata: [DONE]\n\n' if done else '\n\n')


def provider(body,status=200):
    def handle(request):
        sent=json.loads(request.content)
        assert sent['thinking']=={'type':'disabled'}
        assert sent['stream_options']=={'include_usage':True}
        return httpx.Response(status,text=body,headers={'content-type':'text/event-stream'})
    return DeepSeekProvider(CFG,httpx.MockTransport(handle))


def test_actual_stream_usage_and_non_thinking():
    result=asyncio.run(provider(stream()).complete([dict(role='user',content='json')]))
    assert result.content=='{"ok":true}'
    assert result.metrics['cost']=='0.00028'
    assert result.metrics['usage_complete'] and result.metrics['provider_ttft_ms'] is not None


@pytest.mark.parametrize('body,code',[(stream(done=False),'PROVIDER_INCOMPLETE'),
    (stream(finish='length'),'PROVIDER_INCOMPLETE'),(stream(content=''),'PROVIDER_EMPTY'),
    ('data: []\n\n','PROVIDER_INVALID_STREAM'),('data: not-json\n\n','PROVIDER_INVALID_STREAM')])
def test_stream_failures_are_not_drafts(body,code):
    with pytest.raises(ProviderError) as exc:
        asyncio.run(provider(body).complete([]))
    assert exc.value.code==code
    assert 'test-secret' not in str(exc.value.metrics)


@pytest.mark.parametrize('status,code',[(401,'PROVIDER_AUTH'),(402,'PROVIDER_BALANCE'),(403,'PROVIDER_FORBIDDEN'),(429,'PROVIDER_RATE_LIMIT'),(500,'PROVIDER_HTTP_ERROR')])
def test_http_failures_without_body_or_secret(status,code):
    with pytest.raises(ProviderError) as exc:
        asyncio.run(provider('sensitive provider body',status).complete([]))
    assert exc.value.code==code and exc.value.metrics['llm_calls']==1
    assert exc.value.metrics['cost'] is None
    assert 'sensitive' not in str(exc.value.metrics)


def test_total_deadline():
    class Slow(httpx.AsyncByteStream):
        async def __aiter__(self):
            await asyncio.sleep(.1)
            yield b'data: [DONE]\n\n'
    p=DeepSeekProvider(CFG,httpx.MockTransport(lambda r:httpx.Response(200,stream=Slow())))
    with pytest.raises(ProviderError) as exc:asyncio.run(p.complete([],deadline_seconds=.02))
    assert exc.value.code=='PROVIDER_TIMEOUT' and not exc.value.metrics['usage_complete']


def make_store_ticket(tmp_path,case=CASES[0]):
    store=Store(tmp_path/'live.sqlite3');store.seed()
    with store.transaction() as db:t=services.create_ticket(store,db,ACTOR,case['ticket'])
    return store,t


class BaselineProvider:
    def __init__(self,store=None,ticket=None,change=False):self.store,self.ticket,self.change=store,ticket,change
    async def complete(self,messages,deadline_seconds=None):
        trusted=json.loads(messages[1]['content'])['trusted_rule_result']
        if self.change:
            with self.store.transaction() as db:
                services.change_ticket(self.store,db,ACTOR,self.ticket['id'],dict(version=1,draft='my concurrent human edit'))
        content=json.dumps({k:trusted[k] for k in ('status','reply_draft','summary','missing_fields')}|dict(claims=[]))
        return Completion(content,dict(llm_calls=1,cost='0.001',usage_complete=True,input_tokens=100,output_tokens=100,provider_ttft_ms=1,provider_total_ms=2))


def test_candidate_preserves_human_draft(tmp_path):
    store,t=make_store_ticket(tmp_path)
    with store.transaction() as db:t=services.change_ticket(store,db,ACTOR,t['id'],dict(version=1,draft='existing human draft'))
    result=asyncio.run(LiveAssist(store,CFG,BaselineProvider()).run(ACTOR,t['id'],dict(version=t['version'],as_of=CASES[0]['assist']['as_of'])))
    assert result['quality']['status']=='REVIEW' and not result['draft_adoptable']
    with store.transaction() as db:assert store.get(db,'tickets',t['id'])['draft']=='existing human draft'


def test_stale_result_is_not_adoptable(tmp_path):
    store,t=make_store_ticket(tmp_path)
    result=asyncio.run(LiveAssist(store,CFG,BaselineProvider(store,t,True)).run(ACTOR,t['id'],CASES[0]['assist']))
    assert result['error_code']=='STALE_CONTEXT'
    with store.transaction() as db:assert store.get(db,'tickets',t['id'])['draft']=='my concurrent human edit'


def test_safety_bypass_does_not_call_provider(tmp_path):
    case=next(c for c in CASES if c['scenario']=='safety')
    store,t=make_store_ticket(tmp_path,case)
    class Never:
        async def complete(self,*args):raise AssertionError('Safety called provider')
    result=asyncio.run(LiveAssist(store,CFG,Never()).run(ACTOR,t['id'],case['assist']))
    assert result['llm_calls']==0 and result['execution_mode']=='rules_bypass'


def test_unknown_usage_remains_reserved_after_restart(tmp_path):
    store=Store(tmp_path/'budget.sqlite3');budget=Budget(store,replace(CFG,budget_cny=Decimal('.015')))
    key=budget.reserve([]);budget.settle(key,dict(llm_calls=1,cost=None))
    reopened=Budget(Store(store.path),budget.config)
    assert reopened.summary()['unknown_usage_calls']==1
    with pytest.raises(Exception) as exc:reopened.reserve([])
    assert exc.value.code=='BUDGET_EXCEEDED'


def test_not_sent_reservation_released(tmp_path):
    b=Budget(Store(tmp_path/'budget.sqlite3'),CFG);key=b.reserve([]);b.settle(key,dict(llm_calls=0))
    assert b.summary()['held_or_estimated']=='0'


def test_endpoint_disabled_and_no_key_exposure(tmp_path):
    with TestClient(create_app(tmp_path/'api.sqlite3',False,replace(CFG,enabled=False))) as c:
        headers={'Authorization':'Bearer test-agent-token'}
        t=c.post('/api/tickets',headers=headers,json=CASES[0]['ticket']).json()['data']
        assert c.post(f"/api/tickets/{t['id']}/assist-live",headers=headers,json=CASES[0]['assist']).status_code==503
        assert 'test-secret' not in c.get('/api/model-status',headers=headers).text
        assert c.get('/api/evaluation-reports',headers=headers).status_code==403


def test_positive_action_claim_rejected_but_negation_allowed():
    baseline=dict(status='draft',missing_fields=[],evidence_refs=[],item_results=[],reply_draft='No refund has been approved.')
    draft=dict(status='draft',summary='待审核',missing_fields=[],claims=[],reply_draft='We have approved your refund.')
    assert 'UNAUTHORIZED_ACTION_CLAIM' in check_draft(json.dumps(draft),baseline)[1]['issues']
    draft['reply_draft']='No refund has been approved or executed.'
    assert check_draft(json.dumps(draft),baseline)[1]['status']=='REVIEW'


def test_invented_citation_rejected():
    baseline=dict(status='draft',missing_fields=[],evidence_refs=[],item_results=[],reply_draft='Unknown.')
    draft=dict(status='draft',summary='待审核',missing_fields=[],claims=[dict(text='65W',evidence_ids=['FAKE'])],reply_draft='65W is supported.')
    assert 'INVALID_CITATION' in check_draft(json.dumps(draft),baseline)[1]['issues']


def test_verbatim_claim_still_requires_a_source_id():
    baseline=dict(status='draft',missing_fields=[],evidence_refs=[dict(id='SRC',text='Maximum power is 60W.')],
                  item_results=[],reply_draft='Maximum power is 60W.')
    draft=dict(status='draft',summary='参数',missing_fields=[],claims=[dict(text='Maximum power is 60W.',evidence_ids=[])],reply_draft='Maximum power is 60W.')
    assert 'MISSING_CITATION' in check_draft(json.dumps(draft),baseline)[1]['issues']
    baseline['evidence_refs']=[]
    assert 'CLAIMS_WITHOUT_EVIDENCE' in check_draft(json.dumps(draft),baseline)[1]['issues']
    draft['claims']=[]
    assert check_draft(json.dumps(draft),baseline)[1]['status']=='REVIEW'


def test_json_mode_metadata_is_not_part_of_draft_schema():
    baseline=dict(status='escalated',missing_fields=[],evidence_refs=[],item_results=[],reply_draft='Please contact human support.')
    draft=dict(type='json_object',status='escalated',summary='交人工',missing_fields=[],claims=[],reply_draft='Please contact human support for help.')
    assert 'type, response_format, or model' in SYSTEM_PROMPT
    assert check_draft(json.dumps(draft),baseline)[1]['issues']==['INVALID_SCHEMA']
    del draft['type']
    assert check_draft(json.dumps(draft),baseline)[1]['status']=='REVIEW'


def test_unsupported_handoff_promise_rejected():
    baseline=dict(status='escalated',missing_fields=[],evidence_refs=[],item_results=[],reply_draft='Please contact human support.')
    draft=dict(status='escalated',summary='交人工',missing_fields=[],claims=[],reply_draft='Our team will follow up with you.')
    assert 'UNRECORDED_HANDOFF_PROMISE' in check_draft(json.dumps(draft),baseline)[1]['issues']


def test_negated_assignment_is_not_a_handoff_promise():
    baseline=dict(status='escalated',missing_fields=[],evidence_refs=[],item_results=[],reply_draft='Please contact human support.')
    draft=dict(status='escalated',summary='交人工',missing_fields=[],claims=[],
               reply_draft='Please contact human support. No case has been assigned yet.')
    assert check_draft(json.dumps(draft),baseline)[1]['status']=='REVIEW'
    draft['reply_draft']='Please contact human support. Your case has been assigned.'
    assert 'UNRECORDED_HANDOFF_PROMISE' in check_draft(json.dumps(draft),baseline)[1]['issues']


def test_deferred_topic_is_not_silently_dropped():
    baseline=dict(status='escalated',missing_fields=[],evidence_refs=[],
                  item_results=[dict(intent='safety',state='handoff'),dict(intent='order_status',state='deferred')],
                  reply_draft='Please stop using the product and contact human support.')
    draft=dict(status='escalated',summary='待人工',missing_fields=[],claims=[],reply_draft=baseline['reply_draft'])
    assert 'MISSING_DEFERRED_TOPIC' in check_draft(json.dumps(draft),baseline)[1]['issues']
    draft['reply_draft']+=' Your tracking question remains pending for human review after the safety review.'
    assert check_draft(json.dumps(draft),baseline)[1]['status']=='REVIEW'
    draft['reply_draft']+=' Our team will follow up with you.'
    assert 'UNRECORDED_HANDOFF_PROMISE' in check_draft(json.dumps(draft),baseline)[1]['issues']


def test_no_evidence_output_contract_has_empty_claims(tmp_path):
    from app.assist import assist
    case=next(case for case in CASES if case['scenario']=='connector_only')
    store,t=make_store_ticket(tmp_path,case)
    with store.transaction() as db:baseline=assist(store,db,ACTOR,t['id'],case['assist'],persist=False)
    contract=json.loads(build_messages(t,baseline)[1]['content'])['output_contract']
    assert contract['claims_must_be_empty'] and contract['allowed_evidence_ids']==[]
    assert contract['json_example']['claims']==[]


def test_rewrite_context_excludes_unrelated_source_text(tmp_path):
    from app.assist import assist
    case=next(case for case in CASES if case['scenario']=='unknown_device')
    store,t=make_store_ticket(tmp_path,case)
    with store.transaction() as db:baseline=assist(store,db,ACTOR,t['id'],case['assist'],persist=False)
    saved=json.dumps(baseline,sort_keys=True)
    messages=build_messages(t,baseline);payload=json.loads(messages[1]['content'])
    assert 'Maximum rated power' not in messages[1]['content'] and '60 W' not in messages[1]['content']
    assert all('text' not in ref for ref in payload['trusted_rule_result']['evidence_refs'])
    assert all('evidence_refs' not in item for item in payload['trusted_rule_result']['item_results'])
    assert payload['output_contract']['claims_must_be_empty']
    assert json.dumps(baseline,sort_keys=True)==saved


def test_tracking_timestamp_and_elapsed_days_cannot_be_simplified():
    reply='The snapshot shows in_transit as of 2026-09-29T12:00:00+00:00. Last event was 9.17 days before the reference time.'
    baseline=dict(status='draft',missing_fields=[],evidence_refs=[],item_results=[],reply_draft=reply)
    draft=dict(status='draft',summary='快照',missing_fields=[],claims=[],reply_draft='The snapshot shows in transit as of 2026-09-29.')
    assert 'TIME_PRECISION_LOST' in check_draft(json.dumps(draft),baseline)[1]['issues']
    draft['reply_draft']=reply
    assert check_draft(json.dumps(draft),baseline)[1]['status']=='REVIEW'


def test_tracking_snapshot_evidence_is_redacted_and_versioned(tmp_path):
    from app.assist import assist
    import hashlib
    case=next(case for case in CASES if case['scenario']=='tracking_snapshot')
    store,t=make_store_ticket(tmp_path,case)
    with store.transaction() as db:baseline=assist(store,db,ACTOR,t['id'],case['assist'],persist=False)
    ref=baseline['evidence_refs'][0]
    assert ref['checksum']==hashlib.sha256(ref['text'].encode()).hexdigest()
    assert set(json.loads(ref['text']))=={'version','status','observed_at','last_tracking_at','max_age_seconds'}
    content=build_messages(t,baseline)[1]['content']
    assert 'customer_ref' not in content and 'paid' not in content and 'TEST-CUSTOMER' not in content
    with store.transaction() as db:
        order=store.get(db,'orders',case['ticket']['order_id']);order['version']+=1;order['status']='delivered'
        store.put(db,'orders',order['id'],order)
        changed=assist(store,db,ACTOR,t['id'],case['assist'],persist=False)
    assert changed['evidence_refs'][0]['id']!=ref['id']
    assert ref['version']=='1' and json.loads(ref['text'])['status']=='in_transit'


def test_rule_clarification_asks_only_missing_fields(tmp_path):
    from app.assist import assist
    store,t=make_store_ticket(tmp_path)
    with store.transaction() as db:r=assist(store,db,ACTOR,t['id'],CASES[0]['assist'],persist=False)
    assert r['missing_fields']==['device_id']
    assert 'device model' in r['reply_draft'] and 'whether' not in r['reply_draft']


def test_redundant_use_case_question_rejected():
    baseline=dict(status='need_clarification',missing_fields=['device_id'],evidence_refs=[],item_results=[],reply_draft='Please confirm the exact device model.')
    draft=dict(status='need_clarification',summary='缺型号',missing_fields=['device_id'],claims=[],reply_draft='Please confirm whether you need charging or video.')
    assert 'ASKED_KNOWN_USE_CASE' in check_draft(json.dumps(draft),baseline)[1]['issues']


def test_live_endpoint_enabled_with_safe_candidate(tmp_path):
    with TestClient(create_app(tmp_path/'endpoint.sqlite3',False,CFG,BaselineProvider())) as c:
        headers={'Authorization':'Bearer test-agent-token'}
        t=c.post('/api/tickets',headers=headers,json=CASES[0]['ticket']).json()['data']
        response=c.post(f"/api/tickets/{t['id']}/assist-live",headers=headers,json=CASES[0]['assist'])
        assert response.status_code==200
        assert response.json()['data']['quality']['status']=='REVIEW'
        assert c.get(f"/api/tickets/{t['id']}",headers=headers).json()['data']['draft']==''


def test_candidate_load_rechecks_without_overwriting_human_draft(tmp_path):
    with TestClient(create_app(tmp_path/'load.sqlite3',False,CFG,BaselineProvider())) as c:
        headers={'Authorization':'Bearer test-agent-token'}
        t=c.post('/api/tickets',headers=headers,json=CASES[0]['ticket']).json()['data']
        r=c.post(f"/api/tickets/{t['id']}/assist-live",headers=headers,json=CASES[0]['assist']).json()['data']
        loaded=c.post(f"/api/tickets/{t['id']}/candidates/{r['id']}/load-for-editing",headers=headers,json=dict(version=1))
        assert loaded.status_code==200 and loaded.json()['data']['reply_draft']==r['reply_draft']
        assert loaded.json()['data']['human_review']=='required'
        unchanged=c.get(f"/api/tickets/{t['id']}",headers=headers).json()['data']
        assert unchanged['draft']=='' and unchanged['version']==1
        assert c.post(f"/api/tickets/{t['id']}/candidates/{r['id']}/load-for-editing",json=dict(version=1)).status_code==403


@pytest.mark.parametrize('change',['source','order','ticket','checker','different_ticket'])
def test_stale_or_wrong_candidate_cannot_be_loaded(tmp_path,change):
    case=next(case for case in CASES if case['scenario']==('tracking_snapshot' if change=='order' else 'conditional_match'))
    with TestClient(create_app(tmp_path/'stale-load.sqlite3',False,CFG,BaselineProvider())) as c:
        headers={'Authorization':'Bearer test-agent-token'}
        t=c.post('/api/tickets',headers=headers,json=case['ticket']).json()['data']
        r=c.post(f"/api/tickets/{t['id']}/assist-live",headers=headers,json=case['assist']).json()['data']
        expected=1
        store=c.app.state.store
        if change=='ticket':
            c.patch(f"/api/tickets/{t['id']}",headers=headers,json=dict(version=1,message='Changed customer question'))
            expected=2
        elif change=='different_ticket':
            t=c.post('/api/tickets',headers=headers,json=case['ticket']).json()['data']
        else:
            with store.transaction() as db:
                if change=='source':
                    source=store.get(db,'sources','SRC-TEST-L1');source['status']='retired';store.put(db,'sources',source['id'],source)
                elif change=='order':
                    order=store.get(db,'orders',case['ticket']['order_id']);order['version']+=1;order['status']='delivered';store.put(db,'orders',order['id'],order)
                else:
                    trace=store.get(db,'runs',r['id']);raw=json.loads(trace['raw_model_content'])
                    raw['claims']=[dict(text=trace['baseline_reply_draft'],evidence_ids=[])]
                    trace['raw_model_content']=json.dumps(raw);store.put(db,'runs',trace['id'],trace)
        result=c.post(f"/api/tickets/{t['id']}/candidates/{r['id']}/load-for-editing",headers=headers,json=dict(version=expected))
        assert result.status_code==(403 if change=='different_ticket' else 409)
        assert c.get(f"/api/tickets/{t['id']}",headers=headers).json()['data']['draft']==''


def test_report_detail_permission_and_no_path_interpolation(tmp_path):
    with TestClient(create_app(tmp_path/'reports.sqlite3',False,CFG)) as c:
        agent={'Authorization':'Bearer test-agent-token'}
        supervisor={'Authorization':'Bearer test-supervisor-token'}
        assert c.get('/api/evaluation-reports/unknown',headers=agent).status_code==403
        assert c.get('/api/evaluation-reports/unknown',headers=supervisor).status_code==404


def test_shared_budget_blocks_across_runtimes(tmp_path):
    shared=Store(tmp_path/'shared.sqlite3')
    cfg=replace(CFG,budget_cny=Decimal('.015'))
    a=LiveAssist(Store(tmp_path/'a.sqlite3'),cfg,budget_store=shared)
    b=LiveAssist(Store(tmp_path/'b.sqlite3'),cfg,budget_store=shared)
    a.budget.reserve([])
    with pytest.raises(Exception) as exc:b.budget.reserve([])
    assert exc.value.code=='BUDGET_EXCEEDED'


def test_source_retired_during_generation_is_rejected(tmp_path):
    case=next(c for c in CASES if c['scenario']=='conditional_match')
    store,t=make_store_ticket(tmp_path,case)
    class Retiring(BaselineProvider):
        async def complete(self,messages,deadline_seconds=None):
            with store.transaction() as db:
                source=store.get(db,'sources','SRC-TEST-L1')
                source['status']='retired'
                store.put(db,'sources',source['id'],source)
            return await super().complete(messages,deadline_seconds)
    result=asyncio.run(LiveAssist(store,CFG,Retiring()).run(ACTOR,t['id'],case['assist']))
    assert result['error_code']=='STALE_EVIDENCE'
    assert result['quality']['status']=='ERROR'
    with store.transaction() as db:assert store.get(db,'tickets',t['id'])['draft']==''


def test_order_changes_during_generation_is_rejected(tmp_path):
    case=next(c for c in CASES if c['scenario']=='boundary_equal')
    store,t=make_store_ticket(tmp_path,case)
    class ChangedOrder(BaselineProvider):
        async def complete(self,messages,deadline_seconds=None):
            with store.transaction() as db:
                order=store.get(db,'orders','TEST-O1')
                order['delivered_at']='2026-08-01T00:00:00+00:00'
                store.put(db,'orders',order['id'],order)
            return await super().complete(messages,deadline_seconds)
    result=asyncio.run(LiveAssist(store,CFG,ChangedOrder()).run(ACTOR,t['id'],case['assist']))
    assert result['error_code']=='STALE_EVIDENCE'


def test_cancelled_live_call_retains_unknown_usage_reservation(tmp_path):
    store,t=make_store_ticket(tmp_path)
    class Hangs:
        def __init__(self):self.started=asyncio.Event()
        async def complete(self,messages,deadline_seconds=None):
            self.started.set()
            await asyncio.Future()
    async def exercise():
        provider=Hangs();runtime=LiveAssist(store,CFG,provider)
        task=asyncio.create_task(runtime.run(ACTOR,t['id'],CASES[0]['assist']))
        await provider.started.wait();task.cancel()
        result=await task
        return result,runtime
    result,runtime=asyncio.run(exercise())
    assert result['cancelled'] and result['error_code']=='RUN_CANCELLED'
    assert runtime.budget.summary()['unknown_usage_calls']==1
    assert runtime.waiting==0 and t['id'] not in runtime.busy_tickets
    with store.transaction() as db:assert store.get(db,'runs',result['id'])['cancelled']


def test_health_and_model_status_do_not_misstate_connection(tmp_path):
    with TestClient(create_app(tmp_path/'health.sqlite3',False,replace(CFG,enabled=False))) as c:
        health=c.get('/api/health').json()['data']
        assert health['provider']=='rules_and_optional_deepseek'
        assert health['model_experiment_enabled'] is False
        status=c.get('/api/model-status',headers={'Authorization':'Bearer test-agent-token'}).json()['data']
        assert 'last_successful_probe_at' in status
        assert 'connection_verified' not in status


def test_database_and_local_tokens_use_local_settings(tmp_path,monkeypatch):
    custom_db=tmp_path/'configured.sqlite3'
    monkeypatch.setenv('SELLERSUPPORT_DB',str(custom_db))
    monkeypatch.setenv('SELLERSUPPORT_AGENT_TOKEN','custom-local-agent')
    with TestClient(create_app(serve_frontend=False,model_config=replace(CFG,enabled=False))) as c:
        assert c.app.state.store.path==custom_db
        assert c.get('/api/session',headers={'Authorization':'Bearer custom-local-agent'}).status_code==200
        assert c.get('/api/session',headers={'Authorization':'Bearer test-agent-token'}).status_code==403


def test_project_env_file_supplies_database_and_local_tokens(tmp_path,monkeypatch):
    monkeypatch.delenv('SELLERSUPPORT_DB',raising=False)
    monkeypatch.delenv('SELLERSUPPORT_AGENT_TOKEN',raising=False)
    monkeypatch.delenv('SELLERSUPPORT_SUPERVISOR_TOKEN',raising=False)
    monkeypatch.setattr('app.model_config.ROOT',tmp_path)
    expected=tmp_path/'from-env.sqlite3'
    (tmp_path/'.env').write_text('SELLERSUPPORT_DB='+str(expected)+'\nSELLERSUPPORT_AGENT_TOKEN=from-env-agent\n',encoding='utf-8')
    with TestClient(create_app(serve_frontend=False,model_config=replace(CFG,enabled=False))) as c:
        assert c.app.state.store.path==expected
        assert c.get('/api/session',headers={'Authorization':'Bearer from-env-agent'}).status_code==200

