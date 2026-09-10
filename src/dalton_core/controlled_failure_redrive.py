"""Explicit append-only recovery for two repaired host completion failures."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from .store import canonical_json, content_hash
from .readonly_sqlite import connect_read_only
from .thesis_impact_budget import ThesisImpactBudgetStore

ACTOR = "automation:operator-recovery"
ELIGIBLE_CODES = frozenset({"HOST_COMPLETION_FAILED", "HOST_CONTROL_PROOF_MISSING"})
_REVIEWED_HOST_HASHES = {
    "completion_bundle": "49dda99ac8a3c036b2cafdd9c9b28e849868fa706efcc4cd015882c331596ed8",
    "runtime_proof": "26918355da740418539934495ab4612fe8e37aceb7ba8d26c27583709b5e1c68",
    "native_google_controls": "afa619910da140d3c2c95ae24076bbb20eb3a44a48da0d65b95517c0545c9859",
}

_ORIGINAL_TRANSPORT = """\tlet completionModel = getModelCompletionTransport(params.model) ?? prepareModelForSimpleCompletion({
\t\tapiRegistry: runtime?.registry ?? defaultApiRegistry,
\t\tmodel: params.model,
\t\tcfg: params.cfg
\t});"""
_PATCHED_TRANSPORT = """\tconst controlledTransport = params.options?.providerControls !== void 0;
\tconst boundCompletionTransport = getModelCompletionTransport(params.model);
\tlet completionModel = controlledTransport ? { ...params.model } : boundCompletionTransport ?? prepareModelForSimpleCompletion({
\t\tapiRegistry: runtime?.registry ?? defaultApiRegistry,
\t\tmodel: params.model,
\t\tcfg: params.cfg
\t});
\tif (controlledTransport && getModelLlmRuntime(completionModel)) throw new Error("Controlled completion retained a host-bound transport");"""
_ORIGINAL_BIND = "\tif (runtime) completionModel = bindModelLlmRuntime(completionModel, runtime);"
_PATCHED_BIND = "\tif (runtime && !controlledTransport) completionModel = bindModelLlmRuntime(completionModel, runtime);"


class ControlledFailureRedriveError(RuntimeError):
    pass


def _connect_existing_writable(path: str | Path) -> sqlite3.Connection:
    """Open an existing authority without permitting SQLite to create it."""
    if str(path) == ":memory:":
        raise ControlledFailureRedriveError("apply requires an existing authority database")
    target = Path(path).absolute()
    connection = None
    try:
        with target.open("rb") as stream:
            if stream.read(16) != b"SQLite format 3\x00":
                raise ControlledFailureRedriveError(
                    "apply target is not an SQLite authority database"
                )
        connection = sqlite3.connect(
            target.as_uri() + "?mode=rw", uri=True, isolation_level=None
        )
        connection.execute("PRAGMA busy_timeout=5000")
        # Opening a WAL database for an ordinary read provisions its transient
        # WAL/SHM pair.  Keep this connection alive across the strict read-only
        # revalidation and both append-only writes below.
        connection.execute("SELECT name FROM sqlite_master LIMIT 1").fetchone()
        return connection
    except ControlledFailureRedriveError:
        raise
    except (OSError, sqlite3.Error) as exc:
        if connection is not None:
            connection.close()
        raise ControlledFailureRedriveError(
            "apply requires an existing writable authority database"
        ) from exc


def installed_repair(openclaw_root: str | Path) -> dict[str, str]:
    root = Path(openclaw_root).resolve()
    try:
        package = root / "package.json"
        package_wire = json.loads(package.read_text("utf-8"))
        if package_wire.get("version") != "2026.9.3":
            raise ValueError("unsupported OpenClaw version")
        matches = sorted((root / "dist").glob("simple-completion-execution-*.mjs"))
        if len(matches) != 1:
            raise ValueError("managed completion bundle is ambiguous")
        bundle = matches[0]
        source = bundle.read_text("utf-8")
        runtime = root / "dist" / "runtime-llm.runtime-DdpXXHBe.mjs"
        google = root / "node_modules" / "@openclaw" / "ai" / "dist" / "google-shared-BedY23XS.mjs"
        if (source.count(_PATCHED_TRANSPORT) != 1
                or source.count(_PATCHED_BIND) != 1
                or source.count(_ORIGINAL_TRANSPORT) != 0
                or source.count(_ORIGINAL_BIND) != 0):
            raise ValueError("controlled transport patch is absent, partial, or duplicated")
        observed = {
            "completion_bundle": hashlib.sha256(bundle.read_bytes()).hexdigest(),
            "runtime_proof": hashlib.sha256(runtime.read_bytes()).hexdigest(),
            "native_google_controls": hashlib.sha256(google.read_bytes()).hexdigest(),
        }
        if observed != _REVIEWED_HOST_HASHES:
            raise ValueError("managed host bytes differ from the reviewed repair set")
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise ControlledFailureRedriveError(
            "managed OpenClaw controlled transport repair is not installed"
        ) from exc
    return {
        "openclaw_root": str(root),
        "openclaw_version": "2026.9.3",
        "bundle_path": str(bundle),
        "bundle_sha256": hashlib.sha256(bundle.read_bytes()).hexdigest(),
        "runtime_proof_path": str(runtime),
        "runtime_proof_sha256": observed["runtime_proof"],
        "native_google_controls_path": str(google),
        "native_google_controls_sha256": observed["native_google_controls"],
        "package_sha256": hashlib.sha256(package.read_bytes()).hexdigest(),
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
            old_work_order_ref: str, openclaw_root: str | Path) -> dict[str, Any]:
    with closing(connect_read_only(scheduler_db)) as connection:
        connection.row_factory = sqlite3.Row
        formal = connection.execute(
            "SELECT attempt_number,result_envelope_id,result_envelope_hash,"
            "result_envelope_json,terminal_state "
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
    if (canonical_json(envelope) != formal["result_envelope_json"]
            or content_hash(envelope) != formal["result_envelope_hash"]):
        raise ControlledFailureRedriveError("formal result envelope authority is invalid")
    code = _eligible_code(envelope)
    if formal["terminal_state"] != "failed" or code is None:
        raise ControlledFailureRedriveError("formal failure is not eligible for controlled redrive")
    work_wire = json.loads(work["work_order_json"])
    if (canonical_json(work_wire) != work["work_order_json"]
            or content_hash(work_wire) != work["work_order_hash"]):
        raise ControlledFailureRedriveError("old WorkOrder authority is invalid")
    with closing(connect_read_only(budget_db)) as budget_connection:
        budget_connection.row_factory = sqlite3.Row
        rows = budget_connection.execute(
            "SELECT a.admission_id,a.content_hash AS admission_hash,a.reserved_micros,"
            "a.attempt_number,s.settlement_id,s.content_hash AS settlement_hash,"
            "s.actual_micros,b.record_json AS binding_json "
            "FROM thesis_impact_day_admissions a JOIN thesis_impact_day_settlements s "
            "ON s.admission_id=a.admission_id JOIN model_mission_budget_bindings b "
            "ON b.admission_id=a.admission_id WHERE a.work_order_ref=? AND a.attempt_number=?",
            (old_work_order_ref, formal["attempt_number"]),
        ).fetchall()
    if len(rows) != 1:
        raise ControlledFailureRedriveError("old work has no unique mission-bound cost authority")
    row = rows[0]
    binding = json.loads(row["binding_json"])
    mission_ref = binding.get("mission_version_ref")
    mission_hash = binding.get("mission_version_hash")
    if not isinstance(mission_ref, str) or not isinstance(mission_hash, str):
        raise ControlledFailureRedriveError("cost authority has no exact mission binding")
    metadata = work_wire.get("metadata") or {}
    if (metadata.get("mission_version_ref") != mission_ref
            or (metadata.get("mission_version_hash") is not None
                and metadata.get("mission_version_hash") != mission_hash)):
        raise ControlledFailureRedriveError(
            "WorkOrder and cost authority mission bindings disagree"
        )
    if row["actual_micros"] >= row["reserved_micros"]:
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
        "repair": installed_repair(openclaw_root),
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
    # Candidate preparation is deliberately strict read-only.  Apply is the
    # explicit writable operation, so it may provision transient WAL sidecars,
    # but only after the reviewed candidate hash has passed and only for exact
    # existing database files opened with SQLite's mode=rw.
    with closing(_connect_existing_writable(scheduler_db)), closing(
        _connect_existing_writable(budget_db)
    ):
        fresh = prepare(scheduler_db=scheduler_db, budget_db=budget_db,
                        old_work_order_ref=candidate["old_work_order_ref"],
                        openclaw_root=candidate["repair"]["openclaw_root"])
        if (fresh != candidate
                or candidate.get("repair") != installed_repair(
                    candidate["repair"]["openclaw_root"]
                )):
            raise ControlledFailureRedriveError(
                "failure, mission, cost, or installed repair changed"
            )
        with ThesisImpactBudgetStore(budget_db) as budget:
            correction = budget.correct_uncertain_settlement(
                candidate["admission_id"], settlement_id=candidate["settlement_id"],
                corrected_micros=candidate["corrected_micros"],
                evidence_ref="managed-openclaw-bundle:" + candidate["repair"]["openclaw_version"],
                evidence_hash=candidate["repair"]["bundle_sha256"], actor_ref=ACTOR,
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
            connection.execute("PRAGMA busy_timeout=5000")
            connection.execute("PRAGMA foreign_keys=ON")
            connection.executescript(Path(__file__).with_name("scheduler_schema.sql").read_text())
            connection.execute("BEGIN IMMEDIATE")
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


def approved_request(scheduler_db: str | Path, budget_db: str | Path, *, old_work_order_ref: str,
                     formal: Mapping[str, Any], mission: Mapping[str, Any]) -> str | None:
    connection = connect_read_only(scheduler_db)
    try:
        row = connection.execute(
            "SELECT r.record_json,r.content_hash,w.work_order_hash "
            "FROM controlled_failure_redrives r JOIN scheduler_work_orders w "
            "ON w.work_order_id=r.old_work_order_ref WHERE r.old_work_order_ref=?",
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
            or record.get("old_work_order_hash") != row[2]
            or content_hash({key: value for key, value in record.items()
                             if key != "content_hash"}) != row[1]):
        return None
    envelope = formal.get("result_envelope") or {}
    try:
        repair_current = (
            record.get("repair") == installed_repair(
                record["repair"]["openclaw_root"]
            )
        )
    except (ControlledFailureRedriveError, KeyError, TypeError):
        return None
    if (formal.get("terminal_state") != "failed"
            or content_hash(envelope) != formal.get("result_envelope_hash")
            or record.get("failure_code") != _eligible_code(envelope)
            or record.get("formal_result_envelope_hash") != formal.get("result_envelope_hash")
            or record.get("mission_version_ref") != mission.get("id")
            or record.get("mission_version_hash") != mission.get("content_hash")
            or record.get("old_work_order_ref") != old_work_order_ref
            or not repair_current):
        return None
    try:
        with closing(connect_read_only(budget_db)) as budget_connection:
            budget_connection.row_factory = sqlite3.Row
            correction = budget_connection.execute(
                "SELECT record_json,content_hash FROM thesis_impact_settlement_corrections "
                "WHERE correction_id=?", (record["cost_correction_ref"],),
            ).fetchone()
    except (sqlite3.Error, OSError, ValueError):
        return None
    if correction is None:
        return None
    correction_wire = json.loads(correction["record_json"])
    if (canonical_json(correction_wire) != correction["record_json"]
            or correction_wire.get("content_hash") != correction["content_hash"]
            or content_hash({key: value for key, value in correction_wire.items()
                             if key != "content_hash"}) != correction["content_hash"]
            or correction_wire.get("admission_id") != record["admission_id"]
            or correction_wire.get("settlement_id") != record["settlement_id"]
            or correction_wire.get("admission_hash") != record["admission_hash"]
            or correction_wire.get("settlement_hash") != record["settlement_hash"]
            or correction_wire.get("corrected_micros") != record["corrected_micros"]
            or correction_wire.get("evidence_hash") != record["repair"]["bundle_sha256"]
            or correction_wire.get("reason") != "historical_completion_unknown"
            or correction_wire.get("actor_ref") != ACTOR):
        return None
    return ":operator-recovery:" + record["content_hash"][:16]
