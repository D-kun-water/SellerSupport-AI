"""Local paired-workload verification. No external model call or customer action."""
import argparse,csv,hashlib,json,math,sys
from pathlib import Path
from statistics import median
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from app.domain import timestamp,now

def verify(manifest_path):
    manifest_path=Path(manifest_path).resolve()
    m=json.loads(manifest_path.read_text(encoding='utf-8'))
    mode=m.get('source_type')
    if mode not in {'local','merchant_authorized'}:raise ValueError('必须声明local或merchant_authorized')
    real=mode=='merchant_authorized'
    auth=m.get('authorization',{})
    if real and not (m.get('merchant_name') and auth.get('confirmed') is True and auth.get('approved_by') and auth.get('reference') and 'local_offline' in auth.get('scope',[])):
        raise ValueError('真实验证缺少商家名称、确认授权、责任人与本地离线范围')
    def local(path):
        p=(manifest_path.parent/path).resolve()
        if not p.is_relative_to(manifest_path.parent):raise ValueError('资料必须在授权资料包目录内')
        if not p.is_file():raise ValueError('资料文件不存在')
        return p
    if not m.get('files'):raise ValueError('缺少授权资料/依据文件清单')
    files=[]
    for f in m['files']:
        p=local(f['path'])
        if p.stat().st_size>25*1024*1024:raise ValueError('单项资料上限25MB')
        files.append(dict(path=f['path'],kind=f['kind'],sha256=hashlib.sha256(p.read_bytes()).hexdigest()))
    source_refs={f['path'] for f in files}
    metrics=local(m['observations_csv'])
    with metrics.open(encoding='utf-8-sig',newline='') as handle:
        rows=list(csv.DictReader(handle))
    if not rows:raise ValueError('缺少真实观测，不能生成效果结论')
    seen=set();accepted=[]
    def number(row,key):
        v=float(row[key])
        if not math.isfinite(v) or v<0:raise ValueError('时长必须是实际测得的非负有限秒数')
        return v
    def flag(row,key):
        if row[key] not in {'true','false'}:raise ValueError('错误标记只能为true或false')
        return row[key]=='true'
    for row in rows:
        if not row.get('case_id') or row['case_id'] in seen:raise ValueError('案例ID缺失或重复')
        seen.add(row['case_id'])
        if row.get('source_type')!=mode:raise ValueError('真实/合成观测混用')
        if row.get('review_mode')!=('human' if real else 'test_review'):raise ValueError('真实观测必须明确human审核，不能使用测试审核')
        if not row.get('reviewer') or not row.get('evidence_ref') in source_refs:raise ValueError('缺少审核者或资料包内依据定位')
        timestamp(row['reviewed_at'])
        required=['question','expected_answer','agent_answer']
        if any(not row.get(k,'').strip() for k in required):raise ValueError('缺少问题、经审核期望或实际Agent输出')
        if row.get('semantic_decision') not in {'pass','fail','uncertain'}:raise ValueError('语义判断需pass/fail/uncertain')
        accepted.append(dict(case_id=row['case_id'],manual_seconds=number(row,'manual_seconds'),
                             assisted_seconds=sum(number(row,k) for k in ('agent_wait_seconds','review_seconds','rework_seconds')),
                             baseline_error=flag(row,'baseline_error'),assisted_error=flag(row,'assisted_error'),
                             semantic_decision=row['semantic_decision'],reviewer=row['reviewer'],evidence_ref=row['evidence_ref']))
        if (row['semantic_decision']=='pass' and accepted[-1]['assisted_error']) or (row['semantic_decision']=='fail' and not accepted[-1]['assisted_error']):
            raise ValueError('语义判断与错误标记冲突，需先人工核对')
    maintenance=float(m['knowledge_maintenance_seconds'])
    if not math.isfinite(maintenance) or maintenance<0:raise ValueError('知识维护时间必须填写实测非负秒数')
    baseline=sum(r['manual_seconds'] for r in accepted)
    assisted=sum(r['assisted_seconds'] for r in accepted)+maintenance
    errors=sum(r['assisted_error'] for r in accepted);previous=sum(r['baseline_error'] for r in accepted)
    uncertain=sum(r['semantic_decision']=='uncertain' for r in accepted)
    return dict(observed_at=now(),source_type=mode,state='observations_verified' if real else 'local_tool_check',
                merchant_effect='observed_sample_only' if real else 'unverified',authorization_reference=auth.get('reference'),
                scope='本地成对回放；授权与人工标签为提供者声明，不自动验证签名、真实性或因果效果',
                files=files,observations_sha256=hashlib.sha256(metrics.read_bytes()).hexdigest(),sample_size=len(accepted),
                manual_total_seconds=baseline,assisted_total_seconds_including_maintenance=assisted,net_saved_seconds=baseline-assisted,
                net_saved_ratio=(baseline-assisted)/baseline if baseline else None,
                median_case_seconds_saved=median(r['manual_seconds']-r['assisted_seconds'] for r in accepted),
                baseline_errors=previous,assisted_errors=errors,semantic_passed=sum(r['semantic_decision']=='pass' for r in accepted),
                semantic_uncertain=uncertain,quality_not_worse_observed=errors<=previous and not uncertain,
                decision='review_observed_value' if real else 'no_real_merchant_conclusion',rows=accepted,
                llm_calls=0,external_actions=0)

def main():
    p=argparse.ArgumentParser();p.add_argument('manifest',type=Path);p.add_argument('--report',type=Path,required=True);a=p.parse_args()
    try:r=verify(a.manifest)
    except (ValueError,KeyError,OSError,TypeError) as exc:raise SystemExit('未完成验证：'+str(exc))
    if a.report.resolve().is_relative_to(a.manifest.resolve().parent):raise SystemExit('报告请写到资料包以外，保留原始证据')
    if a.report.exists():raise SystemExit('报告已存在，不覆盖历史验证')
    a.report.parent.mkdir(parents=True,exist_ok=True);a.report.write_text(json.dumps(r,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({k:v for k,v in r.items() if k not in {'rows','files'}},ensure_ascii=False))
if __name__=='__main__':main()
