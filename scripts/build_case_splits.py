"""Versioned split manifest; preserve historical exposure and original reviewed labels."""
import argparse,hashlib,json,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from app.domain import now

def build():
    inputs=['evaluation-cases.json','edge-cases.json'];rows=[];hashes={}
    for name in inputs:
        raw=(ROOT/'tests/fixtures'/name).read_bytes();hashes[name]=hashlib.sha256(raw).hexdigest();rows.extend(json.loads(raw)['cases'])
    counts={f:sum(r['scenario']==f for r in rows) for f in {r['scenario'] for r in rows}}
    assignments={}
    for size,cutoffs in [(5,(12,16)),(1,(10,13))]:
        group=sorted((f for f,n in counts.items() if n==size),key=lambda s:hashlib.sha256(('split-v2:'+s).encode()).hexdigest())
        assignments.update({f:('development' if i<cutoffs[0] else 'validation' if i<cutoffs[1] else 'frozen') for i,f in enumerate(group)})
    families=list(assignments)
    cases=[dict(case_id=r['id'],scenario_family=r['scenario'],split=assignments[r['scenario']],legacy_split=r.get('split'),historically_used_for_development=True) for r in rows]
    assert len({r['case_id'] for r in cases})==len(cases)==116
    assert len(families)==36
    return dict(version='scenario-splits-v4',supersedes='scenario-splits-v3',created_at=now(),source_type='local',inputs_sha256=hashes,
                split_unit='scenario_family',family_counts={s:sum(v==s for v in assignments.values()) for s in ('development','validation','frozen')},
                case_counts={s:sum(r['split']==s for r in cases) for s in ('development','validation','frozen')},
                historical_exposure='All 116 cases were already used for development checks. Frozen means future change control, not untouched holdout.',
                future_rule='Do not tune against frozen outcomes. Keep versioned manifests immutable; change through a new reviewed version.',cases=cases)

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--apply-db',type=Path);args=parser.parse_args()
    path=ROOT/'data/evaluation/case-splits.json';result=build();path.parent.mkdir(parents=True,exist_ok=True)
    if path.exists():
        previous=json.loads(path.read_text(encoding='utf-8'))
        assert previous['inputs_sha256']==result['inputs_sha256'] and previous['cases']==result['cases'],'Frozen manifest drift; create a new version.'
        print('Existing frozen manifest validated; not overwritten.');result=previous
    else:path.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    if args.apply_db:
        from app.store import Store,uid
        dbpath=args.apply_db.resolve()
        if not dbpath.is_file() or not dbpath.is_relative_to(ROOT/'storage'):raise SystemExit('Only an existing storage database may be updated.')
        store=Store(dbpath);backup=dbpath.with_name(dbpath.stem+'-before-splits-'+uid('BACKUP')+'.sqlite3');store.backup(backup)
        changed=0
        with store.transaction() as db:
            for entry in result['cases']:
                row=store.get(db,'eval_cases',entry['case_id'])
                if row.get('split_version')==result['version']:continue
                row.setdefault('legacy_split',row['split']);row.update(split=entry['split'],split_version=result['version'],historically_used_for_development=True,version=row['version']+1)
                store.put(db,'eval_cases',row['id'],row);changed+=1
            store.audit(db,{'id':'local-supervisor','role':'supervisor'},'evaluation_splits_versioned',result['version'],{'changed':changed,'backup':str(backup),'human_review_completed':False})
        print(json.dumps({'updated':changed,'backup':str(backup)}))
    print(json.dumps({k:result[k] for k in ('version','family_counts','case_counts','historical_exposure')}))
if __name__=='__main__':main()
