"""Deterministic exit checks for the two model stages before Investment Memo."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .forecast_sensitivity import projection_readiness
from .model_forecast_driver import model_readiness


def industry_model_readiness(framework: Mapping[str, Any] | None) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    if not framework:
        return {"passed": False, "checks": [], "reasons": ["no_industry_framework"],
                "evidence_refs": []}
    blocks = list(framework.get("sections") or []) + [
        framework.get("industry_characteristics") or {},
        framework.get("long_term_drivers") or {},
        framework.get("short_term_drivers") or {},
    ]
    all_inputs_sourced = bool(framework.get("evidence_refs")) and all(
        block.get("status") == "drafted" and block.get("sources") for block in blocks)
    comparison = framework.get("cross_company_comparison") or {}
    comparison_ready = (
        comparison.get("status") == "computed"
        and bool(comparison.get("companies")) and bool(comparison.get("cells"))
    )
    gaps = list(framework.get("gaps") or [])
    high_frequency_identified = any(
        gap.get("gap_ref") == "gap:high-frequency-demand"
        and gap.get("status") in {"open", "partially_covered", "covered"}
        and gap.get("candidate_sources")
        for gap in gaps
    )
    checks.extend([
        {"criterion": "inputs_source_bound", "passed": all_inputs_sourced},
        {"criterion": "cross_company_comparison_computed", "passed": comparison_ready},
        {"criterion": "high_frequency_data_identified", "passed": high_frequency_identified},
    ])
    return {
        "passed": all(row["passed"] for row in checks), "checks": checks,
        "reasons": [row["criterion"] for row in checks if not row["passed"]],
        "evidence_refs": [str(framework["id"])],
    }


def company_model_readiness(
    model: Mapping[str, Any] | None,
    sensitivity: Mapping[str, Any] | None,
) -> dict[str, Any]:
    if not model:
        return {"passed": False, "checks": [], "reasons": ["no_forecast_model"],
                "evidence_refs": []}
    ready = model_readiness(model)
    historical = (
        ready["history_quarters"] > 0 and not ready["drivers_without_history"]
        and not ready["results_unavailable"]
    )
    assumptions = list(model.get("assumptions") or [])
    assumptions_explicit = bool(assumptions) and all(
        item.get("because") and item.get("refs") for item in assumptions)
    projection = projection_readiness(sensitivity or {}) if sensitivity else {}
    consensus_quantified = (
        bool(sensitivity)
        and projection.get("selection_status") == "selected"
        and projection.get("drivers_selected", 0) > 0
        and projection.get("what_if_cells", 0) > 0
        and projection.get("what_if_cells_unavailable", 0) == 0
        and projection.get("bridge_status") == "available"
        and projection.get("bridge_metrics", 0) > 0
    )
    checks = [
        {"criterion": "historical_model_checks_pass", "passed": historical},
        {"criterion": "forecast_assumptions_explicit", "passed": assumptions_explicit},
        {"criterion": "consensus_divergence_quantified", "passed": consensus_quantified},
    ]
    refs = [str(model["id"])]
    if sensitivity:
        refs.append(str(sensitivity["id"]))
    return {
        "passed": all(row["passed"] for row in checks), "checks": checks,
        "reasons": [row["criterion"] for row in checks if not row["passed"]],
        "evidence_refs": refs,
    }


__all__ = ["company_model_readiness", "industry_model_readiness"]
