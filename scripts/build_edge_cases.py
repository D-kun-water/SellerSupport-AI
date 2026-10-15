"""Distinct fixture/permission/multi-turn scenarios. Pending review, no LLM-generated gold."""
import json
from copy import deepcopy
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]


def main():
    catalog=json.loads((ROOT/'tests/fixtures/catalog.json').read_text(encoding='utf-8'))
    refs=deepcopy(catalog['sources'])
    product=next(p for p in catalog['products'] if p['id']=='TEST-L1')
    order=catalog['orders'][0]
    policy=catalog['policies'][0]
    cases=[]
    def add(name, ticket=None, status='escalated', required=None, fixtures=None, follow_ups=None, actor_scope=None,error=None,as_of='2026-09-29T12:00:00+00:00'):
        number=len(cases)+1
        base=dict(message='Can I charge TEST-D1 with this cable?',source_type='local',sku='TEST-L1',device_id='TEST-D1',use_case='charging',region='US',channel='direct',order_id=None)
        base.update(ticket or {})
        cases.append(dict(id=f'S2C-{number:03}',version=1,state='pending',reviewer=None,source_type='local',
                     scenario=name,scenario_family=f's2-complex-{name}',split='regression',ticket=base,as_of=as_of,
                     expected=dict(status=status,required_fragments=required or [],forbidden_fragments=['Your refund has been processed.'],error_code=error),
                     fixtures=fixtures or [],follow_ups=follow_ups or [],
                     actor_scope=actor_scope or dict(role='agent',orders=['TEST-O1','TEST-O2','TEST-O3','TEST-O4']),
                     evidence_refs=refs,review_scope='pending_human_not_gold'))
    add('device_correction_after_first_message',follow_ups=[dict(message='Correction: actually TEST-D2, not TEST-D1.')],required=['conflicts with the confirmed record'])
    add('safety_refund_and_tracking',ticket=dict(message='The charger is smoking. Where is my parcel? Please refund my order.',order_id='TEST-O1'),required=['stop using','tracking question remains pending','return or refund question remains pending'])
    stale=deepcopy(order);stale['max_age_seconds']=1
    add('stale_tracking_snapshot',ticket=dict(message='Where is my parcel?',order_id='TEST-O1'),fixtures=[dict(table='orders',object=stale)],as_of='2026-09-29T12:00:02+00:00',required=['snapshot is stale'])
    add('snapshot_from_future',ticket=dict(message='Where is my parcel?',order_id='TEST-O1'),as_of='2026-09-29T11:59:59+00:00',required=['snapshot timing needs human verification'])
    future=deepcopy(order);future['last_tracking_at']='2026-10-01T00:00:00+00:00'
    add('tracking_event_from_future',ticket=dict(message='Where is my parcel?',order_id='TEST-O1'),fixtures=[dict(table='orders',object=future)],required=['snapshot timing needs human verification'])
    absent=deepcopy(policy);absent['review_status']='draft'
    add('return_policy_not_published',ticket=dict(message='Can I return this?',order_id='TEST-O1'),fixtures=[dict(table='policies',object=absent)],required=['policy needs human verification'])
    conflict=deepcopy(policy);conflict['id']='TEST-POLICY-CONFLICT';conflict['window_days']=45
    add('two_active_return_policies',ticket=dict(message='Can I return this?',order_id='TEST-O1'),fixtures=[dict(table='policies',object=conflict)],required=['policy needs human verification'])
    conflict_product=deepcopy(product);conflict_product['id']='TEST-L1-CONFLICT'
    add('two_active_same_sku_versions',fixtures=[dict(table='products',object=conflict_product)],required=['unambiguous published specification'])
    archived=deepcopy(next(s for s in refs if s['id']==product['source_id']));archived['status']='disabled'
    add('published_product_archived_source',fixtures=[dict(table='sources',current_source_for_sku='TEST-L1',changes={'status':'disabled'})],error='NO_EVIDENCE')
    expired=deepcopy(product);expired['valid_to']='2026-09-29T12:00:00+00:00'
    add('product_expiry_exact_boundary',fixtures=[dict(table='products',object=expired)],required=['unambiguous published specification'])
    add('restricted_actor_cannot_create_order_ticket',ticket=dict(message='Refund my order',order_id='TEST-O2'),actor_scope=dict(role='agent',orders=['TEST-O1']),error='FORBIDDEN')
    add('clarification_then_explicit_model',ticket=dict(message='Does this work with my iPad?',device_id=None),follow_ups=[dict(device_id='TEST-D1',message='The exact device is TEST-D1 and I need charging.')],status='draft',required=['verified USB-C PD','does not provide video'])
    add('return_and_tracking_both_preserved',ticket=dict(message='Can I return this? Also where is my parcel?',order_id='TEST-O1'),status='draft',required=['no refund has been approved or executed','tracking question remains pending'])
    add('missing_delivery_multi_intent',ticket=dict(message='Can I return this? Also where is my parcel?',order_id='TEST-O2'),status='need_clarification',required=['exact time','tracking question remains pending'])
    add('confirmed_model_changed_in_fields',follow_ups=[dict(device_id='TEST-D2',message='Please check charging for this model.')],required=['has not been verified'])
    add('sku_changes_to_unverified_cable',follow_ups=[dict(sku='TEST-L2',message='I selected TEST-L2 instead; check charging.')],required=['has not been verified'])
    bundle=dict(dataset_version='s2-complex-v1',source_type='local',human_review='pending',independent_scenarios=len(cases),cases=cases)
    (ROOT/'tests/fixtures/edge-cases.json').write_text(json.dumps(bundle,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(f'{len(cases)} distinct complex candidates written; no approvals')


if __name__=='__main__':main()
