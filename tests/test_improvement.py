import json
from copy import deepcopy
from datetime import timedelta
import pytest
from fastapi.testclient import TestClient
from app.main import create_app
from app.store import ROOT, Store, TABLES, encode
from app.domain import now, timestamp

SUP = {'Authorization': 'Bearer test-supervisor-token'}
AGENT = {'Authorization': 'Bearer test-agent-token'}
RESTRICTED = {'Authorization': 'Bearer test-limited-local'}


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(tmp_path/'s2.sqlite3', serve_frontend=False)) as c:
        yield c


def data(r):
    assert r.status_code == 200, r.text
    return r.json()['data']


def post(c, path, body=None, headers=SUP):
    return data(c.post('/api'+path, json=body, headers=headers))


def get(c, path):
    return data(c.get('/api'+path, headers=SUP))


def ticket(c, **extra):
    return post(c, '/tickets', dict(message='Can I charge TEST-D1 using this cable? The page is unclear.',
                                   source_type='local', sku='TEST-L1', device_id='TEST-D1', use_case='charging', **extra), AGENT)


def nominate(c, t, quote=None, label='instructions'):
    return post(c, '/feedback', dict(ticket_id=t['id'], message_id=t['messages'][-1]['id'],
                                     quote=quote or t['messages'][-1]['text'], label=label), AGENT)


def issue(c, count=3):
    ts = [ticket(c) for _ in range(count)]
    fs = [nominate(c, t) for t in ts]
    i = post(c, '/issues', dict(title='TEST-L1 说明缺项测试专题', feedback_ids=[f['id'] for f in fs]))
    i = post(c, f"/issues/{i['id']}/review", dict(version=i['version'], reason='核对SKU与条件，接受测试原文',
             decisions=[dict(feedback_id=f['id'], decision='accepted') for f in fs]))
    return i, fs, ts


def task(c):
    i, fs, ts = issue(c)
    h = post(c, f"/issues/{i['id']}/hypotheses", dict(version=i['version'],type='instructions',
             description='合成场景：商品页未突出充电与视频区别',support_refs=[fs[0]['id']],counter_refs=[fs[1]['id']],
             missing_checks='对照旧说明与拟修改内容，原手册已有事实但展示层需补FAQ'))
    h = post(c, f"/hypotheses/{h['id']}/review", dict(version=h['version'],decision='confirm',
             reason='测试运营角色核对版本对照，属于内容展示改进',before_source_id=fs[0]['source_snapshot']['id'],
             proposed_text=fs[0]['source_snapshot']['text']+'\nlocal FAQ highlight: This cable does not provide video output.'))
    t = post(c, '/tasks', dict(issue_id=i['id'], cause_id=h['id'], title='补充充电与视频FAQ', type='faq', owner='local-supervisor',
                            expected_change='明确充电条件和视频边界', acceptance_criteria='有来源且固定回归通过'))
    t = post(c, f"/tasks/{t['id']}/state", dict(version=t['version'], state='accepted', reason='测试运营接受任务'))
    t = post(c, f"/tasks/{t['id']}/state", dict(version=t['version'], state='in_progress', reason='开始核对版本资料'))
    return t, i, fs, ts


def released_task(c):
    t, i, fs, ts = task(c)
    text = 'This cable does not provide video output.'
    t = post(c, f"/tasks/{t['id']}/content-draft", dict(version=t['version'], text=text))
    product = get(c, '/products/TEST-L1')['product']
    rules = deepcopy(product['rules'])
    rules[0]['conditions'].append('Data rate: 480 Mbps.')
    rev = post(c, '/knowledge-revisions', dict(kind='products', object_id='TEST-L1', expected_version=product['version'],
                                             changes={'rules':rules}, source_text=t['content_draft']['after_text'],
                                             reason='测试运营确认FAQ重复强调来源事实', task_id=t['id']))
    t = post(c, f"/tasks/{t['id']}/deliver", dict(version=t['version'], reference=rev['id'], result='FAQ与商品说明草稿已交付'))
    rev = post(c, f"/knowledge-revisions/{rev['id']}/regression", dict(version=rev['version']))
    rev = post(c, f"/knowledge-revisions/{rev['id']}/publish", dict(version=rev['version'], reason='测试主管核对来源后发布'))
    t = post(c, f"/tasks/{t['id']}/activate", dict(version=t['version'], revision_id=rev['id'], effective_at=now()))
    return t, i, fs, ts, rev


def observation(c, task, **extra):
    start = task['effective_at']
    return c.post(f"/api/tasks/{task['id']}/observations", headers=SUP, json=dict(version=task['version'],
                  start_at=start, end_at=(timestamp(start)+timedelta(days=7)).isoformat(),
                  conclusion='insufficient_data', coverage_notes='合成数据，后续样本未积累', confounders='尚未确认同期变化',
                  reason='当前观察窗口未成熟', **extra))


def test_full_improvement_release_answer_observation_and_restore(client, tmp_path):
    c = client
    t, i, fs, ts, rev = released_task(c)
    assert len(ts) == 3 and i['ticket_count'] == 3
    run = post(c, f"/tickets/{ts[0]['id']}/assist", dict(version=ts[0]['version'], as_of=now()), AGENT)
    assert run['status'] == 'draft' and 'Data rate: 480 Mbps.' in run['reply_draft']
    assert run['evidence_refs'][0]['version'] == rev['id']
    obs = data(observation(c, t))
    assert obs['conclusion'] == 'insufficient_data' and obs['cohort_count'] == 0
    assert obs['rates'] is None and obs['sales_denominator'] is None
    t = next(x for x in get(c, '/tasks') if x['id'] == t['id'])
    t = post(c, f"/tasks/{t['id']}/state", dict(version=t['version'], state='done', reason='交付与回归验收通过，效果仍数据不足'))
    i = next(x for x in get(c, '/issues') if x['id'] == i['id'])
    i = post(c, f"/issues/{i['id']}/state", dict(version=i['version'], state='closed', reason='完成说明改进，未证明真实收益'))
    assert i['closure']['note'] == '人工关闭，不代表统计收益证明'
    exported = get(c, '/export')
    assert all(exported[k] for k in ('feedback','issues','tasks','observations','knowledge_revisions'))
    original = c.app.state.store
    target = tmp_path/'restored.sqlite3'
    original.backup(target)
    restored = Store(target)
    with original.transaction() as a, restored.transaction() as b:
        for table in TABLES:
            assert encode(original.all(a,table)) == encode(restored.all(b,table))
    with TestClient(create_app(target, serve_frontend=False)) as again:
        assert get(again, '/tasks')[0]['state'] == 'done'
        assert get(again, '/products/TEST-L1')['product']['version'] == rev['id']


def test_feedback_quote_dedup_permission_and_multi_message_count(client):
    c = client
    i, fs, ts = issue(c)
    assert nominate(c, ts[0])['id'] == fs[0]['id']
    t = data(c.patch('/api/tickets/'+ts[0]['id'], headers=AGENT, json={'version':ts[0]['version'],'message':'Still unclear about charging and video.'}))
    f = nominate(c, t)
    # A second topic with all four snippets still has only three unique tickets.
    j = post(c, '/issues', {'title':'多片段去重检查', 'feedback_ids':[x['id'] for x in fs]+[f['id']]})
    j = post(c, f"/issues/{j['id']}/review", {'version':1,'reason':'核对全部消息','decisions':[{'feedback_id':m['feedback_id'],'decision':'accepted'} for m in j['members']]})
    assert j['feedback_count'] == 4 and j['ticket_count'] == 3 and j['rates'] is None
    assert c.post('/api/feedback',headers=AGENT,json={'ticket_id':t['id'],'message_id':t['messages'][-1]['id'],'quote':'invented','label':'quality'}).status_code == 422
    assert c.post('/api/feedback',headers=RESTRICTED,json={'ticket_id':t['id'],'message_id':t['messages'][-1]['id'],'quote':f['quote'],'label':'quality'}).status_code == 403
    assert get(c, '/feedback')[0]['source_snapshot']['version'] == '1.0'


@pytest.mark.parametrize('field,value',[('sku','TEST-C1'),('device_id','TEST-D2'),('use_case','video'),('region','EU')])
def test_different_conditions_cannot_group(client, field, value):
    a = ticket(client)
    b = data(client.patch('/api/tickets/'+ticket(client)['id'],headers=SUP,json={'version':1,field:value}))
    fs = [nominate(client,t) for t in (a,b)]
    assert client.post('/api/issues',headers=SUP,json={'title':'错误混合专题','feedback_ids':[f['id'] for f in fs]}).status_code == 422


def test_merge_split_undo_and_stale_protection(client):
    c = client
    a, fs, _ = issue(c, 2)
    b, _, _ = issue(c, 1)
    op = post(c, f"/issues/{a['id']}/grouping", dict(version=a['version'],action='merge',other_id=b['id'],other_version=b['version'],reason='人工判断同范围'))
    assert next(i for i in get(c,'/issues') if i['id']==a['id'])['ticket_count']==3
    post(c, f"/issue-operations/{op['id']}/undo", dict(version=1,reason='测试撤回错误合并'))
    a = next(i for i in get(c,'/issues') if i['id']==a['id'])
    assert a['ticket_count']==2
    assert c.post(f"/api/issue-operations/{op['id']}/undo",headers=SUP,json={'version':2,'reason':'再次撤回'}).status_code==409
    op = post(c, f"/issues/{a['id']}/grouping", dict(version=a['version'],action='split',feedback_ids=[fs[0]['id']],title='拆分测试专题',reason='拆分同组证据'))
    post(c, f"/issue-operations/{op['id']}/undo", dict(version=1,reason='撤回测试拆分'))
    a = next(i for i in get(c,'/issues') if i['id']==a['id'])
    b = next(i for i in get(c,'/issues') if i['id']==b['id'])
    op = post(c, f"/issues/{a['id']}/grouping", dict(version=a['version'],action='merge',other_id=b['id'],other_version=b['version'],reason='再归集测试证据'))
    merged = next(i for i in get(c,'/issues') if i['id']==a['id'])
    post(c, f"/issues/{a['id']}/review", dict(version=merged['version'],reason='后续人工审核',decisions=[dict(feedback_id=fs[0]['id'],decision='rejected')]))
    assert c.post(f"/api/issue-operations/{op['id']}/undo",headers=SUP,json={'version':1,'reason':'不能覆盖后续审核'}).status_code==409


@pytest.mark.parametrize('cause_type',['instructions','quality','compatibility'])
def test_cause_confirmation_requires_extra_evidence(client, cause_type):
    c = client
    i, fs, _ = issue(c)
    h = post(c, f"/issues/{i['id']}/hypotheses", dict(version=i['version'],type=cause_type,description='测试客户反馈待核实原因',support_refs=[fs[0]['id']],counter_refs=[fs[1]['id']],missing_checks='需要额外检查'))
    assert h['state']=='unconfirmed'
    body=dict(version=1,decision='confirm',reason='仅聊天不能确认')
    assert c.post(f"/api/hypotheses/{h['id']}/review",headers=SUP,json=body).status_code==422
    if cause_type=='instructions':
        body.update(before_source_id=fs[0]['source_snapshot']['id'],proposed_text=fs[0]['source_snapshot']['text']+' local FAQ highlights charging conditions.')
    else:
        body['verification_record']='测试商品负责人检查记录：本记录为合成确认，不是实际检测。'
    h=post(c,f"/hypotheses/{h['id']}/review",body)
    assert h['state']=='confirmed' and h['reviewer']=='local-supervisor'
    assert h['counter_refs']==[fs[1]['id']]


def test_task_cannot_deliver_without_evidence_or_skip_work(client):
    t, _, _, _ = task(client)
    assert client.post(f"/api/tasks/{t['id']}/deliver",headers=SUP,json={'version':t['version'],'reference':'','result':'交付'}).status_code==422
    assert client.post(f"/api/tasks/{t['id']}/state",headers=SUP,json={'version':t['version'],'state':'done','reason':'无观察跳验收'}).status_code==409
    assert client.post(f"/api/tasks/{t['id']}/content-draft",headers=SUP,json={'version':t['version'],'text':'An invented 500W feature'}).status_code==422
    assert client.post(f"/api/tasks/{t['id']}/state",headers=SUP,json={'version':1,'state':'blocked','reason':'旧版本操作'}).status_code==409


def test_activate_rejects_unlinked_unpublished_and_future(client):
    t, _, _, _ = task(client)
    t=post(client,f"/tasks/{t['id']}/deliver",dict(version=t['version'],reference='测试交付链接',result='来源草稿已交付'))
    assert client.post(f"/api/tasks/{t['id']}/activate",headers=SUP,json=dict(version=t['version'],effective_at=now())).status_code==422
    assert client.post(f"/api/tasks/{t['id']}/activate",headers=SUP,json=dict(version=t['version'],effective_at='2099-01-01T00:00:00Z',revision_id='nonexistent')).status_code==404


def test_observation_cannot_invent_benefits_or_denominator(client):
    t, _, _, _, _ = released_task(client)
    assert observation(client,t).status_code==200
    t=next(x for x in get(client,'/tasks') if x['id']==t['id'])
    p=dict(version=t['version'],start_at=t['effective_at'],end_at=(timestamp(t['effective_at'])+timedelta(days=7)).isoformat(),
           conclusion='improvement_signal',coverage_complete=True,coverage_notes='未成熟且无后续样本',confounders='未确认其他变化',reason='不能凭任务完成报收益')
    assert client.post(f"/api/tasks/{t['id']}/observations",headers=SUP,json=p).status_code==422
    p.update(conclusion='insufficient_data',sales_denominator=100)
    assert client.post(f"/api/tasks/{t['id']}/observations",headers=SUP,json=p).status_code==422


def test_pending_dataset_100_split_and_no_family_leakage(client):
    c=client
    result=post(c,'/eval-cases/import-improvement-candidates')
    assert result['inserted']==100 and post(c,'/eval-cases/import-improvement-candidates')['inserted']==0
    rows=get(c,'/eval-cases')
    assert {s:sum(r['split']==s for r in rows) for s in ('development','validation','frozen')}=={'development':60,'validation':20,'frozen':20}
    for family in {r['scenario_family'] for r in rows}:
        assert len({r['split'] for r in rows if r['scenario_family']==family})==1
    assert all(r['state']=='pending' and r['reviewer'] is None for r in rows)
    assert c.post('/api/eval-runs',headers=SUP).status_code==422
    # Candidate self-check is not gold review and never persists approvals.
    from app.evaluation import evaluate_cases
    with c.app.state.store.transaction() as db:
        results=evaluate_cases(c.app.state.store,db,rows)
    assert all(r['status']=='PASS' for r in results), [(r['case_id'],r['issues']) for r in results if r['status']!='PASS']
    assert all(r['state']=='pending' for r in get(c,'/eval-cases'))


def reviewed_bad_case(c, expected=None):
    t=ticket(c)
    run=post(c,f"/tickets/{t['id']}/assist",dict(version=1,as_of=now()),AGENT)
    check=expected or dict(status='draft',required_fragments=['verified USB-C PD','does not provide video'],forbidden_fragments=['Your refund has been processed.'])
    prior_runs=get(c,'/eval-runs')
    row=post(c,'/eval-cases',dict(ticket_id=t['id'],run_id=run['id'],error_type='missing_condition',correction='测试校对充电与视频边界',expected=check),AGENT)
    assert row['state']=='pending' and get(c,'/eval-runs')==prior_runs
    row=post(c,f"/eval-cases/{row['id']}/review",dict(version=1,decision='approve',reason='测试主管核对完整来源断言',expected=check))
    return row


def revision(c):
    p=get(c,'/products/TEST-L1')
    return post(c,'/knowledge-revisions',dict(kind='products',object_id='TEST-L1',expected_version=p['product']['version'],
                    changes={},source_text=p['evidence']['text']+' local source clarification.',reason='测试回归发布草稿'))


def test_reviewed_bad_cases_are_in_release_gate_and_hash(client):
    c=client
    row=reviewed_bad_case(c)
    before=len(get(c,'/tickets'))
    report=post(c,'/eval-runs')
    assert report['passed']==report['planned']==1 and len(get(c,'/tickets'))==before
    rev=revision(c)
    rev=post(c,f"/knowledge-revisions/{rev['id']}/regression",dict(version=1))
    assert rev['regression']['planned']==61 and rev['regression']['passed']==61
    reviewed_bad_case(c)
    assert c.post(f"/api/knowledge-revisions/{rev['id']}/publish",headers=SUP,json={'version':rev['version'],'reason':'数据集变更应拦截'}).status_code==409
    rev=post(c,f"/knowledge-revisions/{rev['id']}/regression",dict(version=rev['version']))
    assert rev['regression']['planned']==62
    post(c,f"/knowledge-revisions/{rev['id']}/publish",dict(version=rev['version'],reason='测试完整回归通过'))
    assert get(c,'/eval-runs')[0]==report
    assert get(c,'/eval-cases')[0]['run_snapshot']['evidence_refs']==row['run_snapshot']['evidence_refs']


def test_keywords_with_wrong_refund_promise_fail_and_block_release(client,monkeypatch):
    c=client
    reviewed_bad_case(c)
    from app import evaluation
    real_assist=evaluation.assist
    def false_promise(*args,**kwargs):
        run=real_assist(*args,**kwargs)
        run['reply_draft']+=' Your refund has been processed.'
        return run
    monkeypatch.setattr(evaluation,'assist',false_promise)
    report=post(c,'/eval-runs')
    assert report['passed']==0 and 'FORBIDDEN_PROMISE' in report['results'][0]['issues']
    rev=revision(c)
    rev=post(c,f"/knowledge-revisions/{rev['id']}/regression",dict(version=1))
    assert rev['regression']['passed']==60 and rev['regression']['planned']==61
    assert c.post(f"/api/knowledge-revisions/{rev['id']}/publish",headers=SUP,json={'version':rev['version'],'reason':'关键断言失败'}).status_code==409
    assert get(c,'/products/TEST-L1')['product']['version']=='1.0'


def test_mature_observation_counts_real_imported_cohort_and_detects_rollback(client):
    c=client
    t, _, _, _, rev=released_task(c)
    start=(timestamp(now())-timedelta(days=8)).isoformat()
    end=(timestamp(now())-timedelta(days=1)).isoformat()
    store=c.app.state.store
    # Historical local fixture, never fake a future mature window in the API.
    with store.transaction() as db:
        row=store.get(db,'tasks',t['id']);row['effective_at']=start;store.put(db,'tasks',row['id'],row)
    ts=[ticket(c) for _ in range(5)]
    fs=[nominate(c,ts[0]),nominate(c,ts[1])]
    with store.transaction() as db:
        for row in ts:
            row['created_at']=(timestamp(start)+timedelta(days=1)).isoformat();store.put(db,'tickets',row['id'],row)
        for f in fs:
            f['observed_at']=(timestamp(start)+timedelta(days=1)).isoformat()
            f['state']='accepted';store.put(db,'feedback',f['id'],f)
    p=dict(version=t['version'],start_at=start,end_at=end,conclusion='improvement_signal',coverage_complete=True,
           coverage_notes='5单完整合成窗口覆盖',confounders='测试资料，仅定性观察',reason='人工测试确认观察迹象，未证明因果')
    obs=post(c,f"/tasks/{t['id']}/observations",p)
    assert obs['cohort_count']==5 and obs['related_count']==2 and obs['rates'] is None and obs['adequate']
    # Replacing the effective knowledge version invalidates a benefit claim for the old intervention.
    rollback=post(c,f"/knowledge-revisions/{rev['id']}/rollback",dict(version=rev['version'],reason='测试撤回说明修改'))
    rollback=post(c,f"/knowledge-revisions/{rollback['id']}/regression",dict(version=rollback['version']))
    post(c,f"/knowledge-revisions/{rollback['id']}/publish",dict(version=rollback['version'],reason='测试回滚发布'))
    p['version']=next(x for x in get(c,'/tasks') if x['id']==t['id'])['version']
    assert c.post(f"/api/tasks/{t['id']}/observations",headers=SUP,json=p).status_code==422
    p['conclusion']='insufficient_data'
    obs=post(c,f"/tasks/{t['id']}/observations",p)
    assert not obs['revision_current'] and not obs['adequate']


def test_parallel_member_review_rejects_duplicate_and_stale_choices(client):
    i,fs,_=issue(client)
    p=dict(version=i['version'],reason='重复审核应被拒绝',decisions=[dict(feedback_id=fs[0]['id'],decision='accepted')]*2)
    assert client.post(f"/api/issues/{i['id']}/review",headers=SUP,json=p).status_code==422
    # Adding a hypothesis updates the topic version; stale member writes cannot override it.
    post(client,f"/issues/{i['id']}/hypotheses",dict(version=i['version'],type='unknown',description='另一个待验证原因',missing_checks='仍需额外查证'))
    p['decisions']=p['decisions'][:1]
    assert client.post(f"/api/issues/{i['id']}/review",headers=SUP,json=p).status_code==409


@pytest.mark.parametrize('path',['/issues','/hypotheses','/observations','/eval-cases','/eval-runs','/issue-operations','/issue-suggestions'])
def test_improvement_supervisor_data_not_visible_to_agent(client,path):
    assert client.get('/api'+path,headers=AGENT).status_code==403
    assert client.get('/api'+path).status_code==403


def test_agent_cannot_review_cases_or_change_issues(client):
    i, fs, _=issue(client)
    assert client.post(f"/api/issues/{i['id']}/review",headers=AGENT,json={'version':i['version'],'reason':'越权审核测试','decisions':[{'feedback_id':fs[0]['id'],'decision':'accepted'}]}).status_code==403
    post(client,'/eval-cases/import-improvement-candidates')
    row=get(client,'/eval-cases')[0]
    assert client.post(f"/api/eval-cases/{row['id']}/review",headers=AGENT,json={'version':1,'decision':'approve','reason':'越权批准测试','expected':row['expected']}).status_code==403


COMPLEX=json.loads((ROOT/'tests/fixtures/edge-cases.json').read_text(encoding='utf-8'))['cases']


@pytest.mark.parametrize('case',COMPLEX,ids=[c['scenario'] for c in COMPLEX])
def test_complex_fixture_cases_are_isolated_and_pending(client,case):
    from app.evaluation import evaluate_cases
    store=client.app.state.store
    with store.transaction() as db:
        before={table:store.all(db,table) for table in TABLES}
        result=evaluate_cases(store,db,[case])[0]
        assert result['status']=='PASS', result
        assert {table:store.all(db,table) for table in TABLES}==before
    assert case['state']=='pending' and case['reviewer'] is None


def test_complex_import_idempotent_does_not_approve_gold(client):
    result=post(client,'/eval-cases/import-complex-candidates')
    assert result['inserted']==result['independent_scenarios']==16
    assert post(client,'/eval-cases/import-complex-candidates')['inserted']==0
    assert all(c['state']=='pending' for c in get(client,'/eval-cases'))


def test_archived_source_fixture_tracks_published_knowledge_version(client):
    from app.evaluation import evaluate_cases
    released_task(client)
    store=client.app.state.store
    case=next(c for c in COMPLEX if c['scenario']=='published_product_archived_source')
    with store.transaction() as db:
        before={table:store.all(db,table) for table in TABLES}
        result=evaluate_cases(store,db,[case])[0]
        assert result['status']=='PASS', result
        assert {table:store.all(db,table) for table in TABLES}==before


def test_task_reopen_preserves_every_prior_delivery_and_observation(client):
    t,_,_,_,_=released_task(client)
    obs=data(observation(client,t))
    key=t['id']
    t=next(row for row in get(client,'/tasks') if row['id']==key)
    # The first delivered cycle remains available after reopening.
    t=post(client,f"/tasks/{t['id']}/state",dict(version=t['version'],state='reopened',reason='客户仍有疑问，重新优化内容'))
    old=t['cycles'][0]
    assert old['snapshot']['deliverable']['reference'] and old['snapshot']['revision_id']
    assert old['observation_ids']==[obs['id']]
    assert t['deliverable'] is None and t['content_draft'] is None and t['revision_id'] is None
    assert get(client,'/observations')[0]==obs
    assert get(client,'/issues')[0]['state']=='investigating'


def test_cause_references_cannot_be_rejected_after_confirmation(client):
    i,fs,_=issue(client)
    h=post(client,f"/issues/{i['id']}/hypotheses",dict(version=i['version'],type='unknown',description='测试待验证原因',support_refs=[fs[0]['id']],missing_checks='仍需额外确认'))
    i=get(client,'/issues')[0]
    r=client.post(f"/api/issues/{i['id']}/review",headers=SUP,json=dict(version=i['version'],reason='拒绝已引用证据',decisions=[dict(feedback_id=fs[0]['id'],decision='rejected')]))
    assert r.status_code==409 and get(client,'/hypotheses')[0]==h
    assert get(client,'/issues')[0]['members'][0]['decision']=='accepted'


def test_clarification_bad_case_and_historical_context_snapshot(client):
    t=post(client,'/tickets',dict(message='Does this work with my iPad?',source_type='local',sku='TEST-L1',use_case='charging'),AGENT)
    run=post(client,f"/tickets/{t['id']}/assist",dict(version=1,as_of=now()),AGENT)
    assert not run['evidence_refs']
    data(client.patch('/api/tickets/'+t['id'],headers=AGENT,json=dict(version=2,device_id='TEST-D1',message='Now I know the model is TEST-D1.')))
    row=post(client,'/eval-cases',dict(ticket_id=t['id'],run_id=run['id'],error_type='wrong_status',correction='应先请求准确型号',expected=dict(status='need_clarification',required_fragments=['exact device model'],forbidden_fragments=[])),AGENT)
    assert row['ticket']['device_id'] is None and row['ticket']['message']==t['messages'][-1]['text']
    assert row['evidence_refs'][0]['evidence_type']=='behavior_specification_not_product_fact'
    post(client,f"/eval-cases/{row['id']}/review",dict(version=1,decision='approve',reason='测试主管审核澄清行为依据',expected=row['expected']))
    assert post(client,'/eval-runs')['passed']==1


def test_gold_revision_keeps_old_gate_until_new_candidate_approved(client):
    old=reviewed_bad_case(client)
    report=post(client,'/eval-runs')
    bad=dict(status='draft',required_fragments=['invented assertion'],forbidden_fragments=[])
    candidate=post(client,f"/eval-cases/{old['id']}/revise",dict(version=old['version'],reason='测试审核发现需要修改断言',expected=bad))
    assert candidate['state']=='pending' and candidate['supersedes']==old['id']
    assert post(client,'/eval-runs')['passed']==1
    assert client.post(f"/api/eval-cases/{old['id']}/revise",headers=SUP,json=dict(version=old['version'],reason='重复创建修订',expected=bad)).status_code==409
    new=post(client,f"/eval-cases/{candidate['id']}/review",dict(version=1,decision='approve',reason='测试批准新断言，验证回归会失败',expected=bad))
    assert post(client,'/eval-runs')['failed']==1
    archived=next(c for c in get(client,'/eval-cases') if c['id']==old['id'])
    assert archived['state']=='superseded' and archived['expected']==old['expected']
    assert get(client,'/eval-runs')[0]==report
    fix=post(client,f"/eval-cases/{new['id']}/revise",dict(version=new['version'],reason='纠正上轮测试错误金标',expected=old['expected']))
    post(client,f"/eval-cases/{fix['id']}/review",dict(version=1,decision='approve',reason='核对原始依据后恢复正确断言',expected=old['expected']))
    assert post(client,'/eval-runs')['passed']==1


def test_rejected_gold_revision_never_removes_original(client):
    old=reviewed_bad_case(client)
    candidate=post(client,f"/eval-cases/{old['id']}/revise",dict(version=old['version'],reason='测试候选修订',expected=old['expected']))
    post(client,f"/eval-cases/{candidate['id']}/review",dict(version=1,decision='reject',reason='证据不足，拒绝修订',expected=old['expected']))
    assert next(c for c in get(client,'/eval-cases') if c['id']==old['id'])==old


def test_expected_error_must_actually_happen(client):
    from app.evaluation import evaluate_cases
    case=deepcopy(COMPLEX[0]);case['expected']['error_code']='FORBIDDEN'
    with client.app.state.store.transaction() as db:
        result=evaluate_cases(client.app.state.store,db,[case])[0]
    assert result['status']=='FAIL' and 'EXPECTED_ERROR_NOT_RAISED' in result['issues']


def test_process_effective_date_cannot_predate_delivery(client):
    i,_,_=issue(client)
    t=post(client,'/tasks',dict(issue_id=i['id'],title='测试流程调整',type='process',owner='local-supervisor',expected_change='调整人工交接',acceptance_criteria='提供操作记录'))
    for state in ('accepted','in_progress'):
        t=post(client,f"/tasks/{t['id']}/state",dict(version=t['version'],state=state,reason='测试任务流转'))
    t=post(client,f"/tasks/{t['id']}/deliver",dict(version=t['version'],reference='测试流程交付记录',result='人工交接流程已调整'))
    old=(timestamp(t['deliverable']['at'])-timedelta(seconds=1)).isoformat()
    p=dict(version=t['version'],effective_at=old,evidence='测试主管确认执行流程的记录')
    assert client.post(f"/api/tasks/{t['id']}/activate",headers=SUP,json=p).status_code==422


def test_replay_second_clarification_preserves_prior_rounds(client):
    t=post(client,'/tickets',dict(message='Does it work with my iPad?',source_type='local',sku='TEST-L1',use_case='charging'),AGENT)
    post(client,f"/tickets/{t['id']}/assist",dict(version=1,as_of=now()),AGENT)
    run=post(client,f"/tickets/{t['id']}/assist",dict(version=2,as_of=now()),AGENT)
    assert run['status']=='escalated' and run['prior_clarification_rounds']==1
    row=post(client,'/eval-cases',dict(ticket_id=t['id'],run_id=run['id'],error_type='wrong_status',correction='两轮缺型号应保留交人工出口',expected=dict(status='escalated',required_fragments=['exact device model'],forbidden_fragments=[])),AGENT)
    post(client,f"/eval-cases/{row['id']}/review",dict(version=1,decision='approve',reason='测试审核两轮澄清上下文',expected=row['expected']))
    assert post(client,'/eval-runs')['passed']==1
