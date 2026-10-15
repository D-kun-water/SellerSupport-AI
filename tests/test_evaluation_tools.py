import asyncio
from argparse import Namespace
import json
from decimal import Decimal
import pytest
from scripts.summarize_live import period, usage_cost
from app.model_config import ModelConfig
from app.store import Store, ROOT
import scripts.evaluate_live as live


PRICING=dict(reviewed_calendar={'2026-09-30':'workday','2026-10-01':'offpeak_day'},
             peak=dict(cache_hit='.04',cache_miss='2',output='8'),
             offpeak=dict(cache_hit='.02',cache_miss='1',output='4'))


@pytest.mark.parametrize('clock,expected',[('08:59:59','offpeak'),('09:00:00','peak'),
    ('11:59:59','peak'),('12:00:00','offpeak'),('13:59:59','offpeak'),('14:00:00','peak'),
    ('17:59:59','peak'),('18:00:00','offpeak')])
def test_pricing_time_boundaries(clock,expected):
    assert period('2026-09-30T'+clock+'+08:00',PRICING)==expected


def test_pricing_calendar_requires_review_and_explicit_zone():
    assert period('2026-10-01T10:00:00+08:00',PRICING)=='offpeak'
    with pytest.raises(ValueError):period('2026-10-02T10:00:00+08:00',PRICING)
    with pytest.raises(ValueError):period('2026-09-30T10:00:00',PRICING)


def test_cache_cost_and_crossing_boundary_use_conservative_price():
    row=dict(observed_at='2026-09-30T12:00:01+08:00',accepted_at='2026-09-30T11:59:59+08:00',
             usage=dict(prompt_tokens=100,completion_tokens=10,prompt_cache_hit_tokens=80,prompt_cache_miss_tokens=20))
    result=usage_cost([row],PRICING)
    assert result['boundary_calls']==1 and result['peak_calls']==1
    assert Decimal(result['cache_and_time_estimate_cny'])==Decimal('.0001232')
    row['usage']['prompt_cache_hit_tokens']=-1
    with pytest.raises(ValueError):usage_cost([row],PRICING)
    row['usage'].pop('prompt_cache_hit_tokens')
    with pytest.raises(ValueError):usage_cost([row],PRICING)


@pytest.mark.parametrize('fail',[False,True])
def test_live_evaluation_uses_fresh_facts_and_preserves_shared_ledger(tmp_path,monkeypatch,fail):
    local=tmp_path/'tests/fixtures';local.mkdir(parents=True)
    for name in ('cases.json','catalog.json'):
        (local/name).write_bytes((ROOT/'tests/fixtures'/name).read_bytes())
    ledger=Store(tmp_path/'storage/model-evaluation.sqlite3');ledger.seed()
    with ledger.transaction() as db:
        product=ledger.get(db,'products','TEST-L1');product['power_w']=999
        ledger.put(db,'products',product['id'],product)
        ledger.put(db,'model_calls','existing',dict(id='existing',charged_or_reserved_cny='1'))
    monkeypatch.setattr(live,'ROOT',tmp_path)
    monkeypatch.setattr(live.ModelConfig,'load',lambda:ModelConfig(api_key='test-key'))
    temporary=[]
    async def inspect(args,cfg,fixture,cases,store,budget_store):
        temporary.append(store.path.parent)
        with store.transaction() as db:
            assert store.get(db,'products','TEST-L1')['power_w']==60
        with budget_store.transaction() as db:
            assert budget_store.get(db,'model_calls','existing')['charged_or_reserved_cny']=='1'
        if fail:raise RuntimeError('local evaluation interruption')
    monkeypatch.setattr(live,'evaluate_store',inspect)
    args=Namespace(scenarios='safety_multi',repeats=1,concurrency=[1])
    if fail:
        with pytest.raises(RuntimeError):asyncio.run(live.evaluate(args))
    else:asyncio.run(live.evaluate(args))
    assert temporary and not temporary[0].exists()
    with ledger.transaction() as db:
        assert ledger.get(db,'products','TEST-L1')['power_w']==999

