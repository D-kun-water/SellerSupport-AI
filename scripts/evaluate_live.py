"""Frozen local generation benchmark; no customer sends or business actions."""
import argparse
import asyncio
from collections import Counter
from decimal import Decimal
import hashlib
import json
import math
from pathlib import Path
import sys
import time
from tempfile import TemporaryDirectory

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app.model_config import ModelConfig
from app.store import ROOT,Store,uid
from app.live_assist import LiveAssist
from app import services
from app.domain import now
from app.quality import PROMPT_VERSION,SCORER_VERSION

ACTOR=dict(id='local-agent',role='agent',orders=['TEST-O1','TEST-O2','TEST-O3','TEST-O4'])


def distribution(values):
    values=sorted(v for v in values if v is not None)
    def percentile(p):
        return values[max(0,math.ceil(len(values)*p)-1)] if values else None
    return dict(n=len(values),p50=percentile(.5),p95=percentile(.95),max=max(values) if values else None)


def summarize(results):
    return dict(attempted=len(results),completed=sum(r.get('usage_complete',False) for r in results),
                quality_counts=dict(Counter(r['quality']['status'] for r in results)),
                hard_checks_passed=sum(r['quality'].get('hard_checks_passed',False) for r in results),
                llm_calls=sum(r.get('llm_calls',0) for r in results),
                input_tokens=sum(r.get('input_tokens') or 0 for r in results),
                output_tokens=sum(r.get('output_tokens') or 0 for r in results),
                usage_unknown=sum(r.get('llm_calls',0)>0 and not r.get('usage_complete',False) for r in results),
                estimated_cost_cny=str(sum((Decimal(r['cost']) for r in results if r.get('cost') is not None),Decimal(0))),
                end_to_end_ms=distribution([r['benchmark_total_ms'] for r in results]),
                provider_ttft_ms=distribution([r.get('provider_ttft_ms') for r in results]),
                provider_total_ms=distribution([r.get('provider_total_ms') for r in results]),
                semantic_pass_rate=None)


async def evaluate(args):
    cfg=ModelConfig.load()
    if not cfg.api_key:
        raise SystemExit('NOT_CONFIGURED: fill local .env; do not send key to chat')
    fixture=ROOT/('tests/fixtures/scenario-merchant-cases.json' if getattr(args,'dataset','fixed')=='scenario-merchant' else 'tests/fixtures/cases.json')
    cases=json.loads(fixture.read_text(encoding='utf-8'))
    if args.scenarios:
        requested=set(args.scenarios.split(','))
        unknown=requested-{case['scenario'] for case in cases}
        if unknown:raise SystemExit('Unknown scenarios: '+', '.join(sorted(unknown)))
        cases=[c for c in cases if c['scenario'] in requested]
    # Benchmark facts come from a fresh fixture store, never previously edited runtime data.
    # The separate persistent ledger still charges every run against the same total budget.
    ledger=Store(ROOT/'storage/model-evaluation.sqlite3')
    with TemporaryDirectory(prefix='eval-',dir=ROOT/'storage') as temporary:
        store=Store(Path(temporary)/'fixtures.sqlite3');store.seed()
        await evaluate_store(args,cfg,fixture,cases,store,ledger)


async def evaluate_store(args,cfg,fixture,cases,store,ledger):
    engine=LiveAssist(store,cfg,budget_store=ledger)
    report=dict(id=uid('EVAL'),observed_at=now(),state='running',configuration=cfg.public(),
                evaluation_scope='generation_component_on_local_evidence; escalated cases forced for offline testing; not production bypass performance',
                human_gold_review='pending',prompt_version=PROMPT_VERSION,scorer_version=SCORER_VERSION,
                generation_data_store='isolated_fresh_local_fixture',
                dataset=args.dataset, fixture_path=fixture.relative_to(ROOT).as_posix(),
                fixture_hash=hashlib.sha256(fixture.read_bytes()).hexdigest(),
                catalog_hash=hashlib.sha256((ROOT/'tests/fixtures/catalog.json').read_bytes()).hexdigest(),
                code_hash=hashlib.sha256(b''.join(p.read_bytes() for p in sorted((ROOT/'app').glob('*.py')))).hexdigest(),
                pricing_note='configured 2 CNY/M input and 8 CNY/M output, ignores cache/discount; usage estimate, not billing receipt',
                planned=len(cases)*args.repeats*len(args.concurrency),results=[],groups=[])
    out=ROOT/'output/evaluations';out.mkdir(parents=True,exist_ok=True)
    path=out/f"live-{report['id']}.json"
    fatal=False
    def save():
        report['summary']=summarize(report['results'])
        report['budget']=engine.budget.summary()
        temp=path.with_suffix('.tmp');temp.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8');temp.replace(path)
    save()
    for concurrency in args.concurrency:
        sem=asyncio.Semaphore(concurrency)
        group_start=time.perf_counter();group_results=[]
        async def one(case,repeat):
            nonlocal fatal
            async with sem:
                if fatal:return
                start=time.perf_counter()
                with store.transaction() as db:
                    ticket=services.create_ticket(store,db,ACTOR,case['ticket'])
                result=await engine.run(ACTOR,ticket['id'],case['assist'],force_component=True)
                result.update(case_id=case['id'],scenario=case['scenario'],repeat=repeat,concurrency=concurrency,
                              expected_status=case['expected_status'],benchmark_total_ms=round((time.perf_counter()-start)*1000,3))
                report['results'].append(result);group_results.append(result)
                if result.get('error_code') in {'PROVIDER_AUTH','PROVIDER_BALANCE','PROVIDER_FORBIDDEN','BUDGET_EXCEEDED'}:fatal=True
                save()
                print(json.dumps(dict(done=len(report['results']),planned=report['planned'],scenario=case['scenario'],
                                      concurrency=concurrency,quality=result['quality']['status'],issues=result['quality'].get('issues'),
                                      ms=result['benchmark_total_ms'],cost=result.get('cost')),ensure_ascii=False),flush=True)
        await asyncio.gather(*(one(case,repeat+1) for repeat in range(args.repeats) for case in cases))
        report['groups'].append(dict(concurrency=concurrency,wall_ms=round((time.perf_counter()-group_start)*1000,3),summary=summarize(group_results)))
        if fatal:break
    report.update(state='completed' if len(report['results'])==report['planned'] else 'stopped',finished_at=now(),
                  unattempted=report['planned']-len(report['results']))
    save();print(json.dumps(report['summary'],ensure_ascii=False));print(path)


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--dataset',choices=['fixed','scenario-merchant'],default='fixed')
    parser.add_argument('--repeats',type=int,default=3,choices=range(1,4))
    parser.add_argument('--concurrency',type=int,nargs='+',default=[1,2],choices=[1,2])
    parser.add_argument('--scenarios',default='')
    asyncio.run(evaluate(parser.parse_args()))
