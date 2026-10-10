"""Batch as: research reasoning lint, deterministic where it can be.

The 2026-10-09 product review found the same nine reasoning errors across
companies -- TCV/RPO read as near-term revenue, one broker called 「共识」, a
fixed lag asserted without queue evidence, a classification label with no
counter-check, management's 「赢份额」 stated as fact, survey intent read as
realisation, a price move attributed without a benchmark, a product
improvement pushed straight to ROI, USD revenue read as local-currency
organic growth.

Each rule here has two halves:

* a **deterministic** detector (sentence-level regex over the prose, plus the
  section's citation kinds where the rule is about a source) that the product
  verifiers and the quality score run on every draft;
* a **verifier instruction**, for what a regex cannot decide, which the
  model verifier is handed verbatim.

The registry is versioned and hashed so a score binds the lint it ran.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from .store import content_hash

LINT_VERSION = "research-reasoning-lint:4"

_SENTENCE_RE = re.compile(r"[^。！？；!?\n]+[。！？；!?]?")


def _sentences(text: str) -> list[str]:
    return [m.group(0).strip() for m in _SENTENCE_RE.finditer(text or "") if m.group(0).strip()]


@dataclass(frozen=True)
class LintRule:
    rule_id: str
    title: str
    instruction: str
    # Sentence must match ALL of ``triggers`` ...
    triggers: tuple[str, ...]
    # ... and NONE of ``exempt`` (in the same sentence) to be a finding.
    exempt: tuple[str, ...] = ()
    # If set, the rule is exempt when the section cites a source of one of
    # these kinds (e.g. a consensus authority for 「共识」).
    exempt_source_kinds: tuple[str, ...] = ()
    # If set, exempt when the *section* (not just the sentence) matches.
    section_exempt: tuple[str, ...] = ()

    def body(self) -> dict[str, Any]:
        return {"rule_id": self.rule_id, "title": self.title, "instruction": self.instruction,
                "triggers": list(self.triggers), "exempt": list(self.exempt),
                "exempt_source_kinds": list(self.exempt_source_kinds),
                "section_exempt": list(self.section_exempt)}


_REVENUE = r"(?:收入|营收|revenue)"
_BACKLOG_TERM = r"TCV|RPO|backlog|剩余履约义务|在手订单|新签订单|订单额|bookings"
_BACKLOG_REVENUE = rf"(?:转化为|转为|对应|支撑|带来|意味着|锁定|保障)[^。；]{{0,12}}{_REVENUE}|{_REVENUE}[^。；]{{0,8}}(?:可见|锁定)"
RULES: tuple[LintRule, ...] = (
    LintRule(
        "backlog_as_near_term_revenue", "TCV/RPO/订单不等于近期收入",
        "TCV、RPO、backlog、新签订单不能当作近期收入；要推近期收入必须给出转化节奏"
        "（多少在 12 个月内确认、历史转化率或确认周期），否则只写订单本身。"
        "收入增速与订单指标同时作为观察条件，并未声称订单直接转近期收入。",
        (_BACKLOG_TERM, _BACKLOG_REVENUE),
        exempt=(r"转化率|转化节奏|转化周期|确认周期|\d+\s*个月内(?:确认|转化)|未来\s*12\s*个月|next\s*12\s*months|"
                r"历史转化|确认节奏|收入确认",),
    ),
    LintRule(
        "consensus_without_authority", "「共识」必须引用共识来源",
        "写「共识/一致预期」必须引用 consensus authority（一致预期数据源）；只有一家券商时写"
        "「某券商认为」或券商名，不得称共识。"
        "仅假设未来一致预期上修/下修不等于声称当前共识；当前水平或已发生变动仍须有源。",
        (r"共识|一致预期|consensus|市场预期",),
        exempt=(r"某券商|券商认为|据[^。；]{0,10}(?:券商|证券|银行)", r"买方共识",
                # Saying a consensus is *missing* asserts none (「缺卖方的一致预期」,
                # 「材料里没有一致预期」); the same clause only -- a comma ends it.
                r"(?:没有|缺少?|尚无|暂无|未见|找不到|不可得)[^。；，,]{0,15}(?:共识|一致预期|consensus|市场预期)",
                r"(?:共识|一致预期|consensus)[^。；，,]{0,6}(?:不可得|缺失|缺位|unavailable)",
                r"\bno (?:street )?consensus\b"),
        exempt_source_kinds=("consensus",),
    ),
    LintRule(
        "fixed_lag_uncalibrated", "固定滞后需队列证据",
        "「领先/滞后 N 个季度」这类固定滞后需要队列或历史回测证据；没有就标「未校准假设」。",
        (r"(?:领先|滞后|传导|lag|lead)[^。；]{0,10}(?:\d+|[一二两三四五六七八九十]+)\s*(?:[-–~至到]\s*(?:\d+|[一二两三四五六七八九十]+)\s*)?(?:个)?(?:季度|个月|月|quarters?|months?)",),
        exempt=(r"未校准假设|队列|cohort|回测|历史转化|校准|backtest",),
    ),
    LintRule(
        "classification_without_counter_check", "分类标签需反例检查",
        "把公司归为「合同型 compounder / 结构成长 / 资本周期 / 商品周期 / 转型」等类型时，"
        "同一节必须写反例检查（哪些事实不符合这个分类）。",
        (r"合同型\s*compounder|contract\s*compounder|合[同约]型复利企业|结构性?成长|资本周期|商品周期|转型(?:公司|企业)|周期股|防御型",),
        section_exempt=(r"反例|反证|反方证据|不符合|例外|counter-?example|但[^。；]{0,20}(?:不同|并不)",),
    ),
    LintRule(
        "management_share_gain_as_fact", "管理层「赢份额」是公司观点",
        "「赢份额/份额提升」若来自管理层，必须写成公司观点（管理层称…），并给出独立份额证据才可作为事实。",
        (r"赢得?(?:了)?(?:市场)?份额|份额(?:提升|上升|扩大)|夺取份额|gain(?:ing|ed)?\s+share|share\s+gains?",),
        exempt=(r"管理层|公司称|公司表示|据公司|公司认为|公司观点|management|CEO|CFO|电话会上称|第三方|市场份额数据|IDC|Gartner",),
    ),
    LintRule(
        "survey_intent_as_realisation", "调研意愿不等于兑现",
        "调研/问卷里的意愿或计划不等于实际支出或收入兑现；必须写成意愿，并说明兑现要看什么。",
        (r"调研|问卷|survey|CIO\s*调查|渠道调查",
         r"将(?:带来|推动|转化)|意味着|证明|确认|兑现|will\s+(?:drive|boost)"),
        exempt=(r"意愿|计划层面|尚待兑现|未必兑现|待验证|intent",),
    ),
    LintRule(
        "price_move_without_benchmark", "价格归因须有基准",
        "把股价涨跌归因于某件事，必须同时给出基准（大盘/行业/同业 basket）的同期表现。",
        (r"股价[^。；]{0,8}(?:上涨|下跌|大涨|大跌|跑赢|跑输|反弹|回撤)|涨幅|跌幅|outperform|underperform",
         r"因为|由于|反映|推动|归因|受[^。；]{0,10}(?:影响|拖累|提振)|驱动"),
        exempt=(r"SPY|标普|S&P|指数|行业|同业|板块|basket|相对|超额|基准|SOX|纳指|Nasdaq|大盘",),
    ),
    LintRule(
        "product_to_roi_without_economics", "产品改善推 ROI 需增量利润/资本成本",
        "产品改善、新功能或 AI 能力不能直接推出 ROI/回报提升；要给出增量利润与所需资本或成本。",
        (r"产品(?:改善|升级|迭代|力提升)|新功能|AI\s*能力|功能改进",
         r"ROI|ROIC|回报率?(?:提升|改善)|利润率(?:提升|改善)|盈利能力(?:提升|改善)"),
        exempt=(r"增量利润|资本成本|WACC|投入资本|incremental|capex|资本开支|单位经济|增量成本",),
    ),
    LintRule(
        "usd_revenue_as_organic", "美元收入不代替本币有机增长",
        "美元口径收入增速不等于本币/有机增长；并购与汇率贡献必须拆开写。"
        "同期间明确以百分点披露的增长贡献须与报告增速对账；不同期间或增速与百分点不得混加。",
        (r"美元(?:口径)?(?:收入|营收)|USD\s+revenue|报告口径",
         r"有机|organic|本币|内生"),
        exempt=(r"汇率|外汇|FX|并购|收购|acquisition|剔除",),
    ),
)
RULE_IDS: tuple[str, ...] = tuple(rule.rule_id for rule in RULES)

VERIFIER_LINT_INSTRUCTIONS: tuple[str, ...] = tuple(
    f"[{rule.rule_id}] {rule.instruction}" for rule in RULES
)


def lint_fingerprint() -> str:
    return content_hash({"version": LINT_VERSION, "rules": [rule.body() for rule in RULES]})


def source_kinds_for_refs(refs: Iterable[Any], aliases: Mapping[str, Any] | None = None) -> set[str]:
    """Classify cited authorities, never a source merely present in the pack."""
    kinds: set[str] = set()
    aliases = aliases or {}
    for ref in refs:
        resolved = aliases.get(str(ref).strip("[]"), ref)
        if isinstance(resolved, Mapping):
            resolved = resolved.get("ref") or resolved.get("version_ref") or ""
        if str(resolved).startswith(("consensus-estimate-version:", "consensus-estimate:")):
            kinds.add("consensus")
    return kinds


_GROWTH_LABELS = {
    "reported": r"(?:美元(?:口径)?(?:收入|营收)|报告口径(?:收入|营收)?|reported(?:\s+revenue)?|USD\s+revenue)",
    "organic": r"(?:有机(?:收入|营收)?|organic(?:\s+revenue)?)",
    "fx": r"(?:汇率|外汇|FX|foreign exchange)",
    "acquisition": r"(?:并购|收购|acquisitions?|M&A)",
}
_GROWTH_NUMBER = r"\s*(?:增长|增速|同比|贡献|影响|拉动|拖累|growth|contribution|impact|of|为|是|:|：|\s){0,8}\s*([+−-]?\d+(?:\.\d+)?)\s*(%|％|个百分点|percentage points?|pp\b)"


def growth_bridge_findings(text: str, *, section: str | None = None) -> list[dict[str, Any]]:
    """Check only explicit, same-sentence growth contributions in percentage points.

    FX/acquisition *growth rates* cannot be added to organic growth. Multiple
    values for a label or distinct named periods are ambiguous, so the model
    verifier handles them. Deterministic failures carry the actual equation.
    """
    findings = []
    for sentence in _sentences(text):
        periods = set(re.findall(r"(?:FY\s*)?20\d{2}(?:\s*[Q季]\s*[1-4])?", sentence, re.I))
        if len(periods) > 1:
            continue
        values: dict[str, tuple[Decimal, str]] = {}
        for name, pattern in _GROWTH_LABELS.items():
            matches = list(re.finditer(pattern + _GROWTH_NUMBER, sentence, re.I))
            if len(matches) == 1:
                match = matches[0]
                values[name] = (Decimal(match.group(1).replace("−", "-")), match.group(2))
        combined = re.search(
            r"(?:(?:并购|收购|acquisitions?|M&A)[^，,。;；]{0,8}(?:汇率|外汇|FX)|"
            r"(?:汇率|外汇|FX)[^，,。;；]{0,8}(?:并购|收购|acquisitions?|M&A))"
            r"[^，,。;；]{0,10}(?:合计|共同|combined)[^，,。;；]{0,8}[+−-]?\d+(?:\.\d+)?\s*(?:个百分点|pp\b)",
            sentence, re.I)
        if combined and {"reported", "organic"}.issubset(values):
            findings.append({"code": "lint:usd_revenue_as_organic", "rule": "usd_revenue_as_organic",
                             "title": "并购与汇率贡献需分别披露", "excerpt": sentence[:160],
                             **({"section": section} if section is not None else {})})
            continue
        if set(values) != set(_GROWTH_LABELS):
            continue
        if values["reported"][1] not in {"%", "％"} or values["organic"][1] not in {"%", "％"}:
            continue
        if any(values[name][1] in {"%", "％"} for name in ("fx", "acquisition")):
            continue
        expected = sum((values[name][0] for name in ("organic", "fx", "acquisition")), Decimal(0))
        # Allow rounding of four one-decimal disclosures, without rounding
        # a material inconsistency into agreement.
        difference = values["reported"][0] - expected
        if abs(difference) <= Decimal("0.2"):
            continue
        finding = {
            "code": "lint:usd_revenue_as_organic", "rule": "usd_revenue_as_organic",
            "title": "报告增速与有机、汇率、并购贡献不闭合", "excerpt": sentence[:160],
            "reported_percent": str(values["reported"][0]),
            "organic_percent": str(values["organic"][0]),
            "fx_percentage_points": str(values["fx"][0]),
            "acquisition_percentage_points": str(values["acquisition"][0]),
            "difference_percentage_points": str(difference),
        }
        if section is not None:
            finding["section"] = section
        findings.append(finding)
    return findings


def lint_text(
    text: str, *, source_kinds: Iterable[str] = (), section: str | None = None,
    rules: Sequence[LintRule] = RULES,
) -> list[dict[str, Any]]:
    """Findings for one section's prose.

    ``source_kinds`` are the kinds of the sources the section cites
    (``consensus``, ``sell_side_note``, ``filing`` ...); a rule that is about
    a source is exempt when the right kind of source is cited.
    """

    kinds = {str(kind) for kind in source_kinds}
    whole = text or ""
    findings: list[dict[str, Any]] = []
    for rule in rules:
        if rule.exempt_source_kinds and kinds & set(rule.exempt_source_kinds):
            continue
        if rule.section_exempt and any(re.search(p, whole, re.IGNORECASE) for p in rule.section_exempt):
            continue
        for sentence in _sentences(whole):
            assertion = _assertion_text(rule.rule_id, sentence)
            if not all(re.search(p, assertion, re.IGNORECASE) for p in rule.triggers):
                continue
            if any(re.search(p, assertion, re.IGNORECASE) for p in rule.exempt):
                continue
            finding = {"code": f"lint:{rule.rule_id}", "rule": rule.rule_id,
                       "title": rule.title, "excerpt": sentence[:160]}
            if section is not None:
                finding["section"] = section
            findings.append(finding)
    if any(rule.rule_id == "usd_revenue_as_organic" for rule in rules):
        findings.extend(growth_bridge_findings(whole, section=section))
    return findings


def _assertion_text(rule_id: str, sentence: str) -> str:
    """Do not turn a stated research gap or a question into a false assertion.

    Remove only the qualifying clause: a missing-source statement must not
    exempt a different assertion elsewhere in the same sentence.
    """
    if rule_id == 'backlog_as_near_term_revenue':
        clauses=re.split(r'[，,]',sentence)
        causal=[]
        for index,clause in enumerate(clauses):
            if not re.search(_BACKLOG_REVENUE,clause,re.I):continue
            if re.search(_BACKLOG_TERM,clause,re.I):causal.append(clause)
            elif (index and re.search(_BACKLOG_TERM,clauses[index-1],re.I)
                  and re.match(r'\s*(?:所以|因此|从而|这(?:些)?|就|意味着|将|预计|带来|保障|锁定)',clause)):
                causal.append(clauses[index-1]+'，'+clause)
        sentence='，'.join(causal)
    if rule_id == 'consensus_without_authority':
        kept=[];conditional=False
        for clause in re.split(r'[，,]',sentence):
            if re.search(r'如果|假如|假设|倘若|一旦|(?:^|\s)若|\bif\b',clause,re.I):conditional=True
            if re.match(r'\s*(?:但|但是|不过|当前|目前|已经|现有)',clause):conditional=False
            observation=re.match(r'\s*(?:证伪|观察|调整|改变)[^，,。；]{0,40}(?:条件|信号|触发点)(?:是|为)\s*',clause)
            target=clause[observation.end():] if observation else clause
            movement=re.fullmatch(r'\s*(?:(?:如果|若|未来|届时|后续|可能|或将|预计|会|将)\s*)*'
                r'(?:(?:数据商|卖方|券商|分析师)\s*)?(?:FY\d{4}(?:Q[1-4])?\s*)?'
                r'(?:共识|一致预期|市场预期|consensus)\s*(?:FY\d{4}(?:Q[1-4])?\s*)?'
                r'(?:(?:EPS|每股收益|收入|营收|revenue)\s*)?(?:可能|会|将|或将|被)?'
                r'(?:上修|下修|提高|降低|改善|变化)[。！？!?]?\s*',target,re.I)
            if movement and (conditional or observation or re.search(r'可能|或将',clause)):continue
            kept.append(clause)
        sentence='，'.join(kept)
    if rule_id == "management_share_gain_as_fact" and re.search(
        r"^关于.+的说法[，,]?[^。；]{0,15}(?:尚不能|无法|未能|不能)核实", sentence
    ):
        return ""
    patterns = {
        "consensus_without_authority": (
            r"(?:缺少?|尚无|暂无|未取得|待补齐|需补齐|需要)[^，,。；]{0,50}(?:共识|一致预期|市场预期)",
            r"(?:共识|一致预期|市场预期)[^，,。；]{0,50}(?:缺来源|缺依据|补齐后|待补|缺失|不可得)",
            r"(?:并非|不代表|不能代表)[^，,。；]{0,10}(?:市场)?(?:共识|一致预期)",
            r"(?:无法|不能|尚不能)(?:量化|比较)[^，,。；]{0,20}(?:共识|一致预期|市场预期)",
        ),
        "backlog_as_near_term_revenue": (
            r"(?:能否|是否)[^，,。；]{0,30}(?:转化|转为|对应)[^，,。；]{0,10}(?:收入|营收)",
        ),
        "fixed_lag_uncalibrated": (
            r"(?:能否|是否)[^，,。；]{0,30}(?:领先|滞后|传导)",
        ),
        "management_share_gain_as_fact": (
            r"(?:尚不能|无法|未能|不能)(?:核实|证实|确认)",
        ),
        "usd_revenue_as_organic": (
            r"(?:不能|不可|不应)(?:替代|等同|视为)[^，,。；]{0,8}(?:有机|本币|内生)",
        ),
    }.get(rule_id, ())
    if not patterns:
        return sentence
    return "，".join(clause for clause in re.split(r"[，,]", sentence)
                    if not any(re.search(pattern, clause, re.I) for pattern in patterns))


def lint_sections(sections: Sequence[Mapping[str, Any]], *,
                  source_kinds_of: Any = None) -> list[dict[str, Any]]:
    """Lint every section; ``source_kinds_of(section) -> kinds`` when known."""

    findings: list[dict[str, Any]] = []
    for section in sections:
        kinds = source_kinds_of(section) if callable(source_kinds_of) else ()
        findings.extend(lint_text(str(section.get("body") or ""), source_kinds=kinds or (),
                                  section=str(section.get("title") or "")))
    return findings


__all__ = [
    "LINT_VERSION", "LintRule", "RULES", "RULE_IDS", "VERIFIER_LINT_INSTRUCTIONS",
    "lint_fingerprint", "lint_sections", "lint_text", "growth_bridge_findings", "source_kinds_for_refs",
]
