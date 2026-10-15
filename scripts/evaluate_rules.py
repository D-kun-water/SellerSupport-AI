"""Run the frozen rules fixture set; this is not an LLM quality evaluation."""
import hashlib
import json
import platform
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.main import create_app
from app.store import ROOT
from app.domain import now
from app.model_config import bootstrap_tokens, local_values


def main():
    fixture = ROOT / 'tests/fixtures/cases.json'
    cases = json.loads(fixture.read_text(encoding='utf-8'))
    results = []
    (ROOT/'storage').mkdir(exist_ok=True)
    with TemporaryDirectory(prefix='rules-', dir=ROOT/'storage') as tmp:
        for repeat in range(3):
            with TestClient(create_app(Path(tmp) / f'repeat-{repeat}.sqlite3', serve_frontend=False, seed_mode='test')) as c:
                headers = {'Authorization':'Bearer '+bootstrap_tokens(c.app.state.store.path, local_values())['agent']}
                for case in cases:
                    t = c.post('/api/tickets', headers=headers, json=case['ticket'])
                    if t.status_code != 200:
                        results.append(dict(case_id=case['id'], repeat=repeat+1, status='ERROR', error=t.text))
                        continue
                    r = c.post(f"/api/tickets/{t.json()['data']['id']}/assist", headers=headers, json=case['assist'])
                    actual = r.json().get('data',{}).get('status') if r.status_code == 200 else None
                    results.append(dict(case_id=case['id'], repeat=repeat+1,
                                        status=('PASS' if actual==case['expected_status'] else 'FAIL') if actual else 'ERROR',
                                        expected=case['expected_status'], actual=actual))
    code_hash = hashlib.sha256(b''.join(p.read_bytes() for p in sorted((ROOT/'app').glob('*.py')))).hexdigest()
    report = dict(observed_at=now(), evaluation_kind='deterministic_rules', provider='none', llm_calls=0,
                  dataset_version='local-s1-v1', human_gold_review='pending', fixture_hash=hashlib.sha256(fixture.read_bytes()).hexdigest(),
                  catalog_hash=hashlib.sha256((ROOT/'tests/fixtures/catalog.json').read_bytes()).hexdigest(), code_hash=code_hash,
                  python=sys.version.split()[0], platform=platform.platform(), planned=len(cases)*3, attempted=len(results),
                  completed=sum(x['status'] in {'PASS','FAIL'} for x in results), passed=sum(x['status']=='PASS' for x in results), results=results)
    out=ROOT/'output/evaluations';out.mkdir(parents=True,exist_ok=True)
    path=out / ('rules-'+report['observed_at'].replace(':','-').replace('+','_')+'.json')
    path.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({k:report[k] for k in ('planned','attempted','completed','passed','llm_calls')}))
    print(path)
    return 0 if report['passed']==report['planned'] else 1


if __name__=='__main__':
    raise SystemExit(main())
