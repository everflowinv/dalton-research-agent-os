"""Advance the deterministic industry/company model stages without model calls."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .lane_registry import LaneSpec, register_lane
from .model_stage_readiness import company_model_readiness, industry_model_readiness

ACTOR_REF = "automation:coverage-mission"


def _reader(connection: Any, authority_type: type[Any]) -> Any:
    authority = authority_type.__new__(authority_type)
    authority.connection = connection
    return authority


def _latest(connection: Any, authority_type: type[Any], subject_ref: str) -> dict[str, Any] | None:
    """Read through an authority's checked public reader, without running DDL."""
    return _reader(connection, authority_type).latest(subject_ref)


def _evaluate(connection: Any, mission: Mapping[str, Any], company_ref: str,
              stage_ref: str) -> dict[str, Any]:
    try:
        from .industry_framework import IndustryFrameworkAuthority

        framework = _latest(connection, IndustryFrameworkAuthority,
                            str(mission["industry_ref"]))
        if stage_ref == "industry_model":
            from .tracking_cadence import load_policy

            cadence_keys = frozenset(load_policy()["cadences"])
            return industry_model_readiness(
                framework, mission=mission, cadence_source_keys=cadence_keys)
        from .forecast_sensitivity import SensitivityProjectionAuthority
        from .model_forecast_driver import ForecastModelAuthority

        model = _latest(connection, ForecastModelAuthority, company_ref)
        sensitivity = _latest(connection, SensitivityProjectionAuthority, company_ref)
        return company_model_readiness(model, sensitivity, mission=mission,
                                       company_ref=company_ref,
                                       peer_comparison=(framework or {}).get(
                                           "cross_company_comparison"))
    except Exception as exc:
        reason = f"authority_read_failed:{type(exc).__name__}:{exc}"
        return {"passed": False, "checks": [], "reasons": [reason],
                "evidence_refs": []}


def advance_once(missions: Any, connection: Any,
                 mission: Mapping[str, Any]) -> dict[str, Any]:
    """Advance at most one company/stage; waiting inputs remain recoverable."""
    if "stage_record" not in mission["autonomy"]["may_write"]:
        return {"status": "not_permitted", "reason": "mission lacks stage_record"}
    actor = mission["autonomy"]["automation_principal"]
    waiting: list[dict[str, Any]] = []
    for member in mission["universe"]:
        company_ref = member["company_ref"]
        state = missions.current_stage_state(mission["mission_ref"], company_ref)
        stage_ref = state["next_stage"]
        if stage_ref not in {"industry_model", "company_model"}:
            continue
        verdict = _evaluate(connection, mission, company_ref, stage_ref)
        if state["current_stage"] != stage_ref:
            missions.record_stage(
                mission_version_ref=mission["id"],
                mission_version_hash=mission["content_hash"],
                company_ref=company_ref, stage_ref=stage_ref, status="entered",
                evidence_refs=verdict["evidence_refs"],
                rationale=f"Deterministic {stage_ref} exit checks began.",
                actor_ref=actor,
                idempotency_key=f"model-stage:{mission['mission_ref']}:{company_ref}:{stage_ref}:entered",
            )
        if not verdict["passed"]:
            waiting.append({"company_ref": company_ref, "stage_ref": stage_ref,
                            "readiness": verdict})
            continue
        record = missions.record_stage(
            mission_version_ref=mission["id"], mission_version_hash=mission["content_hash"],
            company_ref=company_ref, stage_ref=stage_ref, status="gate_passed",
            evidence_refs=verdict["evidence_refs"],
            rationale=f"All deterministic {stage_ref} exit checks passed.", actor_ref=actor,
            idempotency_key=(f"model-stage:{mission['mission_ref']}:{company_ref}:"
                             f"{stage_ref}:passed:{':'.join(verdict['evidence_refs'])}"),
        )
        return {"status": "advanced", "company_ref": company_ref,
                "stage_ref": stage_ref, "stage_record_ref": record["id"],
                "readiness": verdict}
    if waiting:
        return {"status": "waiting", **waiting[0], "waiting": waiting}
    return {"status": "idle", "reason": "no deterministic model stage is due"}


def dispatch(server: Any, params: Mapping[str, Any]) -> dict[str, Any]:
    del params
    row = server.store.connection.execute(
        "SELECT mission_version_id FROM coverage_mission_pointer ORDER BY mission_ref LIMIT 1"
    ).fetchone()
    if row is None:
        return {"status": "idle", "reason": "no active mission"}
    mission = server.coverage_mission.mission(row["mission_version_id"])
    return advance_once(server.coverage_mission, server.store.connection, mission)


LANE = register_lane(LaneSpec(
    operation="dispatch_model_stage_bridge", order=142,
    driver_key="model_stage_bridge", handler=dispatch,
    note="Deterministically closes industry_model and company_model after their existing authorities pass explicit checks.",
))

__all__ = ["LANE", "advance_once", "dispatch"]
