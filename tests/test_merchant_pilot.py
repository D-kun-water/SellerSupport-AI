import csv,json
import pytest
from scripts.verify_merchant_pilot import verify

def bundle(tmp_path,mode='local'):
    (tmp_path/'policy.md').write_text('local policy fixture',encoding='utf-8')
    row=dict(case_id='C1',source_type=mode,review_mode='test_review' if mode=='local' else 'human',question='fixture question',expected_answer='fixture answer',agent_answer='fixture output',manual_seconds='60',agent_wait_seconds='5',review_seconds='10',rework_seconds='5',baseline_error='false',assisted_error='false',semantic_decision='pass',reviewer='fixture reviewer',reviewed_at='2026-10-01T10:00:00+08:00',evidence_ref='policy.md')
    with (tmp_path/'observations.csv').open('w',encoding='utf-8',newline='') as f:
        w=csv.DictWriter(f,fieldnames=row);w.writeheader();w.writerow(row)
    m=dict(source_type=mode,merchant_name='',authorization=dict(confirmed=False),files=[dict(path='policy.md',kind='fixture')],observations_csv='observations.csv',knowledge_maintenance_seconds=10)
    path=tmp_path/'manifest.json';path.write_text(json.dumps(m),encoding='utf-8');return path,m,row

def test_pilot_cost_includes_review_rework_wait_and_maintenance(tmp_path):
    path,_,_=bundle(tmp_path);r=verify(path)
    assert r['manual_total_seconds']==60 and r['assisted_total_seconds_including_maintenance']==30
    assert r['net_saved_seconds']==30 and r['net_saved_ratio']==.5
    assert r['merchant_effect']=='unverified' and r['state']=='local_tool_check'
    assert r['llm_calls']==r['external_actions']==0

def test_real_template_without_authorization_cannot_pass(tmp_path):
    path,_,_=bundle(tmp_path,'merchant_authorized')
    with pytest.raises(ValueError,match='授权'):verify(path)

@pytest.mark.parametrize('field,value',[('source_type','merchant_authorized'),('review_mode','human'),('review_seconds','nan'),('manual_seconds','-1'),('evidence_ref','absent.md'),('semantic_decision','automatic_pass')])
def test_invalid_measurements_and_mixed_provenance_rejected(tmp_path,field,value):
    path,_,row=bundle(tmp_path);row[field]=value
    with (tmp_path/'observations.csv').open('w',encoding='utf-8',newline='') as f:
        w=csv.DictWriter(f,fieldnames=row);w.writeheader();w.writerow(row)
    with pytest.raises(ValueError):verify(path)

def test_pilot_does_not_hide_negative_net_value(tmp_path):
    path,m,_=bundle(tmp_path);m['knowledge_maintenance_seconds']=100;path.write_text(json.dumps(m),encoding='utf-8')
    assert verify(path)['net_saved_seconds']==-60

def test_semantic_label_cannot_contradict_error_flag(tmp_path):
    path,_,row=bundle(tmp_path);row['assisted_error']='true'
    with (tmp_path/'observations.csv').open('w',encoding='utf-8',newline='') as f:
        w=csv.DictWriter(f,fieldnames=row);w.writeheader();w.writerow(row)
    with pytest.raises(ValueError,match='冲突'):verify(path)
