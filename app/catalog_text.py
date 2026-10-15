"""Localized labels for source-bound compatibility rules."""
USES = {'charging': '充电', 'data': '传输数据', 'video': '视频输出'}
RESULTS = {'conditional': '满足条件时支持', 'compatible': '支持', 'incompatible': '不支持'}


def chinese_rule_header(rule):
    return f"适配规则：{rule['device_id']} / {USES[rule['use_case']]}：{RESULTS[rule['result']]}。"
