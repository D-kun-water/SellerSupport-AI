"""100 pending candidates; scenario families never cross the 60/20/20 split."""
import json
from copy import deepcopy
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    base = json.loads((ROOT/'tests/fixtures/cases.json').read_text(encoding='utf-8'))
    catalog = json.loads((ROOT/'tests/fixtures/catalog.json').read_text(encoding='utf-8'))
    sources = {s['id']: s for s in catalog['sources']}
    assertions = {
        'missing_model': ['exact device model'], 'connector_only': ['exact device model', 'charging, data transfer or video'],
        'conditional_match': ['verified USB-C PD', 'does not provide video'], 'video_unsupported': ['no video capability'],
        'unknown_device': ['has not been verified'], 'unknown_sku': ['unambiguous published specification'],
        'missing_delivery': ['exact time', 'time zone'], 'boundary_equal': ['within the return', 'no refund has been approved or executed'],
        'boundary_plus_second': ['outside the return', 'no refund has been approved or executed'],
        'tracking_snapshot': ['9.17 days', 'not a live carrier check'], 'safety': ['stop using', 'do not test'],
        'safety_multi': ['stop using', 'tracking question remains pending'], 'no_manual_steps': ['No verified troubleshooting steps'],
        'injection': ['no refund has been approved or executed'], 'wrong_market': ['unambiguous published specification'],
        'missing_order': ['order reference'], 'unknown_intent': ['human support'],
        'power_not_guarantee': ['actual device draw can be lower'], 'no_rules_for_cable': ['has not been verified'],
        'return_recent': ['within the return', 'no refund has been approved or executed']}
    prefixes = ['', 'Hello, ', 'Please help: ', 'I need clarification. ', 'Before proceeding: ']
    rows = []
    for index, case in enumerate(base):
        split = 'development' if index < 12 else 'validation' if index < 16 else 'frozen'
        sku = case['ticket'].get('sku')
        refs = [sources['SRC-'+sku]] if 'SRC-'+str(sku) in sources else []
        if case['ticket'].get('order_id') or case['scenario'] in {'missing_order', 'missing_delivery', 'safety', 'safety_multi'}:
            refs.append(sources['SRC-POLICY-US'])
        # Missing inputs/unknown SKU assertions cite explicit project behavior specifications.
        refs.append(dict(id='SPEC-BR-02', version='v1.2', source_type='local',
                         locator='docs/产品说明.md#关键业务规则',
                         text='Missing exact model or use case requires clarification. Unknown/conflicting evidence requires human review. Never invent product facts or external execution.'))
        for variant, prefix in enumerate(prefixes):
            ticket = deepcopy(case['ticket'])
            ticket['message'] = prefix + ticket['message']
            rows.append(dict(id=f'S2-{index*5+variant+1:03}', version=1, state='pending', source_type='local',
                             scenario=case['scenario'], scenario_family=case['id'], variant=variant+1,
                             ticket=ticket, as_of=case['assist']['as_of'], split=split,
                             expected=dict(status=case['expected_status'], required_fragments=assertions[case['scenario']],
                                           forbidden_fragments=['Your refund has been processed.', 'You can charge it again to test.']),
                             evidence_refs=refs, reviewer=None, review_scope='pending_human_not_gold'))
    bundle = dict(dataset_version='s2-candidates-v1', source_type='local', human_review='pending',
                  total=100, independent_scenarios=20, split_counts={'development':60,'validation':20,'frozen':20},
                  limitation='20基础场景各5个受控措辞变体，不是100个独立场景，不代表人工金标或模型语义通过', cases=rows)
    (ROOT/'tests/fixtures/evaluation-cases.json').write_text(json.dumps(bundle, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    print('100 pending candidates written; 20 scenario families; split 60/20/20')


if __name__ == '__main__':
    main()
