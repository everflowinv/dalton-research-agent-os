"""Chinese presentation of model metadata; calculation records stay unchanged."""
from __future__ import annotations
import re
from typing import Any, Callable, Mapping

FIELD_LABELS = {
    'revenue': '营业收入', 'revenues': '营业收入', 'gross_profit': '毛利润',
    'operating_income': '营业利润', 'net_income': '净利润',
    'parent_net_income': '归属于母公司的净利润', 'noncontrolling_interest': '少数股东损益',
    'cost_of_revenue': '营业成本', 'operating_expense': '经营费用',
    'interest_income': '利息收入', 'interest_expense': '利息费用',
    'income_tax': '所得税', 'income_tax_expense': '所得税费用', 'pretax_income': '税前利润',
    'nonoperating_income_expense': '非经营性净收益或费用',
    'other_nonoperating_income_expense': '其他非经营性净收益或费用',
    'other_operating_income_expense': '其他经营性净收益或费用',
    'operating_cash_flow': '经营现金流', 'capital_expenditure': '资本开支',
    'free_cash_flow': '自由现金流', 'diluted_eps': '摊薄每股收益',
    'diluted_weighted_average_shares': '摊薄加权平均股数',
    'company_presented_component': '公司披露的分项',
    'retained_contract_book': '存量合同', 'new_work_conversion': '新增业务收入转化',
    'realized_pricing': '实际成交价格', 'currency_translation': '汇率折算影响',
    'acquisition_perimeter': '并购带来的业务范围变化',
    'billable_delivery_capacity': '可计费交付能力',
    'infrastructure_shipments_mix': '基础设施出货结构',
    'legacy filed concept': '历史披露科目', 'computed': '已计算',
    'partial': '部分完成', 'unavailable': '暂无结果', 'unbounded': '尚未得到有效范围内的结果',
    'management_direct': '管理层直接披露',
    'assumption_review': '预测假设复核',
    'revenue_share': '收入占比',
    'operating_income_share': '营业利润占比',
    'quarterly_growth': '季度环比增速',
    'share_of_line': '占对应科目的比例',
}
CONCEPT_LABELS = {
    'us-gaap:NetCashProvidedByUsedInOperatingActivities': '经营活动产生的现金流量净额',
    'us-gaap:PaymentsToAcquirePropertyPlantAndEquipment': '购建固定资产支付',
}
ROLE_LABELS = {
    'operating-income': '营业利润',
    'fx-result': '汇兑损益',
    'interest-income': '利息收入',
    'interest-expense': '利息费用',
    'interest-other': '利息及其他收益净额',
    'other-nonoperating': '其他非经营性损益',
    'legacy-interest-expense': '历史口径利息费用',
    'income-tax': '所得税',
    'pretax-income': '税前利润',
    'nci-other': '其他少数股东损益',
    'nci-canada': '加拿大业务少数股东损益',
    'diluted-eps': '摊薄每股收益',
    'diluted-shares': '摊薄股数',
    'total-operating-expenses': '经营费用合计',
    'total-costs': '成本合计',
    'noncontrolling-income': '少数股东损益',
    'equity-method-result': '权益法投资损益',
    'service-cost': '服务成本',
    'sales-marketing': '销售与营销费用',
    'other-result': '其他损益',
    'other-income': '其他收益',
    'optimization-legacy': '历史优化项目',
    'nonoperating-result': '非经营性损益',
    'disposition-gain': '处置收益',
    'depreciation-amortization': '折旧与摊销',
    'delivery-cost': '交付成本',
    'cost-services': '服务成本',
    'business-sale': '业务出售损益',
    'asset-sale-result': '资产出售损益',
    'cost-of-revenue': '营业成本',
    'net-income': '净利润',
}
CHECK_LABELS = {
    'direction_consistency': '收入、成本与利润的变动方向',
    'assumption_band': '预测假设与历史范围的对照',
    'rate_domain': '利润率、税率等比例的合理范围',
    'segment_sum': '分项与合并数的核对',
    'period_basis': '单季、累计及实际与预测的口径',
    'solver_bounds': '计算结果是否落在设定范围内',
}
REASON_LABELS = {
    'fewer than two comparable quarters computed, so there is no direction to check': '可比较的已计算季度不足两个，暂时无法检查变动方向。',
    'no two adjacent quarters both computed with the cost ratios held': '缺少两个已完成计算、且成本占比保持不变的相邻季度，暂时无法检查变动方向。',
    'no driver has two scenarios that both computed': '每个驱动因素均不足两个已完成计算的情景，暂时无法比较情景变化。',
    'no assumption has enough filed history to have a band': '披露历史不足，暂时无法确定预测假设的历史范围。',
    'this output carries no rate-type quantity': '这项结果不含比例数值，无需检查比例范围。',
    'no line has both a consolidated figure and a breakdown to check it against': '尚无可相互核对的分项与合并数，暂时无法检查两者是否一致。',
    'this output carries no dated line to check': '这项结果未提供可用于核对期间的日期。',
    'this output rests on no solved quantity': '这项结果不依赖数值求解，无需检查求解范围。',
}

def field_label(value: Any, translate: Callable[[str], str] | None = None) -> str:
    raw = str(value or '')
    key = raw.strip().casefold().replace(' ', '_')
    if raw in FIELD_LABELS or key in FIELD_LABELS:
        return FIELD_LABELS.get(raw, FIELD_LABELS.get(key, raw))
    if raw.startswith(('result:', 'row:', 'driver:')):
        key = raw.split(':', 2)[1]
        return FIELD_LABELS.get(key, '模型项目（标识见技术详情）')
    registered = model_metadata_text(raw)
    if registered != raw:
        return registered
    shown = translate(raw) if translate else raw
    return model_metadata_text(shown)


def model_metadata_text(value: Any) -> str:
    """Replace only registered accounting concepts and model field roles."""

    text = str(value or '')
    fixed_notes = {
        'the specification names no filed counterpart for this line':
            '尚未关联这项指标的历史披露数据',
        '规范中未为该科目指定已申报的对应项':
            '尚未关联这项指标的历史披露数据',
        'this concept holds no frozen model role, so nothing is computed from it':
            '这项披露数据暂未用于预测计算',
        '该概念没有冻结的模型角色，因此不据此计算任何数值':
            '这项披露数据暂未用于预测计算',
        'this concept is not in the statements held':
            '现有财务报表中没有这项披露数据',
        'this concept appears in more than one statement':
            '这项披露数据同时出现在多张报表中，暂不用于预测计算',
    }
    for source, replacement in fixed_notes.items():
        text = text.replace(source, replacement)
    text = re.sub(
        r"(\d+) specification rows split this filed line; the total is forecast and the split is not filed",
        lambda match: f"模型将该科目拆成{match.group(1)}项；预测按合计数计算，尚无各分项的披露数据",
        text,
    )
    text = re.sub(
        r"规范中的(\d+)行对该已申报科目进行拆分；总额为预测值，拆分明细未申报",
        lambda match: f"模型将该科目拆成{match.group(1)}项；预测按合计数计算，尚无各分项的披露数据",
        text,
    )
    text = text.replace(
        'Net income attributable to non-controlling interest, net of tax'
        '（归属于非控制性权益的净利润，税后）',
        '归属于非控制性权益的净利润（税后）',
    )
    for token, label in sorted({**CONCEPT_LABELS, **ROLE_LABELS}.items(),
                               key=lambda row: -len(row[0])):
        text = re.sub(rf'(?<![A-Za-z0-9:_-]){re.escape(token)}(?![A-Za-z0-9:_-])',
                      label, text)
    # Older reviewed display maps sometimes retained a redundant English alias.
    # Remove it only when the immediately preceding Chinese label is its exact
    # registered meaning; unrelated English and quoted text remain untouched.
    aliases = {
        'Diluted weighted-average shares': '摊薄加权平均股数',
        'Diluted weighted average shares': '摊薄加权平均股数',
    }
    for alias, label in aliases.items():
        text = text.replace(f'{label}（{alias}）', label)
        text = text.replace(f'{label} ({alias})', label)
        if label == '摊薄加权平均股数':
            text = text.replace(f'稀释后加权平均股数（{alias}）', '稀释后加权平均股数')
            text = text.replace(f'稀释后加权平均股数 ({alias})', '稀释后加权平均股数')
    return text


def native_chinese_model_text(value: Any) -> str | None:
    """Accept native Chinese metadata, but not a mixed untranslated sentence."""

    text = model_metadata_text(value)
    if not any('\u4e00' <= char <= '\u9fff' for char in text):
        return None
    allowed = {'AI', 'IT', 'GAAP', 'EPS', 'FCF', 'USD', 'SG', 'A', 'NCI',
               'Q', 'FY', 'EBIT', 'EBITDA'}
    words = re.findall(r'[A-Za-z]+', text)
    return text if all(word.upper() in allowed for word in words) else None


def readiness_labels(record: Mapping[str, Any], readiness: Mapping[str, Any],
                     translate: Callable[[str], str]) -> dict[str, Any]:
    labels = {str(row['ref']): field_label(row.get('label') or row['ref'], translate)
              for name in ('drivers', 'results') for row in record.get(name) or []}
    return {**readiness, **{name + '_labels': [labels.get(ref, field_label(ref, translate))
             for ref in readiness.get(name) or []]
             for name in ('drivers_without_history', 'results_unavailable')}}


def present_invariants(values: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    for outputs in values.values():
        for item in outputs.values():
            item['technical_details'] = {key: item.get(key) for key in (
                'output_ref', 'rule_ref', 'reasons', 'failed', 'not_checked')}
            item['display_reasons'] = [
                CHECK_LABELS.get(str(row.get('invariant')), '模型一致性检查') + '未通过。'
                for row in item.get('failed') or []]
            if item.get('status') == 'unavailable' and not item['display_reasons']:
                item['display_reasons'] = ['模型检查暂无结论，具体原因保存在技术详情中。']
            item['display_not_checked'] = [{
                'label': CHECK_LABELS.get(str(row.get('invariant')), '模型一致性检查'),
                'findings': [REASON_LABELS.get(str(reason), '此项检查未完成，原始说明见技术详情。')
                             for reason in row.get('findings') or []],
            } for row in item.get('not_checked') or []]
    return values
