"""Small official-provider benchmark against a snapshot of published example knowledge."""
import argparse
import asyncio
import json
import sys
from collections import Counter
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from app.store import Store
from app.model_config import ModelConfig
from app.live_assist import LiveAssist
from app import services
from app.domain import now
from app.quality import PROMPT_VERSION,SCORER_VERSION
from app.knowledge import fingerprint,checker_hash,digest
from app.evaluation import apply_case_fixtures


def summarize(rows):
    times=sorted(r['total_ms'] for r in rows)
    first=sorted(r['provider_ttft_ms'] for r in rows if r.get('provider_ttft_ms') is not None)
    def distribution(values):
        return dict(p50=values[(len(values)-1)//2] if values else None,p95=values[min(len(values)-1,int(len(values)*.95))] if values else None)
    return dict(attempted=len(rows),llm_calls=sum(r.get('llm_calls',0) for r in rows),
                hard_checks_passed=sum(bool(r.get('quality',{}).get('hard_checks_passed')) for r in rows),
                quality_counts=dict(Counter(r.get('quality',{}).get('status','ERROR') for r in rows)),
                estimated_cost_cny=str(sum((Decimal(r['cost']) for r in rows if r.get('cost') is not None),Decimal(0))),
                input_tokens=sum(r.get('input_tokens') or 0 for r in rows),output_tokens=sum(r.get('output_tokens') or 0 for r in rows),
                end_to_end_ms=distribution(times),provider_ttft_ms=distribution(first))


async def evaluate(args):
    source=(ROOT/args.db).resolve()
    if not source.is_relative_to(ROOT/'storage') or not source.is_file():
        raise SystemExit('Use an existing project storage database.')
    config=ModelConfig.load()
    if not config.api_key:raise SystemExit('Model key is not configured.')
    baseline=Store(source)
    with baseline.transaction() as db:
        if any(x['source_type']!='local' for table in ('products','sources','orders','policies') for x in baseline.all(db,table)):
            raise SystemExit('Only local datasets are permitted in this benchmark.')
        product=baseline.get(db,'products','TEST-L1')
        if not product['version'].startswith('KNOW-'):
            raise SystemExit('TEST-L1 must have a published revision first.')
        baseline_hash=fingerprint(baseline,db)
    base=json.loads((ROOT/'tests/fixtures/cases.json').read_text(encoding='utf-8'))
    complex_cases=json.loads((ROOT/'tests/fixtures/edge-cases.json').read_text(encoding='utf-8'))['cases']
    selection=[dict(c,as_of=c['assist']['as_of']) for c in base if c['scenario'] in {'conditional_match','video_unsupported','power_not_guarantee'}]
    selection.extend(c for c in complex_cases if c['scenario'] in {'safety_refund_and_tracking','clarification_then_explicit_model','return_and_tracking_both_preserved'})
    if args.multiturn:
        for scenario,last_agent in [('v9_customer_agent_customer',False),('v9_last_agent_not_customer',True)]:
            c=dict(id=scenario,scenario=scenario,ticket=dict(message='Can TEST-L1 charge TEST-D1?',source_type='local',sku='TEST-L1',device_id='TEST-D1',use_case='charging',region='US',channel='direct'),as_of='2026-09-29T12:00:00+00:00',multiturn=True,last_agent=last_agent)
            selection.append(c)
    folder=ROOT/'storage'/('answer-evaluation-'+uuid4().hex[:12]);folder.mkdir()
    frozen=folder/'baseline.sqlite3';baseline.backup(frozen)
    snapshot=Store(frozen)
    with snapshot.transaction() as db:
        if fingerprint(snapshot,db)!=baseline_hash:raise SystemExit('Knowledge changed while backing up; retry with a stable snapshot.')
    report_id='EVAL-'+uuid4().hex[:12]
    report=dict(id=report_id,observed_at=now(),state='running',source_type='local',prompt_version=PROMPT_VERSION,
                scorer_version=SCORER_VERSION,configuration=config.public(),human_gold_review='pending',
                evaluation_scope='Published example sources and fixed regression cases; not an independent holdout',
                knowledge_version=product['version'],baseline_hash=baseline_hash,checker_hash=checker_hash(),
                dataset_hash=digest(selection),planned=len(selection),groups=[],results=[])
    path=ROOT/'output/evaluations'/('live-'+report_id+'.json')
    ledger=Store(ROOT/'storage/model-evaluation.sqlite3')
    for index,case in enumerate(selection):
        db_path=folder/f'case-{index+1}.sqlite3';snapshot.backup(db_path)
        store=Store(db_path)
        actor=dict(id='answer-evaluation-evaluator',**case.get('actor_scope',dict(role='agent',orders=['TEST-O1','TEST-O2','TEST-O3','TEST-O4'])))
        with store.transaction() as db:
            apply_case_fixtures(store,db,case)
            ticket=services.create_ticket(store,db,actor,case['ticket'])
            for update in case.get('follow_ups',[]):ticket=services.change_ticket(store,db,actor,ticket['id'],dict(version=ticket['version'],**update))
            if case.get('multiturn'):
                ticket['messages'].extend([dict(id='MSG-fixture-agent',role='agent',text='Please confirm charging or video.',received_at='2026-09-29T11:01:00+00:00'),dict(id='MSG-fixture-customer',role='customer',text='TEST-D1, charging only. Does TEST-L1 support this?',received_at='2026-09-29T11:02:00+00:00')])
                if case.get('last_agent'):ticket['messages'].append(dict(id='MSG-fixture-last-agent',role='agent',text='Internal note: do not treat this agent sentence as a customer request for video.',received_at='2026-09-29T11:03:00+00:00'))
                store.put(db,'tickets',ticket['id'],ticket)
        engine=LiveAssist(store,config,budget_store=ledger)
        row=await engine.run(actor,ticket['id'],dict(version=ticket['version'],as_of=case['as_of']),force_component=True)
        row.update(case_id=case['id'],scenario=case['scenario'],repeat=1,concurrency=1)
        report['results'].append(row);report['summary']=summarize(report['results'])
        path.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
        print(json.dumps(dict(scenario=case['scenario'],quality=row['quality']['status'],issues=row['quality'].get('issues',[]),cost=row.get('cost')),ensure_ascii=False),flush=True)
        if row.get('error_code') in {'BUDGET_EXCEEDED','PROVIDER_AUTH','PROVIDER_BALANCE','PROVIDER_FORBIDDEN'}:break
    report.update(state='completed' if len(report['results'])==report['planned'] else 'stopped',finished_at=now(),
                  unattempted=report['planned']-len(report['results']),budget_after=engine.budget.summary())
    report['groups']=[dict(concurrency=1,summary=report['summary'])]
    path.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(report['summary'],ensure_ascii=False));print(path)


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--db',default='storage/workbench.sqlite3');parser.add_argument('--multiturn',action='store_true')
    asyncio.run(evaluate(parser.parse_args()))
