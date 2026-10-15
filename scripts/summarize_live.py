"""Derived comparison; original run reports remain unchanged."""
from decimal import Decimal
from datetime import datetime, time
import hashlib
import json
from pathlib import Path
import sys
from zoneinfo import ZoneInfo
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app.store import ROOT
from app.domain import now


def period(observed_at,pricing):
    stamp=datetime.fromisoformat(observed_at)
    if stamp.tzinfo is None:raise ValueError('Pricing timestamp must include time zone')
    local=stamp.astimezone(ZoneInfo('Asia/Shanghai'))
    day=pricing['reviewed_calendar'].get(local.date().isoformat())
    if day not in ('workday','offpeak_day'):
        raise ValueError('Pricing calendar not reviewed for '+local.date().isoformat())
    clock=local.time()
    return 'peak' if day=='workday' and (time(9)<=clock<time(12) or time(14)<=clock<time(18)) else 'offpeak'


def usage_cost(results,pricing):
    hit=miss=output=0
    amount=Decimal(0)
    periods={'peak':0,'offpeak':0}
    boundaries=0
    for r in results:
        u=r['usage']
        values=[u.get(key) for key in ('prompt_tokens','completion_tokens','prompt_cache_hit_tokens','prompt_cache_miss_tokens')]
        if any(type(value) is not int or value<0 for value in values):
            raise ValueError('Pricing requires nonnegative integer measured cache usage')
        tokens,completion,h,m=values
        if h+m!=tokens:raise ValueError('Cache usage mismatch')
        hit+=h;miss+=m;output+=completion
        slot=period(r['observed_at'],pricing)
        start=period(r.get('accepted_at',r['observed_at']),pricing)
        if start!=slot:
            # Exact provider billing time is unavailable; use peak for a boundary-crossing call.
            slot='peak';boundaries+=1
        periods[slot]+=1
        rate=pricing[slot]
        amount+=(Decimal(h)*Decimal(rate['cache_hit'])+Decimal(m)*Decimal(rate['cache_miss'])+
                 Decimal(completion)*Decimal(rate['output']))/1000000
    return dict(cache_hit_tokens=hit,cache_miss_tokens=miss,output_tokens=output,
                peak_calls=periods['peak'],offpeak_calls=periods['offpeak'],boundary_calls=boundaries,
                cache_and_time_estimate_cny=str(amount))


def main():
    snapshot=ROOT/'storage/deepseek-pricing.html'
    if not snapshot.exists():raise SystemExit('Fetch the official pricing snapshot before deriving adjusted costs.')
    text=snapshot.read_text(encoding='utf-8')
    if 'DeepSeek-V4.1-Flash' not in text or '空闲时段' not in text:raise SystemExit('Pricing snapshot does not match reviewed source.')
    # Reviewed source metadata is pinned; recomputing a report is not a new price verification.
    pricing=json.loads((ROOT/'data/evaluation/deepseek-pricing-2026-09-30.json').read_text(encoding='utf-8'))
    if pricing['source_hash']!=hashlib.sha256(snapshot.read_bytes()).hexdigest():
        raise SystemExit('Pricing snapshot hash differs from the reviewed metadata.')
    reports=[]
    for path in (ROOT/'output/evaluations').glob('live-*.json'):
        r=json.loads(path.read_text(encoding='utf-8'))
        if r['state']!='completed':continue
        if any(not x.get('usage_complete') for x in r['results']):continue
        if r['configuration']['requested_model']!=pricing['requested_model']:
            raise SystemExit('Report model does not match pinned price sheet: '+r['id'])
        reports.append(dict(id=r['id'],prompt_version=r['prompt_version'],scorer_version=r['scorer_version'],
                            summary=r['summary'],groups=r['groups'],pricing_adjustment=usage_cost(r['results'],pricing),
                            report_hash=hashlib.sha256(path.read_bytes()).hexdigest()))
    reports.sort(key=lambda x:x['prompt_version'])
    probes=[json.loads(p.read_text(encoding='utf-8')) for p in (ROOT/'output/evaluations').glob('PROBE-*.json')]
    probe_metrics=[dict(p.get('metrics',{}),observed_at=p['observed_at']) for p in probes]
    probe_cost=usage_cost([p for p in probe_metrics if p.get('usage_complete')],pricing)
    derived=dict(observed_at=now(),pricing=pricing,reports=reports,probe=probe_cost,
                 total_cache_and_time_estimate_cny=str(sum((Decimal(r['pricing_adjustment']['cache_and_time_estimate_cny']) for r in reports),Decimal(probe_cost['cache_and_time_estimate_cny']))),
                 total_uncached_peak_estimate_cny=str(sum((Decimal(r['summary']['estimated_cost_cny']) for r in reports),sum((Decimal(p['cost']) for p in probe_metrics if p.get('cost')),Decimal(0)))),
                 scope='local fixed cases and targeted rechecks; report-specific planned/attempted counts apply. No independent human gold review.',
                 comparability='Prompt, scorer, rules and application code changed between some revisions. Not a model-only A/B; no causal speed claim.')
    path=ROOT/'output/evaluations/comparison.json'
    path.write_text(json.dumps(derived,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({k:derived[k] for k in ('total_cache_and_time_estimate_cny','total_uncached_peak_estimate_cny')}));print(path)


if __name__=='__main__':main()
