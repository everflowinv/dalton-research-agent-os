"""Deterministic display wording for known research-gap contracts."""
from __future__ import annotations
import re
from typing import Any
from .driver_template import COST_DRIVER_TEMPLATES
from .numeric_display import format_prose_date_ranges, format_prose_usd_amounts, transform_unquoted_prose

_QUESTIONS = {
 "Which product/input spread drives realised gross cost?":"哪项产品与投入品价差决定实际毛成本？",
 "How much energy is consumed per unit and at what price?":"每单位产出消耗多少能源，采购价格是多少？",
 "How does utilisation move fixed cost per unit?":"开工率变化如何影响单位固定成本？",
 "How does the installed asset base depreciate through the forecast?":"现有资产在预测期内如何折旧？",
 "What spending merely maintains current capacity?":"哪些资本开支只用于维持现有产能？",
 "What does serving and retaining contracted work cost?":"交付并维持合同业务需要多少成本？",
 "What revenue and delivery cost does each employee support?":"每名员工对应多少收入和交付成本？",
 "How does unit cost change as adoption scales?":"随着采用规模扩大，单位成本如何变化？",
 "Which fixed costs leave, by when, and with what cash cost?":"哪些固定成本会在何时退出，并产生多少现金成本？",
 "Which costs persist when revenue changes?":"收入变化时，哪些成本仍然存在？",
 "Which costs move with volume or revenue?":"哪些成本会随业务量或收入变化？",
}
_EVIDENCE={"company_figure":"公司财务数据","market_proxy":"市场替代指标",
           "own_assumption":"自有假设","qualitative":"定性证据"}
_SLOTS={slot["slot_id"]:slot["label"] for template in COST_DRIVER_TEMPLATES["templates"]
        for slot in template["cost_slots"]}
_FULL=re.compile(r"^(?P<subject>.+?) 未覆盖成本模板的「(?P<label>[^」]+)」（(?P<key>[a-z_]+)）：(?P<question>.+?) 期望证据种类：(?P<kinds>[a-z_, ]+)。$")
_PREFIX=re.compile(r"^(?P<key>[a-z_]+)(?P<sep>\s*[:：/／-]\s*)(?P<rest>.*)$")
_BAD_TAGS=re.compile(r"^模型引用了不存在的标签：(?P<tags>[^。]+)。?$")

# These are known system explanations, not a general English-word glossary.
# Match the complete wording so proper names and genuine quotations keep
# their original language.
_VALUATION_WORDINGS = (
    "按 Playbook 的数字纪律留空：市场价格、股本、汇率、利率与 consensus 五类正式 authority 尚未接入，写任何倍数或目标价都会是无来源的数字。",
    "估值一节按 Playbook 的数字纪律留空：市场价格、股本、汇率、利率与市场一致预期（consensus）五类正式权威数据源（authority）尚未接入，填入任何估值倍数或目标价都会产生无来源的数字。",
    "估值一节按 Playbook 的数字使用规则留空：市场价格、股本、汇率、利率与市场一致预期（consensus）五类正式 authority 尚未接入，写出任何倍数或目标价都会是无来源的数字。",
    "估值一节按 Playbook 的数字纪律留空：市场价格、股本、汇率、利率与市场一致预期（consensus）五类正式权威数据源尚未接入，写入任何估值倍数或目标价都会缺乏数据来源支撑。",
)
_CLOSED_WORDING = {
    **{text: "估值部分暂时留空。按研究手册的要求，需要先接入市场价格、股本、汇率、利率和市场一致预期这五类正式数据，再计算估值倍数与目标价。"
       for text in _VALUATION_WORDINGS},
    "由人类确定方向、提出初始投资逻辑，并在检查点作出裁决；其余工作由自动化系统（automation）在 playbook、constitution、mandate 与预算规定的范围内自主推进。":
        "由你确定研究方向、提出初始投资判断，并确认关键决策；系统按研究手册、研究章程、授权范围和预算自主推进其余工作。",
    "人类负责方向、初始论点和检查点裁决，automation 在既定规则与预算内执行。":
        "你负责研究方向、初始判断和关键决策，系统按既定规则和预算执行。",
    "花费占 mission 周预算上限": "花费占任务周预算上限",
    "本周实际花费不足 mission 预算上限": "本周实际花费不足任务预算上限",
    "计价调用共": "计费调用共",
    "本记录只读不写：不写入 Ledger、不修改 policy、不登记问题。":
        "这是一份只读检查记录。",
}

_DISPLAY_TERMS = {
    **_SLOTS,
    **_EVIDENCE,
    "insufficient_data": "资料不足",
    "not_drafted_this_run": "本轮尚未起草",
    "cost_structure": "成本结构",
    "unit_economics": "单位经济性",
    "milestones": "关键里程碑",
    "cash_runway": "现金可支撑期限",
    "pricing": "定价",
    "retention": "客户留存",
    "volume": "业务量",
    "price": "实现价格",
    "revenue": "营业收入",
    "revenues": "营业收入",
    "revenue_yoy_growth": "营业收入同比增速",
    "operating_margin": "营业利润率",
    "gross_margin": "毛利率",
    "diluted_eps": "稀释每股收益",
    "free_cash_flow": "自由现金流",
    "ah_premium": "A/H 股溢价",
    "announcements_index": "公告索引",
    "blind_reviews": "匿名审阅",
    "blocks_links": "屏蔽链接",
    "candidate_sources": "候选来源",
    "connection_status": "连接状态",
    "content_kind": "内容类型",
    "cost_note": "成本说明",
    "covered_driver_refs": "已覆盖驱动因素引用",
    "daily_quota": "每日额度",
    "daily_unit_limit": "每日单位上限",
    "disclosure_of_interests": "权益披露",
    "driver_refs": "驱动因素引用",
    "employee_review": "员工评价",
    "evidence_tier": "证据层级",
    "expert_excerpt": "专家访谈摘录",
    "expert_network": "专家网络",
    "fetch_get": "网页获取",
    "financial_statements": "财务报表",
    "gap_ref": "缺口引用",
    "get_document": "读取文档",
    "get_note": "读取笔记",
    "in_inventory": "已纳入资料库",
    "internal_wiki": "内部知识库",
    "list_documents": "文档列表",
    "list_filings": "公告列表",
    "list_notes": "笔记列表",
    "management_minutes": "管理层会议纪要",
    "margin_balance": "融资余额",
    "monthly_returns": "月报表",
    "news_media": "新闻媒体",
    "next_day_disclosure_returns": "翌日披露报表",
    "northbound_flow": "北向资金流",
    "not_connected": "尚未连接",
    "primary_filing": "公司正式公告",
    "probe_only": "仅作探测",
    "quota_unit": "额度单位",
    "sales_note": "销售笔记",
    "search_library": "检索资料库",
    "search_web": "网页搜索",
    "sell_side": "卖方研究",
    "sell_side_report": "卖方研报",
    "source_ref": "来源引用",
    "vendor_note": "供应商笔记",
    "verification_kind": "核验类型",
    "web_page": "网页",
    "what_is_missing": "待补内容",
}
_TERM_PATTERN = re.compile(
    r"(?<![A-Za-z0-9_])("
    + "|".join(re.escape(key) for key in sorted((key for key in _DISPLAY_TERMS if "_" in key), key=len, reverse=True))
    + r")(?![A-Za-z0-9_])", re.IGNORECASE)

_EMBEDDED_PERIOD = re.compile(
    r"(?<![A-Za-z0-9])(?:(?:FY|CY)(?:20)?\d{2}\s+Q[1-4]|"
    r"Q[1-4]\s+(?:FY|CY)(?:20)?\d{2}|"
    r"FY(?:20)?\d{2}(?:Q[1-4])?|CY(?:20)?\d{2}(?:Q[1-4])?|"
    r"20\d{2}Q[1-4]|Q[1-4]\s+20\d{2})(?![A-Za-z0-9])",
    re.IGNORECASE,
)


def _display_embedded_periods(part: str) -> str:
    def replace(match: re.Match[str]) -> str:
        raw = match.group(0)
        fiscal_quarter = re.fullmatch(
            r"(?:(FY|CY)((?:20)?\d{2})\s+Q([1-4])|Q([1-4])\s+(FY|CY)((?:20)?\d{2}))",
            raw, re.IGNORECASE)
        if fiscal_quarter:
            basis = fiscal_quarter[1] or fiscal_quarter[5]
            year_text = fiscal_quarter[2] or fiscal_quarter[6]
            quarter = fiscal_quarter[3] or fiscal_quarter[4]
            full_year = year_text if len(year_text) == 4 else f"20{year_text}"
            return (f"{full_year}{'财年' if basis.upper() == 'FY' else '自然年'}"
                    f"第{'一二三四'[int(quarter) - 1]}季度")
        alternate = re.fullmatch(r"Q([1-4])\s+(20\d{2})", raw, re.IGNORECASE)
        if alternate:
            return f"{alternate[2]}年第{'一二三四'[int(alternate[1]) - 1]}季度"
        year = re.fullmatch(r"(FY|CY)((?:20)?\d{2})", raw, re.IGNORECASE)
        if year:
            full_year = year[2] if len(year[2]) == 4 else f"20{year[2]}"
            return f"{full_year}{'财年' if year[1].upper() == 'FY' else '自然年'}"
        from .cockpit_plane import claim_period_display_label
        return claim_period_display_label(raw) or raw

    return _EMBEDDED_PERIOD.sub(replace, part)

def display_metadata_text(value: Any) -> str:
    """Replace only registered machine metadata tokens in reader-facing text."""
    text = str(value) if value is not None else ""
    for original, readable in _CLOSED_WORDING.items():
        text = text.replace(original, readable)
    # Ordinary English words may be part of proper names or source quotes.
    # Translate those only when the entire value is an exact metadata key.
    if text in _DISPLAY_TERMS:
        return _DISPLAY_TERMS[text]
    text = _TERM_PATTERN.sub(lambda match: _DISPLAY_TERMS[match.group(1).lower()], text)
    text = transform_unquoted_prose(
        text,
        lambda part: _display_embedded_periods(
            part.replace("discretionary spending", "可自由支配支出")
                .replace("discretionary 支出", "可自由支配支出")),
    )
    return format_prose_date_ranges(format_prose_usd_amounts(text))

def gap_display_text(value: Any) -> str:
    """Return friendly Chinese for a closed known gap; preserve everything else."""
    text=str(value) if value is not None else ""
    match=_FULL.fullmatch(text)
    if match and match["key"] in _SLOTS and match["label"]==_SLOTS[match["key"]] and match["question"] in _QUESTIONS:
        kinds=[item.strip() for item in match["kinds"].split(',')]
        if kinds and all(item in _EVIDENCE for item in kinds):
            subject=("公司档案的“供给与成本”章节" if match["subject"]=="档案 supply_and_cost"
                     else match["subject"].replace(" supply_and_cost","的“供给与成本”章节"))
            return f"{subject}尚未覆盖“{match['label']}”：{_QUESTIONS[match['question']]}需要的证据：{'、'.join(_EVIDENCE[x] for x in kinds)}。"
    if text in _SLOTS:return _SLOTS[text]
    match=_PREFIX.fullmatch(text)
    if match and match["key"] in _SLOTS:
        return _SLOTS[match["key"]]+match["sep"]+match["rest"]
    match=_BAD_TAGS.fullmatch(text)
    if match:return f"模型引用了未纳入依据的内部标签：{match['tags']}。"
    return display_metadata_text(text)

__all__=["display_metadata_text", "gap_display_text"]
