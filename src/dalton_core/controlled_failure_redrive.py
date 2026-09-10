"""Explicit append-only recovery for two repaired host completion failures."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from .store import canonical_json, content_hash
from .thesis_impact_budget import ThesisImpactBudgetStore

ACTOR = "automation:operator-recovery"
ELIGIBLE_CODES = frozenset({"HOST_COMPLETION_FAILED", "HOST_CONTROL_PROOF_MISSING"})


class ControlledFailureRedriveError(RuntimeError):
    pass


def _repair_source() -> Path:
    return Path(__file__).with_name("openclaw_model_adapter.py").resolve()


def installed_repair() -> dict[str, str]:
    path = _repair_source()
    raw = path.read_bytes()
    text = raw.decode("utf-8")
    required = ("BrokerDefinitelyNotSent", "before_send", "requiredControls")
    if any(marker not in text for marker in required):
        raise ControlledFailureRedriveError("installed model transport repair is incomplete")
    return {
        "source_ref": "dalton-python:openclaw_model_adapter.py",
        "source_sha256": hashlib.sha256(raw).hexdigest(),
    }


def _eligible_code(envelope: Mapping[str, Any]) -> str | None:
    error = envelope.get("error") or {}
    if error.get("code") in ELIGIBLE_CODES:
        return str(error["code"])
    failures = (envelope.get("metadata") or {}).get("chain_failures") or []
    codes = {row.get("code") for row in failures if isinstance(row, Mapping)}
    matches = codes & ELIGIBLE_CODES
    return next(iter(matches)) if len(matches) == 1 else None


def prepare(*, scheduler_db: str | Path, budget_db: str | Path,
            old_work_order_ref: str) -> dict[str, Any]:
    uri = f"file:{Path(scheduler_db).resolve().as_posix()}?mode=ro"
    with sqlite3.connect(uri, uri=True) as connection:
        connection.row_factory = sqlite3.Row
        formal = connection.execute(
            "SELECT result_envelope_id,result_envelope_hash,result_envelope_json,terminal_state "
            "FROM scheduler_formal_results WHERE work_order_id=?",
            (old_work_order_ref,),
        ).fetchone()
        work = connection.execute(
            "SELECT work_order_json,work_order_hash FROM scheduler_work_orders "
            "WHERE work_order_id=?", (old_work_order_ref,),
        ).fetchone()
    if formal is None or work is None:
        raise ControlledFailureRedriveError("old work or formal result is missing")
    envelope = json.loads(formal["result_envelope_json"])
    code = _eligible_code(envelope)
    if formal["terminal_state"] != "failed" or code is None:
        raise ControlledFailureRedriveError("formal failure is not eligible for controlled redrive")
    work_wire = json.loads(work["work_order_json"])
    metadata = work_wire.get("metadata") or {}
    mission_ref = metadata.get("mission_version_ref")
    mission_hash = metadata.get("mission_version_hash")
    if not isinstance(mission_ref, str) or not isinstance(mission_hash, str):
        raise ControlledFailureRedriveError("old work has no exact mission binding")
    with ThesisImpactBudgetStore(budget_db) as budget:
        row = budget.connection.execute(
            "SELECT a.admission_id,a.content_hash AS admission_hash,a.reserved_micros,"
            "s.settlement_id,s.content_hash AS settlement_hash,s.actual_micros "
            "FROM thesis_impact_day_admissions a JOIN thesis_impact_day_settlements s "
            "ON s.admission_id=a.admission_id WHERE a.work_order_ref=?",
            (old_work_order_ref,),
        ).fetchone()
    if row is None or row["actual_micros"] >= row["reserved_micros"]:
        raise ControlledFailureRedriveError("old work has no conservatively correctable cost authority")
    candidate = {
        "schema_version": "0.1",
        "old_work_order_ref": old_work_order_ref,
        "old_work_order_hash": work["work_order_hash"],
        "formal_result_envelope_ref": formal["result_envelope_id"],
        "formal_result_envelope_hash": formal["result_envelope_hash"],
        "failure_code": code,
        "mission_version_ref": mission_ref,
        "mission_version_hash": mission_hash,
        "admission_id": row["admission_id"],
        "admission_hash": row["admission_hash"],
        "settlement_id": row["settlement_id"],
        "settlement_hash": row["settlement_hash"],
        "corrected_micros": row["reserved_micros"],
        "repair": installed_repair(),
        "actor_ref": ACTOR,
    }
    candidate["candidate_hash"] = content_hash(candidate)
    return candidate


def apply(*, scheduler_db: str | Path, budget_db: str | Path,
          candidate: Mapping[str, Any], expected_candidate_hash: str) -> dict[str, Any]:
    candidate = dict(candidate)
    if candidate.get("candidate_hash") != expected_candidate_hash:
        raise ControlledFailureRedriveError("reviewed candidate hash does not match")
    body = dict(candidate)
    body.pop("candidate_hash", None)
    if content_hash(body) != expected_candidate_hash:
        raise ControlledFailureRedriveError("reviewed candidate was changed")
    fresh = prepare(scheduler_db=scheduler_db, budget_db=budget_db,
                    old_work_order_ref=candidate["old_work_order_ref"])
    if fresh != candidate or candidate.get("repair") != installed_repair():
        raise ControlledFailureRedriveError("failure, mission, cost, or installed repair changed")
    with ThesisImpactBudgetStore(budget_db) as budget:
        correction = budget.correct_uncertain_settlement(
            candidate["admission_id"], settlement_id=candidate["settlement_id"],
            corrected_micros=candidate["corrected_micros"],
            evidence_ref=candidate["repair"]["source_ref"],
            evidence_hash=candidate["repair"]["source_sha256"], actor_ref=ACTOR,
            idempotency_key="controlled-redrive-cost:" + expected_candidate_hash,
        )
    record = {
        "schema_version": "0.1", "recovery_id": "controlled-redrive:" + expected_candidate_hash[:32],
        **candidate, "cost_correction_ref": correction["correction_id"],
        "created_at": datetime.now(timezone.utc).isoformat(timespec="microseconds"),
    }
    record["content_hash"] = content_hash(record)
    connection = sqlite3.connect(scheduler_db)
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.executescript(Path(__file__).with_name("scheduler_schema.sql").read_text())
        prior = connection.execute(
            "SELECT record_json FROM controlled_failure_redrives WHERE old_work_order_ref=?",
            (candidate["old_work_order_ref"],),
        ).fetchone()
        if prior is not None:
            saved = json.loads(prior[0])
            if any(saved.get(k) != record.get(k) for k in (
                "recovery_id", "candidate_hash", "cost_correction_ref")):
                raise ControlledFailureRedriveError("old failure already has another recovery")
            return {**saved, "status": "duplicate"}
        connection.execute(
            "INSERT INTO controlled_failure_redrives VALUES(?,?,?,?,?)",
            (record["recovery_id"], candidate["old_work_order_ref"],
             canonical_json(record), record["content_hash"], record["created_at"]),
        )
        connection.commit()
    finally:
        connection.close()
    return {**record, "status": "fresh"}


def approved_request(scheduler_db: str | Path, *, old_work_order_ref: str,
                     formal: Mapping[str, Any], mission: Mapping[str, Any]) -> str | None:
    connection = sqlite3.connect(f"file:{Path(scheduler_db).resolve().as_posix()}?mode=ro", uri=True)
    try:
        row = connection.execute(
            "SELECT record_json,content_hash FROM controlled_failure_redrives WHERE old_work_order_ref=?",
            (old_work_order_ref,),
        ).fetchone()
    except sqlite3.OperationalError:
        return None
    finally:
        connection.close()
    if row is None:
        return None
    record = json.loads(row[0])
    if (canonical_json(record) != row[0]
            or record.get("content_hash") != row[1]
            or content_hash({key: value for key, value in record.items()
                             if key != "content_hash"}) != row[1]):
        return None
    envelope = formal.get("result_envelope") or {}
    if (formal.get("terminal_state") != "failed"
            or record.get("failure_code") != _eligible_code(envelope)
            or record.get("formal_result_envelope_hash") != formal.get("result_envelope_hash")
            or record.get("mission_version_ref") != mission.get("id")
            or record.get("mission_version_hash") != mission.get("content_hash")
            or record.get("repair") != installed_repair()):
        return None
    return ":operator-recovery:" + record["content_hash"][:16]
