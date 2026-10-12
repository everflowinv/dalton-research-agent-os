"""Read-only proof of a current forecast gap, never new acquisition authority.

Old requests without current evidence keep their ordinary service turn. A
requester label or operations summary alone cannot reserve a priority turn.
"""
from __future__ import annotations

import json
import sqlite3
import time
from contextlib import ExitStack, closing
from pathlib import Path
from typing import Any, Mapping, Sequence

from .readonly_sqlite import connect_read_only
from .store import canonical_json, content_hash

CONTRACT_REF = "material-current-product-gap-priority:0.2"


def _checked_record(row: Any) -> dict:
    wire = json.loads(row["record_json"])
    if (canonical_json(wire) != row["record_json"]
            or wire.get("content_hash") != row["content_hash"]
            or content_hash({k: v for k, v in wire.items() if k != "content_hash"}) != row["content_hash"]):
        raise ValueError("current material priority record hash differs")
    return wire


def _proof(request: Mapping[str, Any], mission: Mapping[str, Any],
           spec: Mapping[str, Any], forecast: Mapping[str, Any] | None) -> dict | None:
    from .forecast_material_gaps import gap_plans, RULE_REF
    from .material_requests import request_digest, normalise_need

    company = request["subject"]
    if (spec.get("company_ref") != company or spec.get("mission_version_ref") != mission["id"]
            or request.get("digest") != request_digest(company, request["need"], request.get("period"))
            or not any(r.get("lane") == "company_model_forecast" and r.get("product_ref") == spec["spec_id"]
                       for r in request.get("requesters") or ())):
        return None
    intrinsic = gap_plans(spec, {"periods": [request.get("period")]})
    plans = list(intrinsic)
    if forecast is not None:
        if (forecast.get("company_ref") != company or forecast.get("spec_ref") != spec["spec_id"]
                or forecast.get("spec_hash") != spec["content_hash"]
                or forecast.get("mission_version_ref") != mission["id"]):
            forecast = None
        else:
            formal_plans = gap_plans(spec, {"periods": [forecast.get("forecast_policy", {}).get("latest_actual_end")]}, record=forecast)
            if (forecast.get("execution_contract") or {}).get("drivers", {}).get("status") == "executed":
                # A proven current successful execution overrides the old
                # missing-identity heuristic; it is not a blocked deliverable.
                intrinsic = []
                plans = []
                formal_plans = [p for p in formal_plans if p["key"] != "economic_driver_history"]
            plans += formal_plans
    for plan in plans:
        if (normalise_need(plan["need"]) != request["need"] or plan["period"] != request.get("period")
                or request.get("acceptance") != plan["acceptance"]
                or request.get("acceptance", {}).get("model_gap_rule_ref") != RULE_REF
                or not any(r.get("lane") == "company_model_forecast" and r.get("product_ref") == spec["spec_id"]
                           and r.get("section") == plan["key"] for r in request.get("requesters") or ())):
            continue
        basis = "current_spec_intrinsic_gap" if plan in intrinsic else "current_formal_forecast_gap"
        body = {"contract_ref": CONTRACT_REF, "request_ref": request["id"], "request_hash": request["content_hash"],
                "mission_version_ref": mission["id"], "company_ref": company, "mission_hash": mission["content_hash"], "spec_ref": spec["spec_id"],
                "spec_hash": spec["content_hash"], "gap_key": plan["key"], "basis": basis,
                "forecast_ref": None if basis != "current_formal_forecast_gap" else forecast["id"],
                "forecast_hash": None if basis != "current_formal_forecast_gap" else forecast["content_hash"]}
        return {**body, "content_hash": content_hash(body)}
    return None


def priority_gaps(state_dir: str | Path, pending: Sequence[Mapping[str, Any]], *,
                  deadline: float) -> tuple[dict[str, dict], dict]:
    """Exact current authorities only; no constructors, summaries or broad scans."""
    from .coverage_mission import CoverageMissionAuthority, CoverageMissionError
    from .material_requests import _view
    from .forecast_material_gaps import RULE_REF
    from .model_forecast_driver import ForecastModelAuthority, ForecastModelError

    from .sec_filing_units import RULE_REF as UNIT_RULE_REF
    from .material_model_unit_priority import unit_input_proof
    candidates = [v for v in pending if (
        (v.get("acceptance") or {}).get("model_gap_rule_ref") == RULE_REF
        and (v.get("need") or {}).get("kind") == "data"
        and any(r.get("lane") == "company_model_forecast" for r in v.get("requesters") or ())) or (
        (v.get("acceptance") or {}).get("unit_proof_rule_ref") == UNIT_RULE_REF
        and (v.get("need") or {}).get("kind") == "document"
        and any(r.get("lane") == "statement_context" and r.get("section") == "exact_xml_units"
                for r in v.get("requesters") or ()))]
    info = {"contract_ref": CONTRACT_REF, "candidates": len(candidates), "qualified": 0,
            "status": "checked", "reason_code": None}
    if not candidates:
        return {}, info
    state = Path(state_dir)
    qualified: dict[str, dict] = {}
    try:
        with ExitStack() as stack:
            core = stack.enter_context(closing(connect_read_only(state / "core.sqlite")))
            ledger = stack.enter_context(closing(connect_read_only(state / "material-requests.sqlite")))
            for connection in (core, ledger):
                connection.row_factory = sqlite3.Row
                connection.execute("PRAGMA busy_timeout=250")
                connection.execute("BEGIN")
                connection.set_progress_handler(lambda: time.monotonic() >= deadline, 2000)
            row = core.execute("SELECT v.record_json,v.content_hash,v.mission_version_id,v.mission_ref "
                "FROM coverage_mission_pointer p JOIN coverage_mission_versions v "
                "ON v.mission_version_id=p.mission_version_id AND v.mission_ref=p.mission_ref "
                "ORDER BY p.mission_ref LIMIT 1").fetchone()
            if row is None:
                return {}, {**info, "status": "unavailable", "reason_code": "no_current_mission"}
            mission = _checked_record(row)
            if mission["id"] != row["mission_version_id"] or mission["mission_ref"] != row["mission_ref"]:
                raise ValueError("priority mission binding differs")
            members = {v["company_ref"] for v in mission.get("universe") or ()}
            cached = {}
            unit_cache = {}
            failure_cache = {}
            failure_candidates = []
            for candidate in candidates:
                if time.monotonic() >= deadline:
                    return {}, {**info, "status": "unavailable", "reason_code": "priority_read_budget"}
                company = candidate["subject"]
                if company not in members:
                    continue
                if (candidate.get("acceptance") or {}).get("unit_proof_rule_ref") == UNIT_RULE_REF:
                    row = ledger.execute("SELECT * FROM material_requests WHERE request_id=?", (candidate["id"],)).fetchone()
                    if row is None:continue
                    raw = _checked_record(row)
                    if raw["id"] != row["request_id"] or raw["subject"] != row["subject_ref"]:
                        raise ValueError("priority unit request binding differs")
                    view = _view(ledger, row, with_events=True)
                    if view["status"] not in {"open", "partial"} or view["closed"]:continue
                    request = {**raw, "requesters": view["requesters"]}
                    proof = unit_input_proof(core,request,mission,contract_ref=CONTRACT_REF,cache=unit_cache)
                    if proof:qualified[request["id"]]=proof
                    continue
                if company not in cached:
                    spec_row = core.execute("SELECT * FROM coverage_mission_company_model_specs "
                        "WHERE company_ref=? ORDER BY created_at DESC,spec_id DESC LIMIT 1", (company,)).fetchone()
                    if spec_row is None:
                        cached[company] = (None, None)
                    else:
                        spec = CoverageMissionAuthority._model_spec_row(spec_row)
                        spec_body = {k: v for k, v in spec.items() if k not in {
                            "spec_id", "mission_version_ref", "model_profile_ref", "work_order_ref", "created_at", "content_hash"}}
                        if (spec.get("schema_version") not in {"0.3", "0.4"}
                                or content_hash(spec_body) != spec["content_hash"]):
                            raise ValueError("priority specification hash differs")
                        if spec.get("mission_version_ref") != mission["id"]:
                            cached[company] = (None, None)
                            continue
                        forecast = None
                        if core.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='forecast_model_versions'").fetchone():
                            latest = core.execute("SELECT version_id FROM forecast_model_versions WHERE company_ref=? "
                                "ORDER BY version_number DESC LIMIT 1", (company,)).fetchone()
                            if latest:
                                authority = object.__new__(ForecastModelAuthority)
                                authority.connection = core
                                forecast = authority.model(latest[0])
                        cached[company] = (spec, forecast)
                spec, forecast = cached[company]
                if spec is None:
                    continue
                row = ledger.execute("SELECT * FROM material_requests WHERE request_id=?", (candidate["id"],)).fetchone()
                if row is None:
                    continue
                raw = _checked_record(row)
                if raw["id"] != row["request_id"] or raw["subject"] != row["subject_ref"]:
                    raise ValueError("priority request binding differs")
                view = _view(ledger, row, with_events=True)
                if view["status"] not in {"open", "partial"} or view["closed"]:
                    continue
                request = {**raw, "requesters": view["requesters"]}
                proof = _proof(request, mission, spec, forecast)
                if proof is None and forecast is None:
                    failure_candidates.append((request, spec))
                if proof:
                    qualified[request["id"]] = proof
            # Establish all cheap current proofs before bounded failure replay.
            # Its timeout must not erase already qualified unit/intrinsic gaps.
            for request, spec in failure_candidates:
                if time.monotonic() >= deadline:
                    break
                from .material_failed_forecast_priority import failure_priority
                proof = failure_priority(state, request, mission, spec, contract_ref=CONTRACT_REF,
                    deadline=deadline, cache=failure_cache)
                if proof:
                    qualified[request["id"]] = proof
    except (OSError, sqlite3.Error, ValueError, KeyError, TypeError, CoverageMissionError, ForecastModelError):
        return {}, {**info, "status": "unavailable", "reason_code": "current_gap_proof_unavailable"}
    return qualified, {**info, "qualified": len(qualified)}


def still_current(state_dir: str | Path, proof: Mapping[str, Any], *, deadline: float) -> bool:
    """Recheck immutable current authority pointers before a priority claim."""
    if (proof.get("contract_ref") != CONTRACT_REF or proof.get("content_hash") != content_hash({
            k: v for k, v in proof.items() if k != "content_hash"})):
        return False
    try:
        with closing(connect_read_only(Path(state_dir) / "core.sqlite")) as core:
            core.row_factory = sqlite3.Row
            core.execute("PRAGMA busy_timeout=250")
            core.execute("BEGIN")
            core.set_progress_handler(lambda: time.monotonic() >= deadline, 2000)
            mission = core.execute("SELECT v.mission_version_id,v.content_hash FROM coverage_mission_pointer p "
                "JOIN coverage_mission_versions v ON v.mission_version_id=p.mission_version_id AND v.mission_ref=p.mission_ref "
                "ORDER BY p.mission_ref LIMIT 1").fetchone()
            if mission is None or tuple(mission) != (proof["mission_version_ref"], proof["mission_hash"]):
                return False
            from .material_failed_forecast_priority import BASIS as FAILED_BASIS, failure_still_current
            if proof["basis"] == FAILED_BASIS:
                return failure_still_current(state_dir, proof, deadline=deadline)
            from .material_model_unit_priority import BASIS, unit_input_still_current
            if proof["basis"] == BASIS:
                return (unit_input_still_current(core,proof) and time.monotonic()<deadline)
            spec = core.execute("SELECT spec_id,content_hash,mission_version_ref FROM coverage_mission_company_model_specs "
                "WHERE company_ref=? ORDER BY created_at DESC,spec_id DESC LIMIT 1", (proof["company_ref"],)).fetchone()
            if spec is None or tuple(spec) != (proof["spec_ref"], proof["spec_hash"], proof["mission_version_ref"]):
                return False
            if proof["basis"] == "current_formal_forecast_gap":
                model = core.execute("SELECT version_id,content_hash FROM forecast_model_versions WHERE company_ref=? "
                    "ORDER BY version_number DESC LIMIT 1", (proof["company_ref"],)).fetchone()
                if model is None or tuple(model) != (proof["forecast_ref"], proof["forecast_hash"]):
                    return False
            return time.monotonic() < deadline
    except (OSError, sqlite3.Error, KeyError, TypeError):
        return False
