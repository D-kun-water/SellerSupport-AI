"""Apply the current hard checker to frozen outputs, without another model call."""
import hashlib
import json
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app.store import ROOT
from app.quality import check_draft,SCORER_VERSION
from app.domain import now

reports=[]
for path in sorted((ROOT/'output/evaluations').glob('live-*.json')):
    r=json.loads(path.read_text(encoding='utf-8'))
    if r['state']!='completed':continue
    results=[]
    for row in r['results']:
        if 'raw_model_content' not in row:
            results.append(dict(run_id=row['id'],status='ERROR',issues=['NO_MODEL_OUTPUT']));continue
        baseline=dict(row,status=row['baseline_status'],reply_draft=row['baseline_reply_draft'])
        _,quality=check_draft(row['raw_model_content'],baseline)
        results.append(dict(run_id=row['id'],scenario=row['scenario'],**quality))
    reports.append(dict(id=r['id'],prompt_version=r['prompt_version'],source_report_hash=hashlib.sha256(path.read_bytes()).hexdigest(),
                        attempted=len(results),hard_checks_passed=sum(x['status']=='REVIEW' for x in results),
                        failed=sum(x['status']=='FAIL' for x in results),results=results))
out=dict(observed_at=now(),scorer_version=SCORER_VERSION,scope='Common hard checker applied to original stored outputs and original trusted baselines; no semantic PASS, no new model calls.',reports=reports)
path=ROOT/'output/evaluations/common-scorer.json';path.write_text(json.dumps(out,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
print(json.dumps([{k:r[k] for k in ('id','prompt_version','attempted','hard_checks_passed','failed')} for r in reports]));print(path)
