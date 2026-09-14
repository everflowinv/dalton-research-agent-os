"""One reviewed reentry for a model call rejected before provider dispatch."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Collection, Mapping

from .readonly_sqlite import connect_read_only
from .store import canonical_json, content_hash

KIND = "proved_pool_exhausted_no_send"
ERROR_CODE = "POOL_EXHAUSTED"
ACTOR = "automation:operator-budget-recovery"


class ControlledBudgetReentryError(RuntimeError):
    pass


def _load_authority(*, scheduler_db: str | Path, budget_db: str | Path,
                    core_db: str | Path, old_work_order_ref: str) -> dict[str, Any]:
    with closing(connect_read_only(scheduler_db)) as db:
        db.row_factory = sqlite3.Row
        work = db.execute(
            "SELECT work_order_json,work_order_hash FROM scheduler_work_orders "
            "WHERE work_order_id=?", (old_work_order_ref,)).fetchone()
        formal = db.execute(
            "SELECT * FROM scheduler_formal_results WHERE work_order_id=?",
            (old_work_order_ref,)).fetchone()
    if work is None or formal is None:
        raise ControlledBudgetReentryError("failed WorkOrder authority is missing")
    work_wire = json.loads(work["work_order_json"])
    envelope = json.loads(formal["result_envelope_json"])
    formal_body = {
        "id": formal["result_record_id"],
        "work_order_id": formal["work_order_id"],
        "attempt_number": formal["attempt_number"],
        "result_envelope_id": formal["result_envelope_id"],
        "result_envelope_hash": formal["result_envelope_hash"],
        "terminal_state": formal["terminal_state"],
        "created_at": formal["created_at"],
    }
    if (canonical_json(work_wire) != work["work_order_json"]
            or content_hash(work_wire) != work["work_order_hash"]):
        raise ControlledBudgetReentryError("failed WorkOrder authority is invalid")
    if (canonical_json(envelope) != formal["result_envelope_json"]
            or content_hash(envelope) != formal["result_envelope_hash"]
            or content_hash(formal_body) != formal["content_hash"]
            or formal["terminal_state"] != "failed"
            or (envelope.get("error") or {}).get("code") != ERROR_CODE
            or envelope.get("outputs") != {}):
        raise ControlledBudgetReentryError("formal result is not an empty pool refusal")
    attempt = int(formal["attempt_number"])
    with closing(connect_read_only(budget_db)) as db:
        db.row_factory = sqlite3.Row
        rejected = db.execute(
            "SELECT * FROM model_budget_pool_rejections WHERE work_order_ref=? "
            "AND attempt_number=?", (old_work_order_ref, attempt)).fetchall()
        admitted = db.execute(
            "SELECT 1 FROM thesis_impact_day_admissions WHERE work_order_ref=? "
            "AND attempt_number=? LIMIT 1", (old_work_order_ref, attempt)).fetchone()
    if len(rejected) != 1 or admitted is not None:
        raise ControlledBudgetReentryError("pool refusal has no unique pre-dispatch rejection")
    rejection = rejected[0]
    rejection_wire = json.loads(rejection["record_json"])
    if (canonical_json(rejection_wire) != rejection["record_json"]
            or rejection_wire.get("content_hash") != rejection["content_hash"]
            or content_hash({k: v for k, v in rejection_wire.items()
                             if k != "content_hash"}) != rejection["content_hash"]
            or rejection_wire.get("reason") != "pool_exhausted"):
        raise ControlledBudgetReentryError("pool rejection authority is invalid")
    with closing(connect_read_only(core_db)) as db:
        sent = db.execute(
            "SELECT 1 FROM model_invocations WHERE work_order_ref=? LIMIT 1",
            (old_work_order_ref,)).fetchone()
    if sent is not None:
        raise ControlledBudgetReentryError("failed WorkOrder has a model invocation")
    metadata = work_wire.get("metadata") or {}
    return {
        "old_work_order_ref": old_work_order_ref,
        "old_work_order_hash": work["work_order_hash"],
        "attempt_number": attempt,
        "formal_result_ref": formal["result_record_id"],
        "formal_result_hash": formal["content_hash"],
        "formal_result_envelope_ref": formal["result_envelope_id"],
        "formal_result_envelope_hash": formal["result_envelope_hash"],
        "rejection_ref": rejection["rejection_id"],
        "rejection_hash": rejection["content_hash"],
        "pool": rejection["pool"],
        "pool_lane": rejection["pool_lane"],
        "purpose": metadata.get("purpose"),
        "mission_version_ref": metadata.get("mission_version_ref"),
        "mission_version_hash": metadata.get("mission_version_hash"),
    }


def prepare(*, scheduler_db: str | Path, budget_db: str | Path,
            core_db: str | Path, old_work_order_ref: str, business_key: str,
            current_permission: str, mission: Mapping[str, Any],
            allowed_purpose: str,
            group_work_order_refs: Collection[str] | None = None) -> dict[str, Any]:
    members = sorted(set(group_work_order_refs or (old_work_order_ref,)))
    if old_work_order_ref not in members or not members:
        raise ControlledBudgetReentryError("recovery group does not contain this work")
    proofs = [_load_authority(
        scheduler_db=scheduler_db, budget_db=budget_db, core_db=core_db,
        old_work_order_ref=ref) for ref in members]
    if any(
        proof["purpose"] != allowed_purpose
        or proof["mission_version_ref"] != mission.get("id")
        or proof["mission_version_hash"] != mission.get("content_hash")
        for proof in proofs
    ):
        raise ControlledBudgetReentryError("work purpose or current mission changed")
    proof = next(row for row in proofs
                 if row["old_work_order_ref"] == old_work_order_ref)
    if not business_key or not current_permission:
        raise ControlledBudgetReentryError("business and permission keys are required")
    candidate = {
        "schema_version": "budget-controlled-reentry-0.1", "kind": KIND,
        **proof, "business_key": business_key,
        "current_permission": current_permission, "actor_ref": ACTOR,
        "group_work_order_refs": members,
        "group_hash": content_hash({
            "business_key": business_key,
            "current_permission": current_permission,
            "mission_version_ref": mission.get("id"),
            "mission_version_hash": mission.get("content_hash"),
            "purpose": allowed_purpose,
            "members": [{"work_order_ref": row["old_work_order_ref"],
                         "work_order_hash": row["old_work_order_hash"],
                         "formal_result_envelope_hash": row["formal_result_envelope_hash"],
                         "rejection_hash": row["rejection_hash"]}
                        for row in proofs],
        }),
    }
    candidate["candidate_hash"] = content_hash(candidate)
    return candidate


def apply(*, scheduler_db: str | Path, budget_db: str | Path,
          core_db: str | Path, candidate: Mapping[str, Any],
          expected_candidate_hash: str, mission: Mapping[str, Any]) -> dict[str, Any]:
    candidate = dict(candidate)
    if (candidate.get("candidate_hash") != expected_candidate_hash
            or content_hash({k: v for k, v in candidate.items()
                             if k != "candidate_hash"}) != expected_candidate_hash):
        raise ControlledBudgetReentryError("reviewed candidate changed")
    fresh = prepare(
        scheduler_db=scheduler_db, budget_db=budget_db, core_db=core_db,
        old_work_order_ref=candidate["old_work_order_ref"],
        business_key=candidate["business_key"],
        current_permission=candidate["current_permission"], mission=mission,
        allowed_purpose=candidate["purpose"])
    if candidate.get("group_work_order_refs") != fresh.get("group_work_order_refs"):
        fresh = prepare(
            scheduler_db=scheduler_db, budget_db=budget_db, core_db=core_db,
            old_work_order_ref=candidate["old_work_order_ref"],
            business_key=candidate["business_key"],
            current_permission=candidate["current_permission"], mission=mission,
            allowed_purpose=candidate["purpose"],
            group_work_order_refs=candidate["group_work_order_refs"])
    if fresh != candidate:
        raise ControlledBudgetReentryError("recovery authority changed before apply")
    created_at = datetime.now(timezone.utc).isoformat(timespec="microseconds")
    record = {
        "schema_version": "budget-controlled-reentry-0.1",
        "recovery_id": "controlled-budget-reentry:" + expected_candidate_hash[:32],
        **candidate, "created_at": created_at,
    }
    record["content_hash"] = content_hash(record)
    db = sqlite3.connect(scheduler_db)
    try:
        db.execute("PRAGMA busy_timeout=5000")
        db.execute("BEGIN IMMEDIATE")
        prior = db.execute(
            "SELECT record_json FROM controlled_failure_redrives "
            "WHERE old_work_order_ref=?", (candidate["old_work_order_ref"],)).fetchone()
        if prior is not None:
            saved = json.loads(prior[0])
            if (saved.get("kind") != KIND
                    or saved.get("candidate_hash") != expected_candidate_hash):
                raise ControlledBudgetReentryError("old failure already has another recovery")
            return {**saved, "status": "duplicate"}
        db.execute(
            "INSERT INTO controlled_failure_redrives VALUES(?,?,?,?,?)",
            (record["recovery_id"], record["old_work_order_ref"],
             canonical_json(record), record["content_hash"], created_at))
        db.commit()
    finally:
        db.close()
    return {**record, "status": "fresh"}


def _record(scheduler_db: str | Path, *, old_work_order_ref: str) -> dict[str, Any] | None:
    try:
        with closing(connect_read_only(scheduler_db)) as db:
            db.row_factory = sqlite3.Row
            row = db.execute(
                "SELECT record_json,content_hash FROM controlled_failure_redrives "
                "WHERE old_work_order_ref=?", (old_work_order_ref,)).fetchone()
    except (OSError, sqlite3.Error):
        return None
    if row is None:
        return None
    try:
        value = json.loads(row["record_json"])
    except (TypeError, ValueError):
        return None
    body = {k: v for k, v in value.items() if k != "content_hash"}
    if (canonical_json(value) != row["record_json"]
            or value.get("content_hash") != row["content_hash"]
            or content_hash(body) != row["content_hash"]
            or value.get("kind") != KIND):
        return None
    return value


def approved_request(scheduler_db: str | Path, budget_db: str | Path, *,
                     old_work_order_ref: str, formal: Mapping[str, Any],
                     mission: Mapping[str, Any]) -> str | None:
    record = _record(scheduler_db, old_work_order_ref=old_work_order_ref)
    envelope = formal.get("result_envelope") or {}
    if record is None or (
        formal.get("terminal_state") != "failed"
        or content_hash(envelope) != formal.get("result_envelope_hash")
        or record.get("formal_result_envelope_hash") != formal.get("result_envelope_hash")
        or record.get("mission_version_ref") != mission.get("id")
        or record.get("mission_version_hash") != mission.get("content_hash")
        or record.get("old_work_order_ref") != old_work_order_ref
        or (envelope.get("error") or {}).get("code") != ERROR_CODE
        or envelope.get("outputs") != {}
    ):
        return None
    return ":operator-recovery:" + record["content_hash"][:16]


def approved_business_key(scheduler_db: str | Path, *, business_key: str,
                          current_permission: str, mission: Mapping[str, Any],
                          allowed_purposes: Collection[str]) -> str | None:
    try:
        with closing(connect_read_only(scheduler_db)) as db:
            rows = db.execute(
                "SELECT old_work_order_ref FROM controlled_failure_redrives"
            ).fetchall()
    except (OSError, sqlite3.Error):
        return None
    matches = []
    for row in rows:
        record = _record(scheduler_db, old_work_order_ref=row[0])
        if record is not None and (
            record.get("business_key") == business_key
            and record.get("current_permission") == current_permission
            and record.get("mission_version_ref") == mission.get("id")
            and record.get("mission_version_hash") == mission.get("content_hash")
            and record.get("purpose") in allowed_purposes
        ):
            matches.append(record)
    if len(matches) != 1:
        if not matches:
            return None
    refs = sorted(record["old_work_order_ref"] for record in matches)
    group_hashes = {record.get("group_hash") for record in matches}
    if (len(group_hashes) != 1
            or any(record.get("group_work_order_refs") != refs for record in matches)):
        return None
    group_hash = next(iter(group_hashes))
    if not isinstance(group_hash, str) or len(group_hash) != 64:
        return None
    return ":operator-recovery:" + group_hash[:16]


__all__ = [
    "ControlledBudgetReentryError", "apply", "approved_business_key",
    "approved_request", "prepare",
]
