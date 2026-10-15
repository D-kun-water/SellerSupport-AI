import json
from pathlib import Path
from scripts.build_case_splits import build

def test_scenario_splits_are_balanced_without_family_leakage():
    r=build();assert r['case_counts']=={'development':70,'validation':23,'frozen':23}
    assignments={}
    for c in r['cases']:
        assignments.setdefault(c['scenario_family'],set()).add(c['split'])
    assert len(assignments)==36 and all(len(v)==1 for v in assignments.values())
    assert all(c['historically_used_for_development'] for c in r['cases'])

def test_frozen_manifest_matches_current_input_hashes():
    r=build();p=Path('data/evaluation/case-splits.json');saved=json.loads(p.read_text(encoding='utf-8'))
    assert saved['inputs_sha256']==r['inputs_sha256'] and saved['cases']==r['cases']

