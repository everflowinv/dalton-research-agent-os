"""Advance the deterministic industry/company model stages without model calls."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from .lane_registry import LaneSpec, register_lane
from .model_stage_readiness import company_model_readiness, industry_model_readiness

ACTOR_REF = "automation:coverage-mission"


def _latest_json(connection: Any, table: str, where: str, value: str,
                 order: str) -> dict[str, Any] | None:
    try:
        row = connection.execute(
            f"SELECT record_json FROM {table} WHERE {where}=? ORDER BY {order} DESC LIMIT 1",
            (value,),
        ).fetchone()
    except Exception:  # absent pre-existing authority is an honest waiting state
        return None
    return None if row is None else json.loads(row["record_json"])


def _evaluate(connection: Any, mission: Mapping[str, Any], company_ref: str,
              stage_ref: str) -> dict[str, Any]:
    if stage_ref == "industry_model":
        from .industry_framework import validate_framework_version

        framework = _latest_json(connection, "industry_framework_versions",
                                 "industry_ref", str(mission["industry_ref"]),
                                 "version_number")
        if framework is not None:
            framework = validate_framework_version(framework)
        return industry_model_readiness(framework)
    from .forecast_sensitivity import validate_projection
    from .model_forecast_driver import validate_forecast_model

    model = _latest_json(connection, "forecast_model_versions", "company_ref",
                         company_ref, "version_number")
    sensitivity = _latest_json(connection, "sensitivity_projections", "company_ref",
                               company_ref, "version_number")
    if model is not None:
        model = validate_forecast_model(model)
    if sensitivity is not None:
        sensitivity = validate_projection(sensitivity)
    return company_model_readiness(model, sensitivity)


def advance_once(missions: Any, connection: Any,
                 mission: Mapping[str, Any]) -> dict[str, Any]:
    """Advance at most one company/stage; waiting inputs remain recoverable."""
    if "stage_record" not in mission["autonomy"]["may_write"]:
        return {"status": "not_permitted", "reason": "mission lacks stage_record"}
    actor = mission["autonomy"]["automation_principal"]
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
            return {"status": "waiting", "company_ref": company_ref,
                    "stage_ref": stage_ref, "readiness": verdict}
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
