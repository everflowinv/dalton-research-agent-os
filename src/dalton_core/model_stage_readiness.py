"""Deterministic exit checks for the two model stages before Investment Memo.

ADR-0011 decides what "the industry_model / company_model stage was executed"
means, because two definitions had grown up side by side: the onboarding
blueprint said a stage is executed when its metric base (``STAGE_SPINE``) is
held, while this module closed the gate on authority checks and the spine for
both stages was empty.  There is now one definition: **a model stage is
executed when its gate passes, and the gate includes the stage's declared
metric base as one of its criteria** (``stage_metric_base_held``).  The spine
says which figures the stage needs; the gate counts them, from the same
authorities the rest of the gate reads; nothing else decides.

Every criterion is a read of an existing append-only authority -- the industry
framework, the company driver model and its filing proof, the sensitivity
projection, the valuation snapshot, the market price series and the catalyst
calendar -- and no criterion needs a model call, so the verdict is replayable
from the evidence refs the stage record cites.

What the gates deliberately do *not* require (ADR-0011, "rejected"):

- ``forecast_reconciliation`` rows.  They need a filed actual for a quarter the
  model forecast, which arrives once a quarter per company; the live Core had
  none on 2026-10-06 (ACN's FY26 Q4 10-K was not yet filed).  A gate that waits
  on the calendar is a gate that cannot be reasoned about; reconciliation is
  the active-coverage loop's job, not a model-stage exit.
- Peer-relative sensitivity bands.  No authority produces them; the industry
  gate's five-company comparison is the peer evidence the Playbook asks for.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from .forecast_sensitivity import (
    BAND_RULE_REF,
    SELECTION_RULE_HASH,
    SELECTION_RULE_REF,
    projection_readiness,
)
from .metric_base import missing_metrics
from .model_forecast_driver import model_readiness

# Short owner-facing words for each criterion: the lane's tick reason and the
# cockpit both say *which* check a company is still missing, in Chinese.
CRITERION_LABELS: Mapping[str, str] = {
    "no_industry_framework": "还没有行业框架",
    "active_mission_binding": "行业框架不属于当前研究任务",
    "inputs_source_bound": "行业框架有输入没有来源",
    "five_company_revenue_growth_margin_comparison": "同业收入/增速/毛利率对比不完整",
    "comparison_limits_explained": "同业对比缺口径说明",
    "unconnected_inputs_explicitly_deferred": "未接入的高频数据没有写明替代来源",
    "company_update_calendar_bound": "这家公司还没有更新日历（催化剂日历）",
    "stage_metric_base_held": "本阶段要求的数字还不够",
    "no_forecast_model": "还没有驱动模型",
    "active_mission_model_sensitivity_binding": "敏感性表与当前模型不对应",
    "current_sensitivity_rule_binding": "敏感性表用的是旧规则",
    "three_to_five_key_drivers": "关键驱动不是 3–5 个",
    "historical_model_checks_pass": "模型历史不足 8 个季度",
    "two_year_filings_reconciled_zero_error": "历史数与申报文件对不上",
    "forecast_assumptions_explicit": "预测假设没有逐条写明来源",
    "driver_peak_trough_mean_bands": "驱动缺历史峰/谷/均值区间",
    "consensus_divergence_quantified": "与一致预期的分歧没有量化",
    "model_covers_latest_filing": "模型没有包含最新一期申报",
    "valuation_snapshot_current": "估值快照没有按最新股价重算",
}


def criterion_label(criterion: str) -> str:
    text = str(criterion)
    if text.startswith("authority_read_failed"):
        return "读取依据失败：" + text.split(":", 1)[-1][:60]
    return CRITERION_LABELS.get(text, text)


# Comparison-cell metric names -> the metric base's refs.
COMPARISON_METRIC_REFS: Mapping[str, str] = {
    "revenue": "metric:revenue",
    "revenue_yoy_growth": "metric:revenue-yoy-growth",
    "gross_margin": "metric:gross-margin",
    "operating_margin": "metric:operating-margin",
}


def industry_model_held_metrics(framework: Mapping[str, Any] | None,
                                company_ref: str) -> list[dict[str, str]]:
    """The figures the framework's comparison holds for one company."""

    comparison = (framework or {}).get("cross_company_comparison") or {}
    held: list[dict[str, str]] = []
    for cell in comparison.get("cells") or ():
        if not isinstance(cell, Mapping) or cell.get("status") != "computed":
            continue
        if str(cell.get("company_ref")) != company_ref:
            continue
        ref = COMPARISON_METRIC_REFS.get(str(cell.get("metric")))
        period = cell.get("quarter") or cell.get("period_end")
        if ref and period:
            held.append({"metric_ref": ref, "period": str(period)})
    return held


def _period_end(value: Any) -> str | None:
    if isinstance(value, str) and value:
        return value
    if isinstance(value, Mapping):
        for key in ("end", "period_end", "date"):
            if isinstance(value.get(key), str) and value.get(key):
                return str(value[key])
    return None


def company_model_held_metrics(model: Mapping[str, Any] | None) -> list[dict[str, str]]:
    """Filed revenue quarters the driver model carries.

    A model's history window is the filed quarters it was solved against; the
    revenue line counts for those quarters only when the model actually
    computes revenue (its ``revenue`` role result).
    """

    if not model:
        return []
    computes_revenue = any(
        isinstance(row, Mapping) and row.get("status") == "computed"
        and (row.get("role") == "revenue" or row.get("ref") == "result:revenue")
        for row in model.get("results") or ())
    if not computes_revenue:
        return []
    held = []
    for index, period in enumerate(model.get("history_periods") or ()):
        end = _period_end(period) or f"history:{index}"
        held.append({"metric_ref": "metric:revenue", "period": end})
    return held


def _metric_base_check(stage: str, company_ref: str,
                       held: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    missing = missing_metrics(list(held), stage=stage, company_ref=company_ref)
    return {"criterion": "stage_metric_base_held", "passed": not missing,
            "missing": [{"metric_ref": item["metric_ref"], "have": item["have"],
                         "periods": item["periods"]} for item in missing]}


def _verdict(checks: list[dict[str, Any]], refs: list[str]) -> dict[str, Any]:
    reasons = [row["criterion"] for row in checks if not row["passed"]]
    return {
        "passed": bool(checks) and not reasons, "checks": checks,
        "reasons": reasons,
        "reason_labels": [criterion_label(reason) for reason in reasons],
        "evidence_refs": refs,
    }


def _mission_refs(mission: Mapping[str, Any] | None,
                  mission_version_refs: Iterable[str] | None) -> frozenset[str]:
    refs = set(mission_version_refs or ())
    if mission and mission.get("id"):
        refs.add(str(mission["id"]))
    return frozenset(refs)


def industry_model_readiness(
    framework: Mapping[str, Any] | None,
    *,
    mission: Mapping[str, Any] | None = None,
    company_ref: str | None = None,
    mission_version_refs: Iterable[str] | None = None,
    update_calendar: Mapping[str, Any] | None = None,
    cadence_source_keys: frozenset[str] = frozenset(),
) -> dict[str, Any]:
    """The industry_model exit gate (ADR-0011).

    The Playbook's three questions, as checks over the industry framework:
    every input carries a source (Q1), the company-vs-peers comparison is
    computed for the whole universe and its limits are explained (Q2), and the
    high-frequency inputs are identified with a named source and the company
    is on an update calendar (Q3).  ``mission_version_refs`` are every version
    of the active mission's ``mission_ref``: a version is provenance, not
    scope (ADR-0008 addendum), so a framework drafted before a budget signing
    still belongs to this mission.  ``cadence_source_keys`` is accepted for
    old callers and unused -- reading it made the lane fail on every install
    whose package had no repository ``deploy/`` directory beside it.
    """

    del cadence_source_keys
    if not framework:
        return {"passed": False, "checks": [], "reasons": ["no_industry_framework"],
                "reason_labels": [criterion_label("no_industry_framework")],
                "evidence_refs": []}
    blocks = list(framework.get("sections") or []) + [
        framework.get("industry_characteristics") or {},
        framework.get("long_term_drivers") or {},
        framework.get("short_term_drivers") or {},
    ]
    all_inputs_sourced = bool(framework.get("evidence_refs")) and all(
        block.get("status") == "drafted" and block.get("sources") for block in blocks)
    comparison = framework.get("cross_company_comparison") or {}
    expected_companies = {
        str(row["company_ref"]) for row in (mission or {}).get("universe") or []
    }
    comparison_companies = {
        str(row.get("company_ref")) for row in comparison.get("companies") or []
    }
    required_metrics = {"revenue", "revenue_yoy_growth", "gross_margin"}
    computed_pairs = {
        (str(row.get("company_ref")), str(row.get("metric")))
        for row in comparison.get("cells") or []
        if row.get("status") == "computed"
    }
    comparison_ready = (
        comparison.get("status") == "computed"
        and bool(expected_companies)
        and expected_companies <= comparison_companies
        and all((ref, metric) in computed_pairs
                for ref in expected_companies
                for metric in required_metrics)
    )
    gaps = list(framework.get("gaps") or [])
    explicit_deferred_gaps = all(
        gap.get("status") in {"open", "partially_covered", "covered"}
        and gap.get("candidate_sources")
        for gap in gaps
    )
    high_frequency_identified = any(
        gap.get("gap_ref") == "gap:high-frequency-demand"
        and gap.get("status") in {"open", "partially_covered", "covered"}
        and gap.get("candidate_sources")
        for gap in gaps
    )
    refs = _mission_refs(mission, mission_version_refs)
    mission_bound = bool(
        mission
        and framework.get("industry_ref") == mission.get("industry_ref")
        and (framework.get("bindings") or {}).get("mission_version_ref") in refs
    )
    comparison_explained = bool(comparison.get("comparability_notes"))
    checks: list[dict[str, Any]] = [
        {"criterion": "active_mission_binding", "passed": mission_bound},
        {"criterion": "inputs_source_bound", "passed": all_inputs_sourced},
        {"criterion": "five_company_revenue_growth_margin_comparison",
         "passed": comparison_ready},
        {"criterion": "comparison_limits_explained", "passed": comparison_explained},
        {"criterion": "unconnected_inputs_explicitly_deferred",
         "passed": explicit_deferred_gaps and high_frequency_identified},
    ]
    evidence = [str(framework["id"])]
    if company_ref is not None:
        calendar_bound = bool(
            update_calendar
            and str(update_calendar.get("company_ref")) == company_ref
            and update_calendar.get("id"))
        checks.append({"criterion": "company_update_calendar_bound",
                       "passed": calendar_bound})
        checks.append(_metric_base_check(
            "industry_model", company_ref,
            industry_model_held_metrics(framework, company_ref)))
        if calendar_bound:
            evidence.append(str(update_calendar["id"]))
    return _verdict(checks, evidence)


def _proof_reconciles(model: Mapping[str, Any],
                      filing_proof: Mapping[str, Any] | None) -> bool:
    if not (filing_proof
            and filing_proof.get("model_version_ref") == model.get("id")
            and filing_proof.get("model_content_hash") == model.get("content_hash")
            and (filing_proof.get("invariant_report") or {}).get("status") == "available"):
        return False
    # ``filing_proof_recheck`` adds the report recomputed on the exact,
    # hash-checked source rows under the current invariant contract.  When it
    # is present it decides: a stored "available" that no longer evaluates to
    # "available" is not proof of anything today.
    if "current_invariant_report" in filing_proof:
        return (filing_proof.get("current_invariant_report") or {}).get(
            "status") == "available"
    return True


def company_model_readiness(
    model: Mapping[str, Any] | None,
    sensitivity: Mapping[str, Any] | None,
    *,
    mission: Mapping[str, Any] | None = None,
    company_ref: str | None = None,
    peer_comparison: Mapping[str, Any] | None = None,
    filing_proof: Mapping[str, Any] | None = None,
    mission_version_refs: Iterable[str] | None = None,
    valuation: Mapping[str, Any] | None = None,
    current_price_ref: str | None = None,
    latest_filed_period: str | None = None,
) -> dict[str, Any]:
    """The company_model exit gate (ADR-0011).

    The Playbook's three questions: history reconciles to the filings with
    zero error (the model's filing proof, re-evaluated on its exact source
    rows), the hidden assumptions are written with sources, and the
    divergence from consensus is quantified (the sensitivity projection's
    consensus bridge).  Plus the two things the Investment Memo will read and
    so must be current when the stage closes: the sensitivity table is the one
    computed from *this* model, and the valuation snapshot was assembled from
    the latest price series.  ``peer_comparison`` is accepted for old callers;
    peer-relative bands are not a criterion (no authority produces them).
    """

    del peer_comparison
    if not model:
        return {"passed": False, "checks": [], "reasons": ["no_forecast_model"],
                "reason_labels": [criterion_label("no_forecast_model")],
                "evidence_refs": []}
    ready = model_readiness(model)
    historical = ready["history_quarters"] >= 8
    assumptions = list(model.get("assumptions") or [])
    assumptions_explicit = bool(assumptions) and all(
        item.get("because") and item.get("refs") for item in assumptions)
    projection = projection_readiness(sensitivity or {}) if sensitivity else {}
    expected_company = company_ref or model.get("company_ref")
    refs = _mission_refs(mission, mission_version_refs)
    inputs_match = (
        not model.get("inputs_hash") or not (sensitivity or {}).get("inputs_hash")
        or model.get("inputs_hash") == (sensitivity or {}).get("inputs_hash"))
    authority_binding = bool(
        mission
        and model.get("company_ref") == expected_company
        and model.get("mission_version_ref") in refs
        and sensitivity
        and sensitivity.get("company_ref") == expected_company
        and sensitivity.get("mission_version_ref") in refs
        and sensitivity.get("model_version_ref") == model.get("id")
        and sensitivity.get("model_version_hash") == model.get("content_hash")
        and inputs_match
    )
    current_sensitivity_rule_binding = bool(
        sensitivity
        and sensitivity.get("selection_rule_ref") == SELECTION_RULE_REF
        and sensitivity.get("selection_rule_hash") == SELECTION_RULE_HASH
        and sensitivity.get("band_rule_ref") == BAND_RULE_REF
    )
    driver_count = projection.get("drivers_selected", 0)
    driver_count_ready = 3 <= driver_count <= 5
    consensus_quantified = (
        bool(sensitivity)
        and projection.get("selection_status") in {"selected", "available"}
        and projection.get("drivers_selected", 0) > 0
        and projection.get("what_if_cells", 0) > 0
        and projection.get("bridge_status") == "available"
        and projection.get("bridge_metrics", 0) > 0
    )
    historical_bands_proved = (
        projection.get("drivers_with_bands") == driver_count
        and not projection.get("drivers_without_bands")
    )
    history_ends = [end for end in (_period_end(item)
                                     for item in model.get("history_periods") or ())
                    if end]
    covers_latest = bool(
        latest_filed_period and history_ends
        and max(history_ends) >= str(latest_filed_period))
    metrics = (valuation or {}).get("metrics") or []
    valuation_current = bool(
        valuation
        and str(valuation.get("company_ref")) == str(expected_company)
        and any(isinstance(row, Mapping) and row.get("status") == "available"
                for row in metrics)
        and current_price_ref
        and ((valuation.get("price") or {}).get("version_ref")) == current_price_ref
    )
    checks = [
        {"criterion": "active_mission_model_sensitivity_binding", "passed": authority_binding},
        {"criterion": "current_sensitivity_rule_binding",
         "passed": current_sensitivity_rule_binding},
        {"criterion": "three_to_five_key_drivers", "passed": driver_count_ready},
        {"criterion": "historical_model_checks_pass", "passed": historical},
        {"criterion": "two_year_filings_reconciled_zero_error",
         "passed": _proof_reconciles(model, filing_proof)},
        {"criterion": "forecast_assumptions_explicit", "passed": assumptions_explicit},
        {"criterion": "driver_peak_trough_mean_bands", "passed": historical_bands_proved},
        {"criterion": "consensus_divergence_quantified", "passed": consensus_quantified},
        {"criterion": "model_covers_latest_filing", "passed": covers_latest},
        {"criterion": "valuation_snapshot_current", "passed": valuation_current},
        _metric_base_check("company_model", str(expected_company),
                           company_model_held_metrics(model)),
    ]
    evidence = [str(model["id"])]
    if sensitivity:
        evidence.append(str(sensitivity["id"]))
    if valuation and valuation.get("id"):
        evidence.append(str(valuation["id"]))
    return _verdict(checks, evidence)


__all__ = ["COMPARISON_METRIC_REFS", "CRITERION_LABELS", "company_model_held_metrics",
           "company_model_readiness", "criterion_label", "industry_model_held_metrics",
           "industry_model_readiness"]
