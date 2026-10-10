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
    'diluted_shares': '摊薄股数',
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
    'rnd': '研发费用', 'sga': '销售、一般及管理费用', 'nci': '少数股东损益',
    'other_income': '其他收益',
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
    'cost-of-services': '服务成本',
    'business-optimization': '业务优化费用',
    'total-operating-expenses': '经营费用合计',
    'loss-on-sale': '业务出售损失',
    'fx-loss': '汇兑损失',
    'gain-on-disposition': '业务处置收益',
    'g-and-a': '一般及管理费用',
    'd-and-a': '折旧与摊销',
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

# 2026-10-08 (DEFECT-02): a statement line's name is structured metadata, not
# prose.  The model table printed 「正文正在检查文字表达，完成后会显示。」 in the
# label column whenever a filer's English line name had no reviewed
# translation.  Line names are shown from this table (the filer's usual
# English line names, by meaning), else the concept's Chinese account name,
# else the line's own role, else the name as filed -- never the prose gate.
LINE_LABELS = {
    'revenue': '营业收入', 'revenues': '营业收入', 'total revenues': '营业收入',
    'net revenues': '营业收入', 'total net sales': '营业收入', 'net sales': '营业收入',
    'cost of revenue': '营业成本', 'cost of revenues': '营业成本',
    'cost of sales': '营业成本', 'cost of services': '服务成本', 'costs of services': '服务成本',
    'cost of revenues excl. d&a': '营业成本（不含折旧摊销）',
    'cost of revenue excl. d&a': '营业成本（不含折旧摊销）',
    'gross profit': '毛利润', 'gross margin': '毛利润',
    'research and development': '研发费用', 'research and development expense': '研发费用',
    'technology and development': '技术与开发费用', 'technology and infrastructure': '技术与基础设施费用',
    'sales and marketing': '销售与营销费用', 'marketing and sales': '销售与营销费用',
    'marketing': '营销费用', 'fulfillment': '履约费用',
    'general and administrative': '一般及管理费用', 'general and administrative costs': '一般及管理费用',
    'general and administrative expense': '一般及管理费用',
    'sg&a': '销售、一般及管理费用', 'selling, general and administrative': '销售、一般及管理费用',
    'selling, general and administrative expense': '销售、一般及管理费用',
    'business optimization costs': '业务优化费用', 'restructuring costs': '重组费用',
    'restructuring charges': '重组费用',
    'depreciation and amortization': '折旧与摊销',
    'total operating expenses': '经营费用合计', 'total costs and expenses': '成本与费用合计',
    'operating income': '营业利润', 'income from operations': '营业利润',
    'operating income (loss)': '营业利润',
    'interest income': '利息收入', 'interest expense': '利息费用',
    'other income (expense), net': '其他收益（费用）净额', 'other income, net': '其他收益净额',
    'other income (expense)': '其他收益（费用）',
    'interest and other income (expense), net': '利息及其他收益（费用）净额',
    'interest and other income (loss), net': '利息及其他收益（损失）净额',
    'foreign exchange loss': '汇兑损失', 'foreign exchange gain (loss)': '汇兑损益',
    'loss on sale of business': '业务出售损失', 'gain on disposition of businesses': '业务处置收益',
    'income before income taxes': '税前利润', 'income before provision for income taxes': '税前利润',
    'income tax expense': '所得税费用', 'provision for income taxes': '所得税费用',
    'provision (benefit) for income taxes': '所得税费用', 'income taxes': '所得税费用',
    'net income': '净利润', 'net income (loss)': '净利润',
    'net income attributable to nci': '少数股东损益', 'nci – other': '其他少数股东损益',
    'nci - other': '其他少数股东损益',
    'diluted weighted-average shares': '摊薄加权平均股数',
    'diluted weighted average shares': '摊薄加权平均股数',
    'diluted eps': '摊薄每股收益', 'diluted earnings per share': '摊薄每股收益',
    'basic eps': '基本每股收益',
    'operating cash flow': '经营现金流', 'net cash provided by operating activities': '经营现金流',
    'capital expenditure': '资本开支', 'capital expenditures': '资本开支',
    'purchases of property and equipment': '资本开支', 'free cash flow': '自由现金流',
}


def statement_line_label(row: Mapping[str, Any],
                         reviewed: Callable[[str], str | None] | None = None) -> str:
    """The Chinese name of one model line (driver or result); never gated.

    ``reviewed`` returns a reviewed translation (or native Chinese) for a
    label, else ``None``.  Order: reviewed text, the line-name table, the
    filed concept's Chinese account name, the line's role from its ref, the
    name as filed.
    """

    raw = str(row.get('label') or row.get('ref') or '')
    if reviewed is not None:
        shown = reviewed(raw)
        if shown:
            return shown
    native = native_chinese_model_text(raw)
    if native is not None:
        return native
    key = re.sub(r'\s+', ' ', raw.strip().casefold())
    if key in LINE_LABELS:
        return LINE_LABELS[key]
    concept = str(row.get('concept') or '')
    ref = str(row.get('ref') or '')
    if not concept and ref.startswith('concept:'):
        concept = ref.split(':', 1)[1]
    name = concept_account_label(concept)
    if name:
        return name
    if ref.startswith(('result:', 'row:', 'driver:')):
        parts = ref.split(':')
        if len(parts) > 2 and parts[2] in ROLE_LABELS:
            return ROLE_LABELS[parts[2]]
        if parts[1] in FIELD_LABELS and len(parts) == 2:
            return FIELD_LABELS[parts[1]]
    return raw


def concept_account_label(concept: Any) -> str | None:
    """A filed XBRL concept's Chinese account name, or ``None`` if unknown."""

    text = str(concept or '').strip()
    if not text:
        return None
    if text in CONCEPT_LABELS:
        return CONCEPT_LABELS[text]
    from .quantitative_claim_promotion import CONCEPT_METRICS
    if text in CONCEPT_METRICS:
        return CONCEPT_METRICS[text][1]
    return XBRL_ACCOUNT_LABELS.get(text)


# Filed concepts outside the promotion table that the cockpit shows by name
# (sources, model lines).  Unknown concepts keep their id under 技术详情.
XBRL_ACCOUNT_LABELS = {
    'us-gaap:CostOfGoodsAndServicesSold': '营业成本',
    'us-gaap:SellingAndMarketingExpense': '销售与营销费用',
    'us-gaap:GeneralAndAdministrativeExpense': '一般及管理费用',
    'us-gaap:RestructuringSettlementAndImpairmentProvisions': '重组、和解及减值费用',
    'us-gaap:RestructuringCharges': '重组费用',
    'us-gaap:CostsAndExpenses': '成本与费用合计',
    'us-gaap:OperatingExpenses': '经营费用合计',
    'us-gaap:InvestmentIncomeInterest': '利息收入',
    'us-gaap:InterestIncomeOperating': '利息收入',
    'us-gaap:InterestExpense': '利息费用',
    'us-gaap:InterestExpenseNonoperating': '利息费用',
    'us-gaap:OtherNonoperatingIncomeExpense': '其他营业外收益（费用）',
    'us-gaap:NonoperatingIncomeExpense': '营业外收益（费用）净额',
    'us-gaap:NonoperatingGainsLosses': '营业外损益',
    'us-gaap:OtherOperatingIncomeExpenseNet': '其他经营收益净额',
    'us-gaap:IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest': '税前利润',
    'us-gaap:IncomeTaxExpenseBenefit': '所得税费用',
    'us-gaap:NetIncomeLossAttributableToNoncontrollingInterest': '少数股东损益',
    'us-gaap:NoncontrollingInterestInNetIncomeLossOtherNoncontrollingInterestsNonredeemable': '其他少数股东损益',
    'us-gaap:NoncontrollingInterestInNetIncomeLossOtherNoncontrollingInterestsRedeemable': '可赎回少数股东损益',
    'us-gaap:DepreciationAndAmortization': '折旧与摊销',
    'us-gaap:DepreciationDepletionAndAmortization': '折旧、损耗与摊销',
    'us-gaap:GainLossOnSaleOfBusiness': '业务出售损益',
    'us-gaap:ForeignCurrencyTransactionGainLossBeforeTax': '汇兑损益',
    'us-gaap:WeightedAverageNumberOfDilutedSharesOutstanding': '摊薄加权平均股数',
    'us-gaap:WeightedAverageNumberOfSharesOutstandingBasic': '基本加权平均股数',
    'us-gaap:PaymentsToAcquireProductiveAssets': '购置生产性资产支付的现金',
    'us-gaap:PaymentsOfDividendsCommonStock': '支付普通股股利',
    'us-gaap:PaymentsOfDividends': '支付股利',
    'us-gaap:LongTermDebt': '长期债务',
    'us-gaap:LongTermDebtNoncurrent': '长期债务（非流动）',
    'us-gaap:CashAndCashEquivalentsAtCarryingValue': '现金及现金等价物',
    'us-gaap:Assets': '总资产', 'us-gaap:Liabilities': '总负债',
    'us-gaap:StockholdersEquity': '股东权益',
    'us-gaap:OperatingIncomeLoss': '营业利润',
    'us-gaap:ShareBasedCompensation': '股份支付费用',
    'us-gaap:AllocatedShareBasedCompensationExpense': '股份支付费用',
    'us-gaap:RevenueRemainingPerformanceObligation': '剩余履约义务',
    'us-gaap:ContractWithCustomerLiability': '合同负债',
    'us-gaap:AccountsReceivableNetCurrent': '应收账款净额',
    'us-gaap:Goodwill': '商誉',
    'dei:EntityCommonStockSharesOutstanding': '已发行普通股股数',
}


def field_label(value: Any, translate: Callable[[str], str] | None = None) -> str:
    raw = str(value or '')
    key = raw.strip().casefold().replace(' ', '_')
    if raw in FIELD_LABELS or key in FIELD_LABELS:
        return FIELD_LABELS.get(raw, FIELD_LABELS.get(key, raw))
    if raw in ROLE_LABELS:
        return ROLE_LABELS[raw]
    if key.replace('-', '_') in FIELD_LABELS:
        return FIELD_LABELS[key.replace('-', '_')]
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
    text = re.sub(
        r'the trailing filed history through (\d{4}-\d{2}-\d{2}) '
        r'for structure line (.+?) is carried forward unchanged',
        lambda match: f'沿用截至 {match.group(1)} 已申报历史中的'
                      f'{field_label(match.group(2))}结构，保持不变',
        text,
    )
    text = re.sub(
        r'the average share of revenue over the (\d+) quarters filed between '
        r'(\d{4}-\d{2}-\d{2}) and (\d{4}-\d{2}-\d{2}) \((-?[\d.]+%)\), '
        r'carried forward against revenue by the cash-flow companion',
        lambda match: f'沿用 {match.group(2)} 至 {match.group(3)} 已披露 {match.group(1)} '
                      f'个季度占营业收入的平均比例（{match.group(4)}），按营业收入外推',
        text,
    )
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
        'Total costs and expenses（成本与费用合计）': '成本与费用合计',
        'Costs of services excluding D&A and restructuring（服务成本，不含折旧摊销及重组费用）':
            '服务成本（不含折旧摊销及重组费用）',
        'Selling, general and administrative excluding D&A and restructuring（销售及管理费用，不含折旧摊销及重组费用）':
            '销售及管理费用（不含折旧摊销及重组费用）',
        'Depreciation and amortization（折旧与摊销）': '折旧与摊销',
        'Restructuring costs（重组费用）': '重组费用',
        'Gain on disposition of businesses（业务处置收益）': '业务处置收益',
        'Other income, net（其他收益净额）': '其他收益净额',
        'Net income attributable to DXC common stockholders（归属于 DXC 普通股股东的净利润）':
            '归属于 DXC 普通股股东的净利润',
    }
    for source, replacement in fixed_notes.items():
        text = text.replace(source, replacement)
    # Batch am: any 「本行为X（Y）科目…」 sentence a translator made of a label.
    from .financial_display import strip_label_filler

    text = strip_label_filler(text)
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
                     translate: Callable[[str], str], *,
                     line_label: Callable[[Mapping[str, Any]], str] | None = None,
                     ) -> dict[str, Any]:
    labels = {}
    for name in ('drivers', 'results'):
        for row in record.get(name) or []:
            ref = str(row['ref'])
            if line_label is not None:
                labels[ref] = line_label(row)
                continue
            raw_label = row.get('label') or ref
            registered = field_label(raw_label)
            shown = registered if registered != str(raw_label) else field_label(raw_label, translate)
            # Some historical labels are explanatory sentences. They belong
            # in the report notes, not in the compact missing-item heading.
            if len(shown) > 36 or shown.endswith(('。', '；')):
                compact = field_label(ref)
                if compact != '模型项目（标识见技术详情）':
                    shown = compact
            labels[ref] = shown
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


# WP-A/A2 display: the routing model page, in the owner's language.
#
# The cooldown itself is a routing decision recorded in the router database
# (``model_profile_cooldowns``, via ``model_profile_health``): once an endpoint
# has proved it is not answering, selection stops offering it, so no call is
# made and no budget is reserved.  That is the right behaviour and it is also
# invisible -- a brain chain quietly running on its second link for six hours
# looks exactly like a chain whose first link was never chosen.  On 2026-09-16
# that invisibility cost a morning's pools.
#
# So every chain row the model page draws says it: which model is being held
# back, until when, and why.  Read-only; the page has no button here, because a
# cooldown is not a setting -- it expires, or the owner changes the chain.
COOLDOWN_REASON_LABELS = {
    'provider_billing': '供应商账户余额不足（HTTP 402），充值后自动恢复',
    'provider_rate_limited': '连续限流（HTTP 429）',
    'provider_unavailable': '供应商或网关不出结果（空回复，常见于 CLI 用量上限）',
    'provider_failure_streak': '连续多次供应商失败',
    'probe_failed': '冷却到期后的探测调用又失败',
}


def cooldown_index(cooldowns: Any) -> dict[str, dict[str, Any]]:
    """The active cooldowns from ``routing_overview()``, keyed by profile id.

    Takes either the whole ``cooldowns`` block or just its ``active`` list, so
    the caller does not have to know which shape it happens to be holding.
    """

    active = cooldowns.get('active') if isinstance(cooldowns, Mapping) else cooldowns
    if not isinstance(active, (list, tuple)):
        return {}
    return {str(item['profile_id']): dict(item) for item in active
            if isinstance(item, Mapping) and item.get('profile_id')}


def _cooldown_clock(value: Any) -> str | None:
    """``HH:MM（UTC）`` from an RFC3339 instant, or ``None`` if unreadable.

    UTC and said so, rather than the reader's local time: the page hands raw
    instants to the browser everywhere else, and a bare ``06:30`` rendered
    server-side would be the one time on the page in a different zone.
    """

    from datetime import datetime, timezone

    if not isinstance(value, str) or not value:
        return None
    try:
        moment = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).strftime('%H:%M') + '（UTC）'


def cooldown_note(cooldown: Mapping[str, Any] | None) -> str | None:
    """One line for one held-back model: 供应商冷却中，至 HH:MM，原因：…"""

    if not isinstance(cooldown, Mapping) or not cooldown:
        return None
    reason = COOLDOWN_REASON_LABELS.get(str(cooldown.get('reason')), '供应商连续失败')
    code = cooldown.get('failure_code')
    if (isinstance(code, str) and code
            and cooldown.get('reason') not in {'provider_billing', 'provider_rate_limited'}):
        # 2026-10-07: "the probe failed again" fourteen times said nothing
        # about *what* failed; the broker's code, in Chinese, is the half of
        # the reason that tells the owner what to do.
        from .model_profile_health import failure_code_label
        reason = f'{reason}（{failure_code_label(code)}）'
    clock = _cooldown_clock(cooldown.get('until'))
    streak = cooldown.get('streak')
    repeat = ('' if not isinstance(streak, int) or streak < 2
              else f'，已是第 {streak} 次')
    return ('供应商冷却中'
            + (f'，至 {clock}' if clock else '')
            + f'，原因：{reason}{repeat}。冷却期内不选它，也不会为它预留预算。')


def chain_link_note(existing: Any, cooldown: Mapping[str, Any] | None) -> str | None:
    """Fold the cooldown line into whatever note the link already carried."""

    held = cooldown_note(cooldown)
    if held is None:
        return existing if isinstance(existing, str) and existing else None
    if isinstance(existing, str) and existing:
        return f'{existing}；{held}'
    return held


def purpose_cooldown_note(chain: Any, index: Mapping[str, Mapping[str, Any]],
                          display_name: Callable[[str], str] | None = None) -> str | None:
    """The row's own heading: which of this stage's models are being held back.

    Named rather than counted, and it says what the chain does next, because
    "one of your models is cooling" and "this stage has nothing left" are very
    different pieces of news and the difference is the whole point.
    """

    if not isinstance(chain, (list, tuple)) or not index:
        return None
    ordered = [str(link.get('profile_id') or link.get('model') or '')
               for link in chain if isinstance(link, Mapping)]
    held = [profile_id for profile_id in ordered if profile_id in index]
    if not held:
        return None
    shown = '、'.join((display_name(profile_id) if display_name else profile_id)
                     for profile_id in held)
    remaining = [profile_id for profile_id in ordered if profile_id not in index]
    if not remaining:
        return (f'这一环的模型都在供应商冷却中（{shown}），暂时无法调用；'
                '冷却结束后会自动恢复，也可以在这一行改选其他模型。')
    following = (display_name(remaining[0]) if display_name else remaining[0])
    return f'{shown} 正在供应商冷却中，这一环暂时由 {following} 承接。'


def cooldown_view(cooldown: Mapping[str, Any] | None,
                  display_name: Callable[[str], str] | None = None) -> dict[str, Any] | None:
    """One cooled profile as the model page marks it wherever it appears.

    The same fields for every chain, the picker and the routing card, so the
    page can put one badge on one model id without knowing which chain it is
    drawing: since when, until when, why (reason and the broker's code in
    Chinese) and the line already used under the stage.
    """

    if not isinstance(cooldown, Mapping) or not cooldown.get('profile_id'):
        return None
    from .model_profile_health import failure_code_label

    profile_id = str(cooldown['profile_id'])
    return {
        'profile_id': profile_id,
        'display_name': display_name(profile_id) if display_name else profile_id,
        'reason': cooldown.get('reason'),
        'reason_label': COOLDOWN_REASON_LABELS.get(str(cooldown.get('reason')), '供应商连续失败'),
        'failure_code': cooldown.get('failure_code'),
        'failure_code_label': failure_code_label(cooldown.get('failure_code')),
        'started_at': cooldown.get('started_at'),
        'until': cooldown.get('until'),
        'until_label': _cooldown_clock(cooldown.get('until')),
        'streak': cooldown.get('streak'),
        'badge': '冷却中' + (f'，至 {_cooldown_clock(cooldown.get("until"))}'
                            if _cooldown_clock(cooldown.get('until')) else ''),
        'note': cooldown_note(cooldown),
    }
