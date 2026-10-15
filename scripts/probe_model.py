import asyncio
import json
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app.model_config import ModelConfig
from app.provider import DeepSeekProvider, ProviderError
from app.store import ROOT, Store, uid
from app.budget import Budget
from app.domain import now


async def main():
    config=ModelConfig.load()
    store=Store(ROOT/'storage/model-evaluation.sqlite3')
    budget=Budget(store,config)
    messages=[{'role':'system','content':'Return only JSON with key status and value ok. Do not use tools.'},
              {'role':'user','content':'Connectivity probe only. Return {"status":"ok"}.'}]
    ref=budget.reserve(messages)
    report=dict(id=uid('PROBE'),observed_at=now(),configuration=config.public(),scope='one short connectivity request')
    try:
        result=await DeepSeekProvider(config).complete(messages)
        budget.settle(ref,result.metrics)
        report.update(status='success',metrics=result.metrics,response=json.loads(result.content))
    except ProviderError as exc:
        budget.settle(ref,exc.metrics)
        report.update(status='error',error_code=exc.code,error_message=exc.message,metrics=exc.metrics)
    except ValueError:
        report.update(status='error',error_code='INVALID_JSON')
    out=ROOT/'output/evaluations';out.mkdir(exist_ok=True,parents=True)
    path=out/(report['id']+'.json');path.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(report,ensure_ascii=False))
    print(path)
    return report['status']=='success'


if __name__=='__main__':
    raise SystemExit(0 if asyncio.run(main()) else 1)
