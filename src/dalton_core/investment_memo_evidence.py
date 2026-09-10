"""Read-only replay of the formal model evidence behind an Investment Memo."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping, Sequence
from contextlib import closing
from pathlib import Path
from typing import Any

from .cockpit_model import unwrap_json_object
from .model_fallback_chain import served_family
from .model_router import ModelRouter, ModelRouterError
from .scheduler import SchedulerError, WorkOrder
from .store import canonical_json, content_hash


class InvestmentMemoEvidenceError(ValueError):
    pass


def _scheduler_connection(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    return connection


def _formal_call(connection: sqlite3.Connection, router: ModelRouter,
                 call: Mapping[str, Any], *, purpose: str,
                 mission: Mapping[str, Any], producer_routes: Sequence[str] = ()) -> dict[str, Any]:
    row = connection.execute(
        "SELECT work_order_json,work_order_hash FROM scheduler_work_orders WHERE work_order_id=?",
        (call["work_order_ref"],),
    ).fetchone()
    if row is None:
        raise InvestmentMemoEvidenceError("formal WorkOrder is missing")
    work = WorkOrder.from_dict(json.loads(row["work_order_json"])).to_dict()
    if canonical_json(work) != row["work_order_json"] or content_hash(work) != row["work_order_hash"]:
        raise InvestmentMemoEvidenceError("formal WorkOrder authority drifted")
    metadata = work.get("metadata") or {}
    if (metadata.get("purpose") != purpose
            or metadata.get("mission_version_ref") != mission["id"]
            or metadata.get("mission_version_hash") != mission["content_hash"]
            or sorted(metadata.get("producer_route_decision_refs") or []) != sorted(producer_routes)):
        raise InvestmentMemoEvidenceError("formal WorkOrder input binding failed")
    route = router.get_decision(str(call["route_decision_ref"]))
    if (route.get("outcome") != "selected"
            or route.get("work_order_ref") != call["work_order_ref"]
            or route.get("work_order_hash") != row["work_order_hash"]):
        raise InvestmentMemoEvidenceError("route and WorkOrder do not match")
    result = connection.execute(
        "SELECT * FROM scheduler_formal_results WHERE work_order_id=?",
        (call["work_order_ref"],),
    ).fetchone()
    if result is None or result["terminal_state"] != "succeeded":
        raise InvestmentMemoEvidenceError("formal result is not succeeded")
    envelope = json.loads(result["result_envelope_json"])
    if (result["result_envelope_id"] != call["result_envelope_ref"]
            or envelope.get("id") != call["result_envelope_ref"]
            or envelope.get("work_order_ref") != call["work_order_ref"]
            or envelope.get("invocation_ref") != call["invocation_ref"]
            or envelope.get("status") != "succeeded"
            or (envelope.get("metadata") or {}).get("route_decision_ref") != route["id"]):
        raise InvestmentMemoEvidenceError("formal result provenance is inconsistent")
    family = served_family(router, route["id"])
    if not family:
        raise InvestmentMemoEvidenceError("served model family is unresolved")
    return {"work_order": work, "route": route, "envelope": envelope, "family": family}


def replay_memo_model_evidence(*, scheduler_db: Path, model_router_db: Path,
                               gate: Mapping[str, Any], mission: Mapping[str, Any]) -> dict[str, Any]:
    """Verify all five calls without initializing either write authority."""
    try:
        with closing(_scheduler_connection(scheduler_db)) as scheduler, \
                closing(ModelRouter(str(model_router_db), read_only=True)) as router:
            producers = []
            families = set()
            for call in gate["producer_calls"]:
                formal = _formal_call(scheduler, router, call,
                                      purpose="investment_memo", mission=mission)
                families.add(formal["family"])
                producers.append({
                    "group": call["group"], "status": "succeeded",
                    "work_order_ref": call["work_order_ref"],
                    "route_decision_ref": call["route_decision_ref"],
                    "result_envelope_ref": call["result_envelope_ref"],
                    "served_family": formal["family"],
                })
            verifier = gate["verifier"]
            formal = _formal_call(
                scheduler, router, verifier, purpose="investment_memo_verifier",
                mission=mission,
                producer_routes=verifier["producer_route_decision_refs"],
            )
            if formal["family"] in families:
                raise InvestmentMemoEvidenceError("verifier model family is not independent")
            output = unwrap_json_object(formal["envelope"]["outputs"]["text"])
            expected = {
                "verdict": verifier["verdict"],
                "verified_body_hash": gate["verified_body_hash"],
                "finding_codes": verifier["finding_codes"],
            }
            if set(output) != set(expected) or output != expected:
                raise InvestmentMemoEvidenceError("verifier formal output does not match the gate")
            return {
                "status": "verified", "producer_calls": producers,
                "verifier": {
                    "status": "succeeded", "verdict": output["verdict"],
                    "work_order_ref": verifier["work_order_ref"],
                    "route_decision_ref": verifier["route_decision_ref"],
                    "result_envelope_ref": verifier["result_envelope_ref"],
                    "served_family": formal["family"], "independent": True,
                },
            }
    except (KeyError, TypeError, json.JSONDecodeError, sqlite3.Error, OSError,
            ValueError, ModelRouterError, SchedulerError) as exc:
        return {"status": "unverified", "reason": str(exc),
                "producer_calls": [], "verifier": None}
