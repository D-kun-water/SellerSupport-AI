"""Reproducible empty-database acceptance; no live model or merchant calls."""
import argparse,csv,io,json,sqlite3,sys
from pathlib import Path
from copy import deepcopy
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from fastapi.testclient import TestClient
from app.main import create_app
from app.model_config import ModelConfig, bootstrap_tokens, local_values
from app.store import TABLES,encode
from app.domain import now
from app.merchant import digest

def acceptance(database,report=None):
    database=Path(database)
    if database.exists():raise ValueError('Use a new database; never overwrite an acceptance run.')
    fixture=json.loads((ROOT/'tests/fixtures/second-merchant.json').read_text(encoding='utf-8'))
    checks=[];bootstrap=bootstrap_tokens(database,local_values())['supervisor'];manager=fixture['initialize']['members'][0]['token'];agent=fixture['initialize']['members'][1]['token'];restricted=fixture['initialize']['members'][2]['token']
    app=create_app(database,serve_frontend=True,model_config=ModelConfig(),seed_mode='empty');store=app.state.store
    def check(name,condition):
        if not condition:raise AssertionError(name)
        checks.append(dict(name=name,passed=True))
    def request(client,method,path,body=None,token=None,status=200):
        r=client.request(method,'/api'+path,json=body,headers={'Authorization':'Bearer '+(token or manager)})
        if r.status_code!=status:raise AssertionError(f'{method} {path}: {r.status_code} != {status}: {r.text}')
        return r.json().get('data')
    with TestClient(app) as c:
        setup=request(c,'GET','/merchant/setup',token=bootstrap)
        check('empty_database_no_local_seed',setup['merchant'] is None and setup['counts']==dict(products=0,policies=0,orders=0,catalog_releases=0))
        check('wizard_html_available',c.get('/merchant-setup').status_code==200)
        request(c,'POST','/merchant/initialize',fixture['initialize'],token=bootstrap)
        request(c,'GET','/session',token=bootstrap,status=403);check('bootstrap_replaced',True)
        request(c,'POST','/merchant/initialize',fixture['initialize'],status=409);check('initialize_once',True)
        check('configured_identity',request(c,'GET','/session',token=agent)['actor']=='beta-support')
        request(c,'GET','/merchant/setup',token=agent,status=403);check('agent_cannot_manage_merchant',True)
        request(c,'POST','/catalog-releases',dict(kind='products',object=fixture['products'][0],source_facts=dict(fixture['products'][0],power_w=80),source_text=fixture['source_text'],reason='反例事实不一致'),status=422);check('mismatched_source_rejected',True)
        for kind in ('products','policies'):
            for obj in fixture[kind]:
                row=request(c,'POST','/catalog-releases',dict(kind=kind,object=obj,source_facts=obj,source_text=fixture['source_text'],reason='第二商家合成验收'))
                request(c,'POST',f"/catalog-releases/{row['id']}/publish",dict(version=row['version'],reason='未审核不能直接发布'),status=409)
                for action in ('review','regress','publish'):
                    row=request(c,'POST',f"/catalog-releases/{row['id']}/{action}",dict(version=row['version'],reason='示例资料审核与规则验证'))
                    if action=='regress':check(obj['id']+'_regression',row['passed'] and len(row['checks'])>=4)
                check(obj['id']+'_published',row['state']=='published' and row['review_mode']=='manual_review')
        overlap=dict(fixture['policies'][0],id='BETA-CONFLICT')
        request(c,'POST','/catalog-releases',dict(kind='policies',object=overlap,source_facts=overlap,source_text=fixture['source_text'],reason='重叠政策反例'),status=409);check('overlapping_policy_rejected',True)
        bad=deepcopy(fixture['orders']);bad[1]['lines'][0]['sku']='UNKNOWN-SKU'
        request(c,'POST','/order-imports/preview',dict(format='json',content=json.dumps(bad)),status=422)
        check('invalid_batch_atomic',request(c,'GET','/orders')==[])
        out=io.StringIO();writer=csv.DictWriter(out,fieldnames=list(fixture['orders'][0]));writer.writeheader()
        for raw in fixture['orders']:
            row=dict(raw,lines=json.dumps(raw['lines']));writer.writerow(row)
        batch=request(c,'POST','/order-imports/preview',dict(format='csv',content=out.getvalue()))
        check('preview_writes_no_orders',request(c,'GET','/orders')==[])
        committed=request(c,'POST',f"/order-imports/{batch['id']}/commit",dict(version=1));check('csv_three_orders_committed',committed['inserted']==3)
        check('commit_idempotent',request(c,'POST',f"/order-imports/{batch['id']}/commit",dict(version=1))==committed)
        batch=request(c,'POST','/order-imports/preview',dict(format='json',content=json.dumps(fixture['orders'])))
        check('json_reimport_idempotent',request(c,'POST',f"/order-imports/{batch['id']}/commit",dict(version=1))['inserted']==0)
        request(c,'GET','/orders/BETA-O1',token=agent,status=403);check('new_orders_no_implicit_staff_grant',True)
        for order in fixture['orders']:
            request(c,'POST','/merchant/grants',dict(member_id='beta-support',order_id=order['id'],version=0,enabled=True,reason='合成客服处理授权'))
        check('staff_grant_visible',len(request(c,'GET','/orders',token=agent))==3)
        request(c,'POST','/merchant/grants',dict(member_id='beta-support',order_id='BETA-O1',version=0,enabled=False,reason='过期授权写入反例'),status=409);check('grant_version_conflict',True)
        def ticket(order,sku,message='Please refund the item.',region='US'):
            return request(c,'POST','/tickets',dict(source_type='local',channel='direct',region=region,order_id=order,sku=sku,device_id='BETA-PAD-01',use_case='charging',message=message),token=agent)
        t=ticket('BETA-O1','BETA-CABLE-01')
        request(c,'POST','/merchant/grants',dict(member_id='beta-support',order_id='BETA-O1',version=1,enabled=False,reason='测试撤回订单权限'))
        request(c,'GET',f"/tickets/{t['id']}",token=agent,status=403)
        check('revocation_hides_owned_ticket',not any(x['id']==t['id'] for x in request(c,'GET','/tickets',token=agent)))
        request(c,'POST','/merchant/grants',dict(member_id='beta-support',order_id='BETA-O1',version=2,enabled=True,reason='恢复测试订单权限'))
        p=request(c,'POST','/action-proposals',dict(ticket_id=t['id'],type='refund',amount='12.00',currency='USD',reason='第二商家部分退款'),token=agent)
        request(c,'POST',f"/action-proposals/{p['id']}/approve",dict(version=1,decision='approve',reason='申请人不能自批'),token=agent,status=403)
        p=request(c,'POST',f"/action-proposals/{p['id']}/approve",dict(version=1,decision='approve',reason='主管测试批准'))
        execution=dict(version=1,execution_mode='manual_record',result='success',receipt='BETA-REFUND-RECEIPT-001',idempotency_key='beta-refund-execution-001')
        p=request(c,'POST',f"/action-proposals/{p['id']}/execution-records",execution,token=agent)
        check('refund_executed',p['state']=='executed' and request(c,'GET','/orders/BETA-O1')['refunded']=='12.00')
        check('refund_execution_idempotent',request(c,'POST',f"/action-proposals/{p['id']}/execution-records",execution,token=agent)==p)
        t2=ticket('BETA-O2','BETA-CABLE-01','Please replace the missing cable.')
        item=dict(line_id='BETA-O2-L1',sku='BETA-CABLE-01',quantity=1)
        p=request(c,'POST','/action-proposals',dict(ticket_id=t2['id'],type='reship',items=[item],currency='USD',reason='第二商家缺件补发',idempotency_key='beta-reship-proposal-001'),token=agent)
        p=request(c,'POST',f"/action-proposals/{p['id']}/approve",dict(version=1,decision='approve',reason='主管核对原订单行'))
        p=request(c,'POST',f"/action-proposals/{p['id']}/execution-records",dict(version=1,execution_mode='manual_record',result='unknown',receipt='BETA-SHIP-TIMEOUT-001',idempotency_key='beta-reship-execution-001'),token=agent)
        request(c,'POST',f"/action-proposals/{p['id']}/execution-records",dict(version=1,execution_mode='manual_record',result='success',receipt='禁止未知重复执行',idempotency_key='beta-reship-execution-002'),token=agent,status=409);check('unknown_reship_blocks_retry',True)
        p=request(c,'POST',f"/action-proposals/{p['id']}/reconcile",dict(version=1,order_version=1,result='success',items=[item],currency='USD',receipt='BETA-RECONCILE-001',reason='测试物流回执确认',idempotency_key='beta-reconcile-001',execution_mode='manual_record'))
        o=request(c,'GET','/orders/BETA-O2');check('reship_reconciled_quantity_only',p['state']=='executed' and o['reshipped_by_line']=={'BETA-O2-L1':1} and o['refunded']=='0.00')
        t3=ticket('BETA-O3','BETA-CABLE-EU','The product smells burnt; please check.',region='EU')
        run=request(c,'POST',f"/tickets/{t3['id']}/assist",dict(version=t3['version'],as_of=now()),token=agent);check('new_merchant_safety_handoff',run['status']=='escalated')
        t3=request(c,'GET',f"/tickets/{t3['id']}",token=agent)
        hbody=dict(version=t3['version'],assignee='beta-reviewer',reason='安全升级人工处理',attempted_steps='停止使用并核对原文',pending_checks='检查合成订单与安全描述')
        request(c,'POST',f"/tickets/{t3['id']}/handoffs",hbody,token=agent,status=403);check('assignment_does_not_grant_order',True)
        request(c,'POST','/merchant/grants',dict(member_id='beta-reviewer',order_id='BETA-O3',version=0,enabled=True,reason='合成安全复核授权'))
        h=request(c,'POST',f"/tickets/{t3['id']}/handoffs",hbody,token=agent)
        for action in ('accept','note'):
            h=request(c,'POST',f"/handoffs/{h['id']}/actions",dict(version=h['version'],action=action,note='第二商家测试核对完成',reference='BETA-SAFETY-001'),token=restricted)
        t3=request(c,'GET',f"/tickets/{t3['id']}",token=restricted)
        h=request(c,'POST',f"/handoffs/{h['id']}/actions",dict(version=h['version'],action='close',note='合成安全处理完成',reference='BETA-SAFETY-CLOSE-001',ticket_version=t3['version']),token=restricted)
        check('custom_assignee_handoff_closed',h['state']=='closed')
        dump=request(c,'GET','/export');check('export_no_credential_hashes',all('token_hash' not in m and 'token' not in m for m in dump['members']))
        check('second_merchant_no_local_business',all(not row['id'].startswith('TEST-') for table in ('products','policies','orders') for row in dump[table]))
        check('zero_model_calls',not dump['model_calls'])
    before={}
    with store.transaction() as db:before={t:store.all(db,t) for t in TABLES}
    with TestClient(create_app(database,model_config=ModelConfig())) as c:
        check('restart_preserves_custom_shop',request(c,'GET','/session')['shop_id']=='SYNTH-BETA')
    with store.transaction() as db:check('restart_no_seed_or_business_mutation',before=={t:store.all(db,t) for t in TABLES})
    backup=database.with_name(database.stem+'-backup.sqlite3')
    if backup.exists():raise ValueError('Backup target already exists')
    store.backup(backup)
    restored=create_app(backup,model_config=ModelConfig())
    with restored.state.store.transaction() as db:check('backup_restore_all_tables',before=={t:restored.state.store.all(db,t) for t in TABLES})
    result=dict(state='passed',source_type='local',real_merchant_validation='skipped_by_user',human_review='test_review',model_calls=0,observed_at=now(),database=str(database.resolve()),backup=str(backup.resolve()),tables=len(TABLES),checks=checks,passed=len(checks),products=3,policies=4,orders=3,refund='12.00 USD test_review',reship='1 original SKU unit test_review',handoff='custom assigned member closed')
    if report:
        report=Path(report);report.parent.mkdir(parents=True,exist_ok=True);report.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    return result

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--database',type=Path,required=True);p.add_argument('--report',type=Path,required=True);a=p.parse_args();print(json.dumps(acceptance(a.database,a.report),ensure_ascii=False))
