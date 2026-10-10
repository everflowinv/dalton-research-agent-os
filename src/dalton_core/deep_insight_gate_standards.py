"""Batch D / ap: answer standards for the Deep Insight Gate, deterministic.

The gate keeps its twelve questions.  What changes is what counts as an
answer for the three questions the 2026-10-09 review found answered with
prose that could not carry the judgement:

* **q7** (过去五至十年的股价主要由什么驱动) -- an attribution series covering at
  least five distinct years, naming what drove the price (盈利 / 估值 / …);
* **q9** (哪些高频指标能证伪我们的判断) -- an indicator observed more often
  than quarterly, with a named source and a threshold;
* **q10** (thesis 被验证后的盈利、估值和催化剂) -- the full chain 营业利润 →
  税前利润 → 净利润 → EPS → 倍数 → 回报, citing a forecast cell of a model
  that passed its economic invariants.

An answer that misses its standard is not published as an answer: it becomes
``unknown`` with reason ``standard_not_met`` and the MaterialRequest opened to
close it.  Separately, **confidence calibration**: ``high`` needs a
decomposition (at least two kinds of source and a figure or forecast cell, or
an explicit 拆分/分解); otherwise it is lowered to ``medium`` with a gap note.

Everything here is pure except :func:`open_request`, which goes through the
batch-C MaterialRequest ledger and degrades to "no ref, reason recorded" when
that module is not installed.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from typing import Any

STANDARDS_VERSION = "deep-insight-gate-answer-standards:1"
STANDARD_REASON = "standard_not_met"
MIN_ATTRIBUTION_YEARS = 5

_YEAR_RE = re.compile(r"(?<!\d)((?:19|20)\d{2})(?!\d)|FY\s?(\d{2})(?!\d)", re.IGNORECASE)
_ATTRIBUTION_RE = re.compile(r"盈利|利润|EPS|估值|倍数|P/?E|市盈率|产品价格|价格|成本|政策|资本周期|"
                             r"earnings|valuation|multiple", re.IGNORECASE)
_FREQUENCY_RE = re.compile(r"月度|每月|按月|周度|每周|按周|日度|每日|每天|日频|周频|月频|旬|"
                           r"monthly|weekly|daily", re.IGNORECASE)
_SOURCE_RE = re.compile(r"来源|数据来自|发布|统计局|协会|官网|数据库|海关|调查|Bloomberg|Wind|"
                        r"source|published by", re.IGNORECASE)
_THRESHOLD_RE = re.compile(r"(?:阈值|超过|低于|高于|跌破|突破|不低于|不高于|连续|[<>≥≤])[^。；]{0,12}\d|"
                           r"\d[^。；]{0,6}(?:以上|以下)")
CHAIN_STEPS: tuple[tuple[str, str], ...] = (
    ("营业利润", r"营业利润|经营利润|operating\s+(?:income|profit)|\bEBIT\b"),
    ("税前利润", r"税前|pre-?tax"),
    ("净利润", r"净利润|net\s+income"),
    ("EPS", r"EPS|每股收益"),
    ("倍数", r"倍数|P/?E|市盈率|EV/EBITDA|multiple"),
    ("回报", r"回报|收益率|IRR|return|涨幅空间|空间"),
)
_DECOMPOSITION_RE = re.compile(r"拆分|分解|拆解|贡献|decompos", re.IGNORECASE)
FIGURE_KINDS = frozenset({"figure", "forecast_cell", "valuation_metric"})

STANDARD_TEXT: Mapping[str, str] = {
    "q7": f"至少 {MIN_ATTRIBUTION_YEARS} 个不同年份的股价归因序列（盈利/估值/价格/成本/政策等）",
    "q9": "高于季度频率的指标（月/周/日），写明数据来源与阈值",
    "q10": "完整链条 营业利润→税前利润→净利润→EPS→倍数→回报，并引用通过不变量检查的模型预测格",
}


def _text(answer: Mapping[str, Any]) -> str:
    return "\n".join(str(row.get("text") or "") for row in answer.get("sentences") or ())


def _source_text(answer: Mapping[str, Any]) -> str:
    return "\n".join(f"{row.get('period') or ''} {row.get('text') or ''}"
                     for row in answer.get("sources") or ())


def attribution_years(answer: Mapping[str, Any]) -> set[str]:
    years: set[str] = set()
    for match in _YEAR_RE.finditer(_text(answer) + "\n" + _source_text(answer)):
        years.add(match.group(1) or f"20{match.group(2)}")
    return years


def standard_findings(question_ref: str, answer: Mapping[str, Any], *,
                      model_invariants_passed: bool | None = None) -> list[str]:
    """Why an answered question misses its standard ([] when it meets it)."""

    if answer.get("status") != "answered":
        return []
    text = _text(answer)
    if question_ref == "q7":
        problems = []
        years = attribution_years(answer)
        if len(years) < MIN_ATTRIBUTION_YEARS:
            problems.append(f"归因序列只覆盖 {len(years)} 个年份，需要至少 {MIN_ATTRIBUTION_YEARS} 个")
        if not _ATTRIBUTION_RE.search(text):
            problems.append("没有写出股价由什么驱动（盈利/估值/价格/成本/政策）")
        return problems
    if question_ref == "q9":
        problems = []
        if not _FREQUENCY_RE.search(text):
            problems.append("没有高于季度频率的指标（月/周/日）")
        if not _SOURCE_RE.search(text):
            problems.append("没有写明指标的数据来源")
        if not _THRESHOLD_RE.search(text):
            problems.append("没有写明证伪阈值")
        return problems
    if question_ref == "q10":
        problems = [f"链条缺「{name}」" for name, pattern in CHAIN_STEPS
                    if not re.search(pattern, text, re.IGNORECASE)]
        if not any(row.get("kind") == "forecast_cell" for row in answer.get("sources") or ()):
            problems.append("没有引用模型预测格")
        if model_invariants_passed is not True:
            problems.append("模型未通过不变量检查" if model_invariants_passed is False
                            else "模型不变量检查结果未知")
        return problems
    return []


def calibrate_confidence(answer: Mapping[str, Any]) -> tuple[dict[str, Any], str | None]:
    """``high`` without a decomposition is lowered to ``medium``; returns the note."""

    out = dict(answer)
    if answer.get("status") != "answered" or answer.get("confidence") != "high":
        return out, None
    sources = list(answer.get("sources") or ())
    kinds = {str(row.get("kind")) for row in sources}
    has_figure = bool(kinds & FIGURE_KINDS)
    decomposed = bool(_DECOMPOSITION_RE.search(_text(answer)))
    if len(kinds) >= 2 and (has_figure or decomposed):
        return out, None
    missing = []
    if len(kinds) < 2:
        missing.append("至少两类来源")
    if not (has_figure or decomposed):
        missing.append("数字或预测格支撑的分解")
    note = "置信度由 high 降为 medium：缺少" + "与".join(missing)
    out["confidence"] = "medium"
    gaps = [str(gap) for gap in out.get("gaps") or ()]
    from .deep_insight_gate import MAX_GAPS

    out["gaps"] = (gaps + [note])[-MAX_GAPS:] if note not in gaps else gaps
    return out, note


def open_request(state_dir: Any, *, company_ref: str, question_ref: str, question: str,
                 problems: list[str]) -> tuple[str | None, str | None]:
    """One MaterialRequest for an unmet standard: ``(ref, reason_if_none)``."""

    from .product_contract import MaterialPackUnavailable, open_material_request

    if state_dir is None:
        return None, "没有研究状态目录，未登记 MaterialRequest"
    try:
        # Batch C's shape: requester {lane, section}, need {kind: answer,
        # question}; source priority, budget and deadline are its defaults.
        ref = open_material_request(
            state_dir, requester={"lane": "deep_insight_gate", "section": question_ref},
            subject=company_ref,
            need={"kind": "answer",
                  "question": f"{question_ref} {STANDARD_TEXT[question_ref]}；当前缺：{'；'.join(problems)}"[:2000]},
            acceptance={"standard": STANDARD_TEXT[question_ref]})
    except MaterialPackUnavailable as exc:
        return None, f"MaterialRequest 未登记：{exc}"[:300]
    except Exception as exc:  # noqa: BLE001 - the request ledger is not the gate
        return None, f"MaterialRequest 登记失败：{type(exc).__name__}: {exc}"[:300]
    return ref, None


def apply_answer_standards(
    answers: Mapping[str, Mapping[str, Any]], *, company_ref: str,
    model_invariants_passed: bool | None, state_dir: Any = None,
    request_opener: Callable[..., tuple[str | None, str | None]] | None = None,
) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    """Answers with the standards applied, and a report of what changed.

    An answered q7/q9/q10 that misses its standard becomes ``unknown`` with
    reason ``standard_not_met`` and a MaterialRequest ref when one could be
    opened.  Every other answered question keeps its text; ``high`` is
    calibrated.
    """

    opener = request_opener or open_request
    out: dict[str, dict[str, Any]] = {}
    report: list[dict[str, Any]] = []
    for ref, answer in answers.items():
        current = dict(answer)
        problems = standard_findings(ref, current, model_invariants_passed=model_invariants_passed)
        if problems:
            request_ref, why_none = opener(state_dir, company_ref=company_ref, question_ref=ref,
                                           question=str(current.get("question") or ""),
                                           problems=problems)
            missing = (f"答案未达到本题标准（{STANDARD_TEXT[ref]}）：{'；'.join(problems)}")[:400]
            next_step = (f"按 MaterialRequest {request_ref} 取得材料后重答" if request_ref
                         else f"取得满足标准的材料后重答；{why_none}")[:400]
            unknown: dict[str, Any] = {"reason": STANDARD_REASON, "missing": missing,
                                       "evidence_that_would_answer": next_step}
            if request_ref:
                unknown["material_request_ref"] = request_ref
            out[ref] = {**current, "status": "unknown", "confidence": None, "unknown": unknown,
                        "sentences": [], "sources": [], "gaps": list(current.get("gaps") or [])}
            report.append({"question_ref": ref, "action": "unanswered", "problems": problems,
                           "material_request_ref": request_ref, "request_note": why_none})
            continue
        calibrated, note = calibrate_confidence(current)
        if note:
            report.append({"question_ref": ref, "action": "confidence_lowered", "note": note})
        out[ref] = calibrated
    return out, report


def model_invariants_passed(connection: Any, company_ref: str) -> bool | None:
    """Did the company's newest forecast model pass the economic invariants?

    ``None`` when there is no model or the check cannot run here.
    """

    import json

    try:
        row = connection.execute(
            "SELECT record_json FROM forecast_model_versions WHERE company_ref=? "
            "ORDER BY created_at DESC LIMIT 1", (company_ref,)).fetchone()
    except Exception:  # noqa: BLE001 - no model table on this Core
        return None
    if row is None:
        return None
    try:
        from .economic_invariants import FORECAST_MODEL, evaluate, forecast_subject

        record = json.loads(row["record_json"] if not isinstance(row, tuple) else row[0])
        report = evaluate(output_kind=FORECAST_MODEL, output_ref=str(record.get("id")),
                          company_ref=company_ref, subject=forecast_subject(record))
    except Exception:  # noqa: BLE001 - an unreadable model is not a passed model
        return None
    return report.status == "available"


__all__ = [
    "CHAIN_STEPS", "MIN_ATTRIBUTION_YEARS", "STANDARDS_VERSION", "STANDARD_REASON",
    "STANDARD_TEXT", "apply_answer_standards", "attribution_years", "calibrate_confidence",
    "model_invariants_passed", "open_request", "standard_findings",
]
