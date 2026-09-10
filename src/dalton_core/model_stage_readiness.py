"""Deterministic exit checks for the two model stages before Investment Memo."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .forecast_sensitivity import projection_readiness
from .model_forecast_driver import model_readiness


def industry_model_readiness(
    framework: Mapping[str, Any] | None,
    *,
    mission: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
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
    mission_bound = bool(
        mission
        and framework.get("industry_ref") == mission.get("industry_ref")
        and (framework.get("bindings") or {}).get("mission_version_ref") == mission.get("id")
    )
    # The current framework authority has drafted-at timestamps, but no field
    # that proves each numerical input's as-of date or that a high-frequency
    # source was placed on an update calendar. Candidate sources are plans,
    # not completed playbook outputs. Keep the gate entered until those
    # authority contracts exist.
    input_as_of_dates_bound = False
    update_calendar_bound = False
    comparison_explained = bool(comparison.get("comparability_notes"))
    checks.extend([
        {"criterion": "active_mission_binding", "passed": mission_bound},
        {"criterion": "inputs_source_bound", "passed": all_inputs_sourced},
        {"criterion": "input_as_of_dates_bound", "passed": input_as_of_dates_bound},
        {"criterion": "cross_company_comparison_computed", "passed": comparison_ready},
        {"criterion": "peer_gap_convergence_explained", "passed": comparison_explained},
        {"criterion": "high_frequency_data_identified", "passed": high_frequency_identified},
        {"criterion": "high_frequency_update_calendar_bound", "passed": update_calendar_bound},
    ])
    return {
        "passed": all(row["passed"] for row in checks), "checks": checks,
        "reasons": [row["criterion"] for row in checks if not row["passed"]],
        "evidence_refs": [str(framework["id"])],
    }


def company_model_readiness(
    model: Mapping[str, Any] | None,
    sensitivity: Mapping[str, Any] | None,
    *,
    mission: Mapping[str, Any] | None = None,
    company_ref: str | None = None,
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
    expected_company = company_ref or model.get("company_ref")
    authority_binding = bool(
        mission
        and model.get("company_ref") == expected_company
        and model.get("mission_version_ref") == mission.get("id")
        and sensitivity
        and sensitivity.get("company_ref") == expected_company
        and sensitivity.get("mission_version_ref") == mission.get("id")
        and sensitivity.get("model_version_ref") == model.get("id")
        and sensitivity.get("model_version_hash") == model.get("content_hash")
    )
    driver_count = ready["drivers"]
    driver_count_ready = 3 <= driver_count <= 5
    consensus_quantified = (
        bool(sensitivity)
        and projection.get("selection_status") == "selected"
        and projection.get("drivers_selected", 0) > 0
        and projection.get("what_if_cells", 0) > 0
        and projection.get("what_if_cells_unavailable", 0) == 0
        and projection.get("bridge_status") == "available"
        and projection.get("bridge_metrics", 0) > 0
    )
    # Neither authority currently records a completed two-year-filings
    # reconciliation result nor peer-relative sensitivity bands. Publication
    # validates arithmetic, but it is not an attestation of these playbook
    # readings and outputs.
    historical_reconciliation_proved = False
    peer_sensitivity_proved = False
    checks = [
        {"criterion": "active_mission_model_sensitivity_binding", "passed": authority_binding},
        {"criterion": "three_to_five_key_drivers", "passed": driver_count_ready},
        {"criterion": "historical_model_checks_pass", "passed": historical},
        {"criterion": "two_year_filings_reconciled_zero_error",
         "passed": historical_reconciliation_proved},
        {"criterion": "forecast_assumptions_explicit", "passed": assumptions_explicit},
        {"criterion": "peer_relative_sensitivity_quantified", "passed": peer_sensitivity_proved},
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
