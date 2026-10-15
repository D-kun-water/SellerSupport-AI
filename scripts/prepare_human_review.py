"""Prepare a two-pass review packet from frozen local cases and a live report.

Pass A deliberately omits the model answer and rule verdict to reduce anchoring.
This script never fills in human labels or computes semantic accuracy.
"""
import argparse
import json
import hashlib
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.store import ROOT


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('report', type=Path)
    args = parser.parse_args()
    report = json.loads(args.report.read_text(encoding='utf-8'))
    if report['state'] != 'completed':
        raise SystemExit('Review packet requires a completed immutable report.')
    fixture=ROOT/'tests/fixtures/cases.json'
    catalog_path=ROOT/'tests/fixtures/catalog.json'
    if report['fixture_hash']!=hashlib.sha256(fixture.read_bytes()).hexdigest() or report['catalog_hash']!=hashlib.sha256(catalog_path.read_bytes()).hexdigest():
        raise SystemExit('Current fixtures differ from this report; use the original frozen fixtures.')
    target = ROOT/'output/evaluations'/f"human-review-{report['id']}.md"
    if target.exists():raise SystemExit('Review packet already exists; refusing to overwrite possible human labels.')
    cases = json.loads(fixture.read_text(encoding='utf-8'))
    catalog = json.loads(catalog_path.read_text(encoding='utf-8'))
    selected = {row['case_id']: row for row in report['results']
                if row['concurrency'] == 1 and row['repeat'] == 1}
    if len(selected) != len(cases):
        raise SystemExit('Expected one serial repeat-1 answer per frozen case.')
    orders = {order['id']: order for order in catalog['orders']}
    lines = [
        '# 客服回答审核', '',
        f"报告：`{report['id']}`；提示词：`{report['prompt_version']}`；数据：合成案例。",
        '状态：**待人工审核**。本文件中的空栏不是金标；脚本不填写语义正确率。', '',
        '先独立完成 A 的 20 条事实与业务判断，再看 B 的模型回答。对不能从资料确认的结论填“未知”，不要猜测。',
        f"A 含 20 条输入；B 含本报告全部 {len(report['results'])} 条运行，不只选首个代表答复。",
        '参考资料：`tests/fixtures/catalog.json`、`tests/fixtures/售后与物流政策.md`、`tests/fixtures/manuals/`。',
        '判定维度：事实与来源、退货/订单权限、安全处理、执行状态是否有凭据、相关性与澄清完整度、英文表达。', '',
        '## A. 独立金标（先填写）', ''
    ]
    for case in cases:
        ticket = case['ticket']
        order = orders.get(ticket.get('order_id'))
        lines += [f"### {case['id']} · {case['scenario']}", '',
                  f"客户原文：{ticket['message']}", '',
                  f"已确认字段：SKU={ticket.get('sku') or '无'}；设备={ticket.get('device_id') or '无'}；用途={ticket.get('use_case') or '无'}；市场={ticket.get('region') or '无'}；渠道={ticket.get('channel') or '无'}；订单={ticket.get('order_id') or '无'}。", '',
                  f"评测时点：{case['assist']['as_of']}。", '']
        if order:
            facts = {key: order.get(key) for key in ('id','sku','status','paid','refunded','ordered_at','shipped_at','delivered_at','last_tracking_at','observed_at','max_age_seconds') if key in order}
            lines += [f"订单事实快照：`{json.dumps(facts, ensure_ascii=False)}`", '']
        lines += ['人工期望出口（draft / need_clarification / escalated / 未知）：________',
                  '必须保留的事实、条件与时间口径：________',
                  '不得声称或执行的事项：________',
                  '依据文件与定位：________',
                  '审核人 / 日期：________', '']
    lines += ['## B. 模型回答复核（完成 A 后再看）', '',
              '每案按“通过 / 不通过 / 无法判断”填写。硬检查 REVIEW 只表示结构及已编码禁令通过。', '']
    for row in sorted(report['results'],key=lambda row:(row['case_id'],row['concurrency'],row['repeat'])):
        try:
            parsed = json.loads(row.get('raw_model_content') or '{}')
        except ValueError:
            parsed = {}
        lines += [f"### {row['case_id']} · {row['scenario']} · 并发 {row['concurrency']} / 重复 {row['repeat']} · {row['id']}", '',
                  f"硬检查：{row['quality']['status']}；问题：{', '.join(row['quality'].get('issues', [])) or '无'}。", '',
                  f"服务端基线出口：{row['baseline_status']}；缺项：{', '.join(row['missing_fields']) or '无'}。", '',
                  '模型客户草稿：', '',
                  '> ' + str(parsed.get('reply_draft') or row.get('raw_model_content') or '无内容').replace('\n', '\n> '), '',
                  f"模型内部摘要：{parsed.get('summary', '无')}。", '',
                  f"引用 ID：{', '.join(ref['id'] for ref in row.get('evidence_refs', [])) or '无'}。", '',
                  '事实/证据：________；出口与边界：________；相关性/澄清：________；表达：________',
                  '最终人工判定（通过 / 不通过 / 无法判断）：________',
                  '问题摘录及依据：________', '']
    target.write_text('\n'.join(lines), encoding='utf-8')
    print(target)


if __name__ == '__main__':
    main()
