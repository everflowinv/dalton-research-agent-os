#!/usr/bin/env python3
"""Two-phase, read-only product audit for R15-compatible successors.

``baseline`` freezes the pre-observation identities of old directed-document
admissions and model/spec rows. ``postdeploy`` binds a deployment receipt and
cutoff, then reports only durable SQLite/file evidence. It never imports or
starts a Dalton lane, provider, model, connector, or network client.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import subprocess
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

R15_REVIEWED_BASE = "d1079bafc565e1bfa7d19d3360504fa845e9b327"
EXPECTED_DEPLOYMENT_SCHEMA = "successor-stopped-window-execution-0.1"
EXPECTED_INSTALLER_STATUS = "installer_finished_runtime_health_pending"
DEFAULT_STATE = Path.home() / "Library/Application Support/Dalton/state/dalton-core"
DEFAULT_TRACKED = (
    # DXC and EPAM were admitted before R15 and reached a legacy cross-UTC
    # atomic day-budget refusal. Their immutable history must remain visible.
    "mission-document-research-admission:ca9bac39454f3ee9bcfd3f0508f342e3",
    "mission-document-research-admission:6a2bcd446237e1bd9732690b4b542b3a",
)
MAX_JSON_BYTES = 16 * 1024 * 1024
PLANNER_SCHEMA_VERSION = "0.1"
PLANNER_TASK_REF = "task:research-plan-directives:0.1"


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False)


def content_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def sha_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def parse_time(value: str, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as exc:
        raise RuntimeError(f"{field} is not an ISO timestamp") from exc
    if parsed.tzinfo is None:
        raise RuntimeError(f"{field} must include a timezone")
    return parsed.astimezone(timezone.utc)


def source_commit(value: str) -> str:
    if len(value) != 40 or any(char not in "0123456789abcdef" for char in value):
        raise RuntimeError("source commit must be one full lowercase Git SHA")
    return value


def create(path: Path, value: bytes) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(value)
        stream.flush()
        os.fsync(stream.fileno())


def write_json(path: Path, value: Any) -> None:
    create(path, (json.dumps(value, ensure_ascii=False, sort_keys=True,
                             separators=(",", ":"), allow_nan=False) + "\n").encode())


def stable_json(path: Path, *, label: str) -> tuple[dict[str, Any], str]:
    if path.is_symlink():
        raise RuntimeError(f"{label} may not be a symlink")
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        before = os.fstat(descriptor)
        if not os.path.isfile(path):
            raise RuntimeError(f"{label} is not a regular file")
        chunks = []
        remaining = MAX_JSON_BYTES + 1
        while remaining:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        after = os.fstat(descriptor)
        current = path.lstat()
    finally:
        os.close(descriptor)
    if len(raw) > MAX_JSON_BYTES:
        raise RuntimeError(f"{label} exceeds the read bound")
    if len(raw) != before.st_size:
        raise RuntimeError(f"{label} was not read completely")
    if ((before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
            != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
            or (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
            != (current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns)):
        raise RuntimeError(f"{label} changed while read")
    try:
        value = json.loads(raw)
    except (json.JSONDecodeError, RecursionError) as exc:
        raise RuntimeError(f"{label} is invalid JSON") from exc
    if not isinstance(value, Mapping):
        raise RuntimeError(f"{label} is not an object")
    return dict(value), sha_bytes(raw)


def ro(path: Path) -> sqlite3.Connection:
    if path.is_symlink() or not path.is_file():
        raise RuntimeError(f"SQLite authority is unavailable: {path}")
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    connection.execute("BEGIN")
    return connection


def table_exists(connection: sqlite3.Connection, table: str) -> bool:
    return connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone() is not None


def exact_record(row: sqlite3.Row, *, label: str,
                 json_column: str = "record_json") -> dict[str, Any]:
    try:
        wire = json.loads(row[json_column])
    except (TypeError, json.JSONDecodeError, RecursionError) as exc:
        raise RuntimeError(f"{label} JSON is invalid") from exc
    if not isinstance(wire, Mapping) or canonical_json(wire) != row[json_column]:
        raise RuntimeError(f"{label} JSON is not canonical")
    body = dict(wire)
    asserted = body.pop("content_hash", None)
    if asserted != row["content_hash"] or asserted != content_hash(body):
        raise RuntimeError(f"{label} content hash differs")
    return dict(wire)


def exact_work(row: sqlite3.Row) -> dict[str, Any]:
    try:
        wire = json.loads(row["work_order_json"])
    except (TypeError, json.JSONDecodeError, RecursionError) as exc:
        raise RuntimeError("Scheduler WorkOrder JSON is invalid") from exc
    if (not isinstance(wire, Mapping)
            or canonical_json(wire) != row["work_order_json"]
            or content_hash(wire) != row["work_order_hash"]
            or wire.get("id") != row["work_order_id"]):
        raise RuntimeError("Scheduler WorkOrder authority differs")
    return dict(wire)


def exact_formal(row: sqlite3.Row) -> dict[str, Any]:
    try:
        envelope = json.loads(row["result_envelope_json"])
    except (TypeError, json.JSONDecodeError, RecursionError) as exc:
        raise RuntimeError("Scheduler ResultEnvelope JSON is invalid") from exc
    if (not isinstance(envelope, Mapping)
            or canonical_json(envelope) != row["result_envelope_json"]
            or content_hash(envelope) != row["result_envelope_hash"]
            or envelope.get("id") != row["result_envelope_id"]
            or envelope.get("work_order_ref") != row["work_order_id"]):
        raise RuntimeError("Scheduler formal ResultEnvelope authority differs")
    return {
        "result_record_ref": row["result_record_id"],
        "attempt_number": row["attempt_number"],
        "terminal_state": row["terminal_state"],
        "result_envelope_ref": row["result_envelope_id"],
        "result_envelope_hash": row["result_envelope_hash"],
        "formal_content_hash": row["content_hash"],
        "created_at": row["created_at"],
        "invocation_ref": envelope.get("invocation_ref"),
        "error": envelope.get("error"),
        "usage_refs": envelope.get("usage_refs") or [],
        "metadata": envelope.get("metadata") or {},
        "outputs_hash": content_hash(envelope.get("outputs") or {}),
    }


def exact_model(row: sqlite3.Row) -> dict[str, Any]:
    wire = exact_record(row, label="forecast model")
    if (wire.get("id") != row["version_id"]
            or wire.get("model_ref") != row["model_ref"]
            or wire.get("version") != row["version_number"]
            or wire.get("prior_version_ref") != row["prior_version_id"]
            or wire.get("company_ref") != row["company_ref"]
            or wire.get("spec_ref") != row["spec_ref"]
            or wire.get("inputs_hash") != row["inputs_hash"]
            or wire.get("body_hash") != row["body_hash"]):
        raise RuntimeError("forecast model SQL projection differs")
    return wire


def model_spec(row: sqlite3.Row) -> dict[str, Any]:
    wire = dict(row)
    anchor = wire.pop("revenue_anchor_json")
    wire["revenue_anchor_concept"] = None if anchor is None else json.loads(anchor)
    for field in ("revenue_drivers", "expense_lines", "forecast_statements",
                  "operating_metrics", "horizon"):
        raw = wire.pop(field + "_json")
        if canonical_json(json.loads(raw)) != raw:
            raise RuntimeError(f"company model spec {field} is not canonical")
        wire[field] = json.loads(raw)
    metadata_raw = wire.pop("metadata_json")
    if metadata_raw is not None:
        metadata = json.loads(metadata_raw)
        if canonical_json(metadata) != metadata_raw or not isinstance(metadata, Mapping):
            raise RuntimeError("company model spec metadata is not canonical")
        wire.update(metadata)
    if wire.get("schema_version") in {"0.3", "0.4"}:
        # This is the exact shape supplied to record_company_model_spec.
        supplied = {
            key: value for key, value in wire.items()
            if key not in {"spec_id", "mission_version_ref", "model_profile_ref",
                           "work_order_ref", "created_at"}
        }
        # SQL names `revenue_anchor_concept`; all remaining supplied keys are
        # the structured model spec, including decided_by/content_hash.
        asserted = supplied.pop("content_hash")
        if content_hash(supplied) != asserted:
            raise RuntimeError("structured company model spec content hash differs")
    return wire


def row_ids(connection: sqlite3.Connection, table: str, column: str) -> list[str]:
    if not table_exists(connection, table):
        return []
    return [str(row[0]) for row in connection.execute(
        f"SELECT {column} FROM {table} ORDER BY {column}")]


def scheduler_bundle(connection: sqlite3.Connection, work_ref: str) -> dict[str, Any] | None:
    row = connection.execute(
        "SELECT * FROM scheduler_work_orders WHERE work_order_id=?", (work_ref,)
    ).fetchone()
    if row is None:
        return None
    work = exact_work(row)
    formals = [exact_formal(item) for item in connection.execute(
        "SELECT * FROM scheduler_formal_results WHERE work_order_id=? "
        "ORDER BY attempt_number,result_record_id", (work_ref,)
    )]
    events = [dict(item) for item in connection.execute(
        "SELECT event_seq,event_id,attempt_number,state,result_envelope_id,"
        "result_envelope_hash,reason,not_before,content_hash,created_at "
        "FROM scheduler_attempt_events WHERE work_order_id=? ORDER BY event_seq",
        (work_ref,),
    )]
    return {
        "work_order_ref": work_ref, "work_order_hash": row["work_order_hash"],
        "created_at": row["created_at"], "purpose": (work.get("metadata") or {}).get("purpose"),
        "stage": ((work.get("metadata") or {}).get("stage")
                  or (work.get("metadata") or {}).get("operation")),
        "requested_capabilities": work.get("requested_capabilities") or [],
        "formals": formals, "events": events,
    }


def scheduler_location(core: sqlite3.Connection, service: sqlite3.Connection,
                       work_ref: str | None) -> dict[str, Any] | None:
    if not work_ref:
        return None
    locations = []
    for name, connection in (("core.sqlite", core), ("scheduler.sqlite", service)):
        bundle = scheduler_bundle(connection, work_ref)
        if bundle is not None:
            locations.append({"database": name, **bundle})
    if len(locations) > 1:
        raise RuntimeError(f"WorkOrder exists in both schedulers: {work_ref}")
    return None if not locations else locations[0]


def budget_bundle(budget: sqlite3.Connection, work_ref: str) -> dict[str, Any]:
    admissions = []
    for row in budget.execute(
        "SELECT * FROM thesis_impact_day_admissions WHERE work_order_ref=? "
        "ORDER BY attempt_number,created_at", (work_ref,),
    ):
        wire = exact_record(row, label="budget admission")
        settlements = []
        for settled in budget.execute(
            "SELECT * FROM thesis_impact_day_settlements WHERE admission_id=? "
            "ORDER BY created_at", (row["admission_id"],),
        ):
            exact_record(settled, label="budget settlement")
            settlements.append({
                "settlement_ref": settled["settlement_id"],
                "actual_micros": settled["actual_micros"],
                "usage_entry_ref": settled["usage_entry_ref"],
                "content_hash": settled["content_hash"],
                "created_at": settled["created_at"],
            })
        admissions.append({
            "admission_ref": row["admission_id"], "attempt_number": row["attempt_number"],
            "phase": row["phase"], "route_decision_ref": row["route_decision_ref"],
            "reserved_micros": row["reserved_micros"], "pool": row["pool"],
            "content_hash": row["content_hash"], "created_at": row["created_at"],
            "settlements": settlements, "record_hash": wire["content_hash"],
        })
    rejections = []
    for row in budget.execute(
        "SELECT * FROM thesis_impact_day_rejections WHERE work_order_ref=? "
        "ORDER BY attempt_number,created_at", (work_ref,),
    ):
        exact_record(row, label="budget rejection")
        rejections.append({
            "rejection_ref": row["rejection_id"], "attempt_number": row["attempt_number"],
            "day": row["day"], "route_decision_ref": row["route_decision_ref"],
            "reserved_micros": row["reserved_micros"],
            "day_committed_micros": row["day_committed_micros"],
            "day_cap_micros": row["day_cap_micros"],
            "content_hash": row["content_hash"], "created_at": row["created_at"],
        })
    return {"admissions": admissions, "rejections": rejections}


def exact_route(router: sqlite3.Connection, route_ref: str, *, work_ref: str,
                work_hash: str, attempt: int) -> dict[str, Any]:
    row = router.execute(
        "SELECT * FROM model_route_decisions WHERE decision_id=?", (route_ref,)
    ).fetchone()
    if row is None:
        raise RuntimeError("model route decision is absent")
    wire = json.loads(row["decision_json"])
    body = dict(wire); asserted = body.pop("content_hash", None)
    if (not isinstance(wire, Mapping) or canonical_json(wire) != row["decision_json"]
            or asserted != row["decision_hash"] or asserted != content_hash(body)
            or wire.get("id") != row["decision_id"]
            or wire.get("work_order_ref") != work_ref
            or wire.get("work_order_hash") != work_hash
            or wire.get("attempt_number") != attempt
            or wire.get("outcome") != "selected"
            or wire.get("selected_profile_version_ref")
                != row["selected_profile_version_ref"]):
        raise RuntimeError("model route decision authority differs")
    return wire


def provider_send_proof(core: sqlite3.Connection, router: sqlite3.Connection,
                        budget: sqlite3.Connection, *, work_ref: str,
                        work_hash: str, formal: Mapping[str, Any]) -> dict[str, Any]:
    budget_evidence = budget_bundle(budget, work_ref)
    invocation_ref = formal.get("invocation_ref")
    if formal.get("terminal_state") != "succeeded" or not isinstance(
            invocation_ref, str) or not invocation_ref.startswith("invocation:"):
        no_send = (
            isinstance(invocation_ref, str)
            and invocation_ref.startswith("invocation:not-started:")
            and bool(budget_evidence["rejections"])
            and not budget_evidence["admissions"]
        )
        return {
            "provider_send_proven": False,
            "actual_cost_settled": False,
            "classification": ("atomic_budget_refusal_no_send" if no_send
                               else "no_successful_provider_response_proven"),
            "budget": budget_evidence,
        }
    row = core.execute(
        "SELECT * FROM model_invocations WHERE invocation_id=?", (invocation_ref,)
    ).fetchone()
    if row is None:
        raise RuntimeError("successful formal result lacks its ModelInvocation")
    invocation = json.loads(row["invocation_json"])
    if (not isinstance(invocation, Mapping)
            or canonical_json(invocation) != row["invocation_json"]
            or invocation.get("id") != invocation_ref
            or invocation.get("invocation_id") != invocation_ref
            or invocation.get("work_order_ref") != work_ref
            or invocation.get("completed_at") in {None, ""}
            or any(invocation.get(field) != row[field] for field in (
                "profile_ref", "provider", "model", "capability", "runtime_ref",
                "actor_ref", "environment_hash", "granularity", "model_family"))):
        raise RuntimeError("ModelInvocation authority differs")
    route_ref = invocation.get("parent_ref")
    if not isinstance(route_ref, str):
        raise RuntimeError("ModelInvocation lacks route decision")
    route = exact_route(router, route_ref, work_ref=work_ref,
                        work_hash=work_hash, attempt=int(formal["attempt_number"]))
    endpoint = route.get("selected_endpoint") or {}
    if (route.get("selected_profile_version_ref") != invocation["profile_ref"]
            or route.get("capability") != invocation["capability"]
            or endpoint.get("provider") != invocation["provider"]
            or endpoint.get("model") != invocation["model"]
            or endpoint.get("family") != invocation["model_family"]
            or endpoint.get("adapter_ref") != invocation["runtime_ref"]):
        raise RuntimeError("ModelInvocation does not match selected route")
    usage_rows = list(core.execute(
        "SELECT * FROM observability_usage_entries WHERE invocation_ref=? "
        "ORDER BY revision_number", (invocation_ref,),
    ))
    if not usage_rows:
        raise RuntimeError("successful ModelInvocation lacks durable usage")
    usage_row = usage_rows[-1]
    usage = exact_record(usage_row, label="model usage")
    if (usage.get("invocation_ref") != invocation_ref
            or usage.get("work_order_ref") != work_ref):
        raise RuntimeError("usage does not bind the invocation and WorkOrder")
    cost_rows = list(core.execute(
        "SELECT * FROM observability_cost_entries WHERE usage_entry_ref=? "
        "ORDER BY revision_number", (usage_row["usage_entry_id"],),
    ))
    if not cost_rows:
        raise RuntimeError("successful ModelInvocation lacks durable cost status")
    cost_row = cost_rows[-1]
    cost = exact_record(cost_row, label="model cost")
    matching_admissions = [item for item in budget_evidence["admissions"]
                           if item["attempt_number"] == formal["attempt_number"]
                           and item["route_decision_ref"] == route_ref]
    if len(matching_admissions) != 1:
        raise RuntimeError("provider result lacks one exact budget admission")
    settlements = matching_admissions[0]["settlements"]
    actual_settled = (
        cost.get("cost_status") == "actual"
        and cost.get("amount_micros") is not None
        and len(settlements) == 1
        and settlements[0]["usage_entry_ref"] == usage_row["usage_entry_id"]
        and settlements[0]["actual_micros"] == cost["amount_micros"]
    )
    estimated_reserved = (
        cost.get("cost_status") == "estimated"
        and cost.get("amount_micros") is not None
        and not settlements
        and cost["amount_micros"] <= matching_admissions[0]["reserved_micros"]
    )
    if not actual_settled and not estimated_reserved:
        raise RuntimeError("provider result accounting is neither settled actual nor held estimate")
    return {
        "provider_send_proven": True,
        "actual_cost_settled": actual_settled,
        "classification": ("provider_response_actual_cost_settled" if actual_settled
                           else "provider_response_estimated_cost_reserved"),
        "invocation": {
            "invocation_ref": invocation_ref, "provider": invocation["provider"],
            "model": invocation["model"], "model_family": invocation["model_family"],
            "profile_ref": invocation["profile_ref"], "route_decision_ref": route_ref,
            "completed_at": invocation["completed_at"],
        },
        "usage": {"usage_entry_ref": usage_row["usage_entry_id"],
                  "content_hash": usage_row["content_hash"],
                  "measurement_status": usage.get("measurement_status"),
                  "metering_source": usage.get("metering_source")},
        "cost": {"cost_entry_ref": cost_row["cost_entry_id"],
                 "content_hash": cost_row["content_hash"],
                 "cost_status": cost.get("cost_status"),
                 "amount_micros": cost.get("amount_micros")},
        "budget": budget_evidence,
    }


def full_inventory(connections: Mapping[str, sqlite3.Connection]) -> dict[str, list[str]]:
    core = connections["core"]
    return {
        "admissions": row_ids(core, "mission_document_research_admissions", "admission_id"),
        "starts": row_ids(core, "mission_document_research_starts", "start_id"),
        "observations": row_ids(core, "mission_document_research_observations", "observation_id"),
        "outcomes": row_ids(core, "mission_document_research_outcomes", "outcome_id"),
        "promotions": row_ids(core, "mission_document_research_promotions", "promotion_id"),
        "recovery_links": row_ids(core, "mission_document_research_recovery_links", "recovery_link_id"),
        "core_work_orders": row_ids(core, "scheduler_work_orders", "work_order_id"),
        "core_formals": row_ids(core, "scheduler_formal_results", "result_record_id"),
        "service_work_orders": row_ids(connections["service"], "scheduler_work_orders", "work_order_id"),
        "service_formals": row_ids(connections["service"], "scheduler_formal_results", "result_record_id"),
        "model_versions": row_ids(core, "forecast_model_versions", "version_id"),
        "model_specs": row_ids(core, "coverage_mission_company_model_specs", "spec_id"),
    }


def open_authorities(state: Path) -> dict[str, sqlite3.Connection]:
    return {
        "core": ro(state / "core.sqlite"),
        "service": ro(state / "scheduler.sqlite"),
        "budget": ro(state / "thesis-impact-budget.sqlite"),
        "router": ro(state / "model-router.sqlite"),
    }


def close_authorities(connections: Mapping[str, sqlite3.Connection]) -> None:
    for connection in connections.values():
        connection.rollback(); connection.close()


def holds_snapshot(state: Path) -> dict[str, Any]:
    path = state / "mission-document-research-runs/holds.json"
    if not path.exists():
        return {"present": False}
    wire, digest = stable_json(path, label="mission document holds")
    body = dict(wire); asserted = body.pop("content_hash", None)
    if asserted != content_hash(body):
        raise RuntimeError("mission document holds content hash differs")
    return {"present": True, "path": str(path), "sha256": digest,
            "content_hash": asserted, "holds": wire.get("holds") or {}}


def run_files(state: Path, admission_ref: str) -> list[dict[str, Any]]:
    root = state / "mission-document-research-runs"
    result = []
    for ticket_path in root.glob("*/ticket.json"):
        try:
            ticket, ticket_sha = stable_json(ticket_path, label="mission document ticket")
        except (OSError, RuntimeError):
            continue
        if ticket.get("admission_ref") != admission_ref:
            continue
        summary_path = ticket_path.with_name("summary.json")
        summary = summary_sha = None
        if summary_path.exists():
            summary, summary_sha = stable_json(summary_path, label="mission document summary")
        markers = []
        for marker_path in ticket_path.parent.glob("controlled-reentry-*.json"):
            marker, marker_sha = stable_json(marker_path, label="controlled reentry marker")
            markers.append({"path": str(marker_path), "sha256": marker_sha, "record": marker})
        result.append({
            "ticket_ref": ticket.get("id"), "ticket_path": str(ticket_path),
            "ticket_sha256": ticket_sha, "started_at": ticket.get("started_at"),
            "completed_at": ticket.get("completed_at"), "status": ticket.get("status"),
            "exit_code": ticket.get("exit_code"),
            "summary": None if summary is None else {
                "path": str(summary_path), "sha256": summary_sha,
                "created_at": summary.get("created_at"), "status": summary.get("status"),
                "error": summary.get("error"), "content_hash": summary.get("content_hash"),
            },
            "controlled_reentry_markers": markers,
        })
    return sorted(result, key=lambda item: (str(item["started_at"]), str(item["ticket_ref"])))


def directed_lifecycle(connections: Mapping[str, sqlite3.Connection], state: Path,
                       admission_ref: str, cutoff: datetime,
                       baseline_ids: Mapping[str, Sequence[str]]) -> dict[str, Any]:
    core, budget, router = connections["core"], connections["budget"], connections["router"]
    row = core.execute(
        "SELECT * FROM mission_document_research_admissions WHERE admission_id=?",
        (admission_ref,),
    ).fetchone()
    if row is None:
        return {"admission_ref": admission_ref, "classification": "admission_absent"}
    admission = exact_record(row, label="mission document admission")
    works = []
    for work_row in core.execute(
        "SELECT * FROM scheduler_work_orders ORDER BY created_at,work_order_id"
    ):
        work = exact_work(work_row)
        if (work.get("metadata") or {}).get(
                "mission_document_research_admission_ref") != admission_ref:
            continue
        bundle = scheduler_bundle(core, work_row["work_order_id"])
        assert bundle is not None
        send_proofs = [provider_send_proof(
            core, router, budget, work_ref=work_row["work_order_id"],
            work_hash=work_row["work_order_hash"], formal=formal,
        ) for formal in bundle["formals"]]
        works.append({
            **bundle,
            "fresh_work_since_cutoff": parse_time(work_row["created_at"], "work created_at") >= cutoff,
            "new_since_baseline": work_row["work_order_id"] not in baseline_ids.get("core_work_orders", []),
            "send_proofs": send_proofs,
        })
    starts = []
    for item in core.execute(
        "SELECT * FROM mission_document_research_starts WHERE admission_ref=? ORDER BY created_at",
        (admission_ref,),
    ):
        wire = exact_record(item, label="mission document start")
        starts.append({"start_ref": item["start_id"], "content_hash": item["content_hash"],
                       "created_at": item["created_at"], "run_id": item["run_id"],
                       "fresh_since_cutoff": parse_time(item["created_at"], "start created_at") >= cutoff,
                       "new_since_baseline": item["start_id"] not in baseline_ids.get("starts", []),
                       "record_hash": wire["content_hash"]})
    observations = []
    for item in core.execute(
        "SELECT * FROM mission_document_research_observations WHERE admission_ref=? "
        "ORDER BY created_at,observation_id", (admission_ref,),
    ):
        wire = exact_record(item, label="mission document observation")
        observations.append({
            "observation_ref": item["observation_id"], "outcome": item["outcome"],
            "work_order_ref": wire.get("work_order_ref"), "stage": wire.get("stage"),
            "recovery": wire.get("recovery"), "created_at": item["created_at"],
            "content_hash": item["content_hash"],
            "fresh_since_cutoff": parse_time(item["created_at"], "observation created_at") >= cutoff,
            "new_since_baseline": item["observation_id"] not in baseline_ids.get("observations", []),
        })
    links = []
    for item in core.execute(
        "SELECT * FROM mission_document_research_recovery_links WHERE admission_ref=? "
        "ORDER BY stage_ordinal,recovery_number", (admission_ref,),
    ):
        wire = exact_record(item, label="mission document recovery link")
        links.append({
            "recovery_link_ref": item["recovery_link_id"],
            "failed_work_order_ref": item["failed_work_order_ref"],
            "recovery_work_order_ref": item["recovery_work_order_ref"],
            "stage_ordinal": item["stage_ordinal"], "recovery_number": item["recovery_number"],
            "created_at": item["created_at"], "content_hash": item["content_hash"],
            "fresh_since_cutoff": parse_time(item["created_at"], "link created_at") >= cutoff,
            "new_since_baseline": item["recovery_link_id"] not in baseline_ids.get("recovery_links", []),
            "record_hash": wire["content_hash"],
        })
    outcome_row = core.execute(
        "SELECT * FROM mission_document_research_outcomes WHERE admission_ref=?", (admission_ref,)
    ).fetchone()
    promotion_row = core.execute(
        "SELECT * FROM mission_document_research_promotions WHERE admission_ref=?", (admission_ref,)
    ).fetchone()
    outcome = None
    if outcome_row is not None:
        wire = exact_record(outcome_row, label="mission document outcome")
        outcome = {"outcome_ref": outcome_row["outcome_id"], "content_hash": outcome_row["content_hash"],
                   "created_at": outcome_row["created_at"], "record": wire,
                   "fresh_since_cutoff": parse_time(outcome_row["created_at"], "outcome created_at") >= cutoff,
                   "new_since_baseline": outcome_row["outcome_id"] not in baseline_ids.get("outcomes", [])}
    promotion = None
    if promotion_row is not None:
        wire = exact_record(promotion_row, label="mission document promotion")
        promotion = {"promotion_ref": promotion_row["promotion_id"],
                     "content_hash": promotion_row["content_hash"],
                     "created_at": promotion_row["created_at"], "record": wire,
                     "fresh_since_cutoff": parse_time(promotion_row["created_at"], "promotion created_at") >= cutoff,
                     "new_since_baseline": promotion_row["promotion_id"] not in baseline_ids.get("promotions", [])}
    tickets = run_files(state, admission_ref)
    fresh_tickets = [item for item in tickets if item.get("started_at")
                     and parse_time(item["started_at"], "ticket started_at") >= cutoff]
    controlled_reentry_markers = [
        marker for ticket in tickets
        for marker in ticket["controlled_reentry_markers"]
    ]
    latest_recovery = next((item.get("recovery") for item in reversed(observations)
                            if item.get("outcome") == "recovery_required"), None)
    now = datetime.now(timezone.utc)
    fresh_links = [item for item in links if item["fresh_since_cutoff"]]
    if promotion is not None:
        classification = "canonical_promotion_observed"
    elif outcome is not None:
        classification = "candidate_outcome_waiting_for_promotion"
    elif fresh_links or (fresh_tickets and controlled_reentry_markers):
        classification = "legacy_or_current_recovery_reentered_and_advancing"
    elif fresh_tickets:
        classification = "fresh_ticket_without_controlled_reentry_proof"
    elif isinstance(latest_recovery, Mapping) and latest_recovery.get("retry_at"):
        due = parse_time(latest_recovery["retry_at"], "recovery retry_at")
        classification = ("utc_budget_wait_not_due" if now < due
                          else "utc_budget_reentry_due_not_yet_observed")
    elif isinstance(latest_recovery, Mapping) and latest_recovery.get("reason") == "send_state_unproved":
        classification = "unknown_send_state_terminal_barrier"
    elif works:
        classification = "execution_incomplete"
    else:
        classification = "admitted_without_work"
    return {
        "admission_ref": admission_ref, "admission_hash": row["content_hash"],
        "admitted_at": row["created_at"], "company_ref": row["company_ref"],
        "plan_ref": row["plan_ref"], "inquiry_ref": row["inquiry_ref"],
        "question": admission.get("question"), "wants": admission.get("wants"),
        "document_ref": admission.get("document_ref"),
        "classification": classification, "starts": starts,
        "observations": observations, "recovery_links": links,
        "tickets": tickets, "fresh_ticket_count": len(fresh_tickets),
        "controlled_reentry_marker_count": len(controlled_reentry_markers),
        "works": works, "outcome": outcome, "promotion": promotion,
        "provider_response_count": sum(
            proof["provider_send_proven"] for work in works for proof in work["send_proofs"]),
        "actual_cost_settled_send_count": sum(
            proof["actual_cost_settled"] for work in works for proof in work["send_proofs"]),
        "atomic_budget_refusal_no_send_count": sum(
            proof["classification"] == "atomic_budget_refusal_no_send"
            for work in works for proof in work["send_proofs"]),
    }


def model_evidence(connections: Mapping[str, sqlite3.Connection], cutoff: datetime,
                   baseline_ids: Mapping[str, Sequence[str]]) -> dict[str, Any]:
    core, service = connections["core"], connections["service"]
    specs = []
    for row in core.execute(
        "SELECT * FROM coverage_mission_company_model_specs ORDER BY created_at,spec_id"
    ):
        wire = model_spec(row)
        if parse_time(row["created_at"], "spec created_at") < cutoff:
            continue
        specs.append({
            "spec_ref": row["spec_id"], "company_ref": row["company_ref"],
            "schema_version": wire.get("schema_version"), "content_hash": row["content_hash"],
            "created_at": row["created_at"], "work_order_ref": row["work_order_ref"],
            "new_since_baseline": row["spec_id"] not in baseline_ids.get("model_specs", []),
            "formal_authority": scheduler_location(core, service, row["work_order_ref"]),
        })
    models = []
    for row in core.execute(
        "SELECT * FROM forecast_model_versions ORDER BY created_at,version_id"
    ):
        wire = exact_model(row)
        if parse_time(row["created_at"], "model created_at") < cutoff:
            continue
        annual = core.execute(
            "SELECT * FROM forecast_model_annual_projections WHERE model_version_id=?",
            (row["version_id"],),
        ).fetchone()
        annual_record = None
        if annual is not None:
            annual_wire = exact_record(annual, label="annual projection")
            annual_record = {"content_hash": annual["content_hash"],
                             "schema_version": annual_wire.get("schema_version"),
                             "created_at": annual["created_at"]}
        models.append({
            "model_version_ref": row["version_id"], "model_ref": row["model_ref"],
            "version": row["version_number"], "prior_version_ref": row["prior_version_id"],
            "company_ref": row["company_ref"], "spec_ref": row["spec_ref"],
            "schema_version": wire.get("schema_version"), "content_hash": row["content_hash"],
            "created_at": row["created_at"],
            "new_since_baseline": row["version_id"] not in baseline_ids.get("model_versions", []),
            "annual_projection": annual_record,
        })
    return {"new_specs_since_cutoff": specs, "new_models_since_cutoff": models}


def exact_research_plan(row: Mapping[str, Any]) -> dict[str, Any]:
    """Replay record_research_plan's archived wire and separately derived ID.

    plan_json stores the model's exact hashed plan. The database ID is derived
    from mission/state and is deliberately not injected into that wire.
    """
    raw = row["plan_json"]
    wire = json.loads(raw)
    if not isinstance(wire, Mapping) or canonical_json(wire) != raw:
        raise RuntimeError("formal research plan JSON is not canonical")
    body = dict(wire)
    asserted = body.pop("content_hash", None)
    identity = {"mission_version_ref": wire.get("mission_version_ref"),
                "state_hash": wire.get("state_hash")}
    expected_id = "mission-research-plan:" + content_hash(identity)[:32]
    if (asserted != row["content_hash"] or asserted != content_hash(body)
            or expected_id != row["plan_id"]
            or wire.get("schema_version") != PLANNER_SCHEMA_VERSION
            or wire.get("task_ref") != PLANNER_TASK_REF
            or not isinstance(wire.get("inquiries"), list)
            or identity["mission_version_ref"] != row["mission_version_ref"]
            or identity["state_hash"] != row["state_hash"]
            or wire.get("assessment") != row["assessment"]
            or canonical_json(wire.get("directives")) != row["directives_json"]
            or canonical_json(wire.get("inquiries")) != row["inquiries_json"]
            or canonical_json(wire.get("sufficiency")) != (row["sufficiency_json"] or "[]")):
        raise RuntimeError("formal research plan authority differs")
    return dict(wire)


def stored_plan_for_work(core: sqlite3.Connection,
                         work_order_ref: str) -> tuple[Mapping[str, Any] | None,
                                                       dict[str, Any] | None]:
    rows = core.execute(
        "SELECT * FROM coverage_mission_research_plans WHERE work_order_ref=? "
        "ORDER BY created_at,plan_id", (work_order_ref,),
    ).fetchall()
    if len(rows) > 1:
        raise RuntimeError("planner WorkOrder names multiple stored plans")
    if not rows:
        return None, None
    return rows[0], exact_research_plan(rows[0])


def planner_classification(plan: Mapping[str, Any] | None,
                           formals: Sequence[Mapping[str, Any]]) -> str:
    terminal = [item.get("terminal_state") for item in formals]
    if plan is not None:
        if "succeeded" in terminal:
            return "stored_plan_with_succeeded_formal"
        return "stored_plan_without_succeeded_formal"
    if "failed" in terminal:
        return "planner_terminal_failed"
    return "planner_work_running_or_queued"


def claim_planner_work(seen: set[str], work_order_ref: str) -> None:
    if work_order_ref in seen:
        raise RuntimeError("planner WorkOrder exists in both schedulers")
    seen.add(work_order_ref)


def planner_evidence(connections: Mapping[str, sqlite3.Connection],
                     cutoff: datetime) -> dict[str, Any]:
    """Inspect stored planner decisions and their separate Scheduler authority."""

    core = connections["core"]
    works = []
    seen = set()
    for database, scheduler in (("core.sqlite", core),
                                ("scheduler.sqlite", connections["service"])):
        for row in scheduler.execute(
            "SELECT * FROM scheduler_work_orders WHERE created_at>=? "
            "ORDER BY created_at,work_order_id", (cutoff.isoformat(),),
        ):
            work = exact_work(row)
            if (work.get("metadata") or {}).get("purpose") != "plan":
                continue
            claim_planner_work(seen, row["work_order_id"])
            bundle = scheduler_bundle(scheduler, row["work_order_id"])
            assert bundle is not None
            plan_row, wire = stored_plan_for_work(core, row["work_order_id"])
            plan = None
            if plan_row is not None:
                assert wire is not None
                plan = {
                    "plan_ref": plan_row["plan_id"],
                    "content_hash": plan_row["content_hash"],
                    "state_hash": plan_row["state_hash"],
                    "mission_version_ref": plan_row["mission_version_ref"],
                    "created_at": plan_row["created_at"],
                    "directive_count": len(wire.get("directives") or []),
                    "inquiry_count": len(wire.get("inquiries") or []),
                }
            succeeded_formals = [item for item in bundle["formals"]
                                 if item["terminal_state"] == "succeeded"]
            classification = planner_classification(plan, bundle["formals"])
            works.append({
                "scheduler_database": database, **bundle,
                "classification": classification, "stored_plan": plan,
                "succeeded_formal_refs": [item["result_record_ref"]
                                          for item in succeeded_formals],
                "budget": budget_bundle(connections["budget"], row["work_order_id"]),
                "note": (
                    "The stored verified plan and Scheduler formal are separate authorities; "
                    "it is not counted as a directed-document paid send."
                ),
            })
    return {"work_count": len(works), "works": works}


def data_versions(connections: Mapping[str, sqlite3.Connection]) -> dict[str, int]:
    return {name: int(connection.execute("PRAGMA data_version").fetchone()[0])
            for name, connection in connections.items()}


def baseline(args: argparse.Namespace) -> None:
    expected_source = source_commit(args.source_commit)
    if args.output_dir.exists() or args.output_dir.is_symlink():
        raise RuntimeError("output directory already exists")
    connections = open_authorities(args.state_dir)
    versions = data_versions(connections)
    inventory = full_inventory(connections)
    tracked = [directed_lifecycle(
        connections, args.state_dir, ref, datetime.max.replace(tzinfo=timezone.utc), inventory
    ) for ref in args.tracked_admission_ref]
    final_versions = data_versions(connections)
    close_authorities(connections)
    report = {
        "schema_version": "dalton-r15-product-audit-baseline-0.1",
        "status": "read_only_reference_snapshot",
        "snapshot_at": datetime.now(timezone.utc).isoformat(),
        "mode": "read_only_no_calls_no_writes",
        "target_source_commit": expected_source,
        "reviewed_schema_base": R15_REVIEWED_BASE,
        "state_dir": str(args.state_dir),
        "sqlite_data_version_start": versions,
        "sqlite_data_version_end": final_versions,
        "inventory": inventory,
        "holds": holds_snapshot(args.state_dir),
        "tracked_admissions": tracked,
        "interpretation": "This freezes row identities for comparison; the deployment cutoff remains authoritative for postdeploy timing.",
    }
    write_json(args.output_dir / "baseline.json", report)
    receipt = {
        "schema_version": "dalton-r15-product-audit-baseline-receipt-0.1",
        "status": "read_only_reference_snapshot",
        "source_commit": expected_source,
        "baseline_sha256": sha(args.output_dir / "baseline.json"),
        "tool_sha256": sha(Path(__file__)),
    }
    write_json(args.output_dir / "receipt.json", receipt)
    print(json.dumps({"output_dir": str(args.output_dir), **receipt,
                      "receipt_sha256": sha(args.output_dir / "receipt.json")}, indent=2))


def validate_deployment(args: argparse.Namespace, cutoff: datetime,
                        expected_source: str) -> dict[str, Any]:
    receipt, receipt_sha = stable_json(args.deploy_receipt, label="deployment receipt")
    if receipt_sha != args.deploy_receipt_sha256:
        raise RuntimeError("deployment receipt SHA-256 differs")
    if receipt.get("source_commit") != expected_source:
        raise RuntimeError("deployment receipt source commit differs")
    if receipt.get("schema_version") != EXPECTED_DEPLOYMENT_SCHEMA:
        raise RuntimeError("deployment receipt schema differs")
    if receipt.get("status") != EXPECTED_INSTALLER_STATUS:
        raise RuntimeError("deployment receipt is not a successful installer result")
    if receipt.get("exit_code") != 0:
        raise RuntimeError("deployment receipt installer exit code is not zero")
    started = receipt.get("started_at")
    finished = receipt.get("finished_at")
    if not isinstance(started, str) or parse_time(
            started, "deployment start") != cutoff:
        raise RuntimeError("deployment cutoff is not the exact receipt started_at")
    if (not isinstance(finished, str)
            or parse_time(finished, "deployment finish") < cutoff):
        raise RuntimeError("deployment receipt finish precedes its start")
    process = subprocess.run(
        ["ps", "-p", str(args.controller_pid), "-o", "lstart=", "-o", "command="],
        text=True, capture_output=True, check=True,
    ).stdout.strip()
    if "daltond" not in process or "--config" not in process:
        raise RuntimeError("controller PID is not a Dalton controller")
    return {"receipt": str(args.deploy_receipt), "receipt_sha256": receipt_sha,
            "status": receipt["status"], "exit_code": receipt["exit_code"],
            "started_at": started, "finished_at": finished,
            "controller_pid": args.controller_pid,
            "controller_process_sha256": sha_bytes(process.encode()),
            "controller_process_identity_scope": (
                "PID command identifies a Dalton controller only; the deployed release "
                "identity is established by the separate release-health evidence."),
            }


def postdeploy(args: argparse.Namespace) -> None:
    expected_source = source_commit(args.source_commit)
    if args.output_dir.exists() or args.output_dir.is_symlink():
        raise RuntimeError("output directory already exists")
    cutoff = parse_time(args.deployed_at, "deployed_at")
    deployment = validate_deployment(args, cutoff, expected_source)
    baseline_wire, baseline_sha = stable_json(args.baseline, label="R15 audit baseline")
    if baseline_sha != args.baseline_sha256:
        raise RuntimeError("baseline SHA-256 differs")
    if (baseline_wire.get("schema_version") != "dalton-r15-product-audit-baseline-0.1"
            or baseline_wire.get("target_source_commit") != expected_source
            or baseline_wire.get("reviewed_schema_base") != R15_REVIEWED_BASE):
        raise RuntimeError("baseline does not bind the deployed source and reviewed schema")
    baseline_ids = baseline_wire.get("inventory")
    if not isinstance(baseline_ids, Mapping):
        raise RuntimeError("baseline inventory is invalid")
    connections = open_authorities(args.state_dir)
    versions = data_versions(connections)
    directed = [directed_lifecycle(
        connections, args.state_dir, ref, cutoff, baseline_ids
    ) for ref in args.tracked_admission_ref]
    # Include admissions first created after deployment without losing the two
    # explicitly tracked pre-cutoff admissions.
    tracked_set = set(args.tracked_admission_ref)
    for row in connections["core"].execute(
        "SELECT admission_id FROM mission_document_research_admissions "
        "WHERE created_at>=? ORDER BY created_at,admission_id", (cutoff.isoformat(),),
    ):
        if row["admission_id"] not in tracked_set:
            directed.append(directed_lifecycle(
                connections, args.state_dir, row["admission_id"], cutoff, baseline_ids))
    models = model_evidence(connections, cutoff, baseline_ids)
    planner = planner_evidence(connections, cutoff)
    final_versions = data_versions(connections)
    close_authorities(connections)
    fresh_outcomes = sum(bool(item.get("outcome") and item["outcome"]["fresh_since_cutoff"])
                         for item in directed)
    fresh_promotions = sum(bool(item.get("promotion") and item["promotion"]["fresh_since_cutoff"])
                           for item in directed)
    report = {
        "schema_version": "dalton-r15-postdeploy-product-audit-0.1",
        "status": "read_only_product_snapshot_complete",
        "snapshot_at": datetime.now(timezone.utc).isoformat(),
        "mode": "read_only_no_calls_no_writes",
        "release": {"source_commit": expected_source, "deployed_at": cutoff.isoformat(),
                    "deployment": deployment, "baseline": str(args.baseline),
                    "baseline_sha256": baseline_sha},
        "snapshot_authority": {"sqlite": "mode=ro, query_only, explicit read transactions",
                               "data_version_start": versions,
                               "data_version_end": final_versions},
        "directed_document_research": {
            "tracked_pre_cutoff_admissions": list(args.tracked_admission_ref),
            "admissions": directed,
            "provider_response_count": sum(item.get("provider_response_count", 0) for item in directed),
            "actual_cost_settled_send_count": sum(item.get("actual_cost_settled_send_count", 0) for item in directed),
            "atomic_budget_refusal_no_send_count": sum(item.get("atomic_budget_refusal_no_send_count", 0) for item in directed),
            "fresh_outcome_count": fresh_outcomes,
            "fresh_promotion_count": fresh_promotions,
            "interpretation": (
                "A settlement alone never proves a send. Provider response counts require exact "
                "successful formal, ModelInvocation, route, provider usage, cost and budget authority. "
                "Atomic day-budget rejection is a no-send wait until its exact UTC reset."
            ),
        },
        "company_models": models,
        "planner": planner,
        "holds": holds_snapshot(args.state_dir),
        "scope": (
            "Normal-pipeline evidence only. No lane, model, provider, connector, network, "
            "dispatch, or live-state mutation was invoked."
        ),
    }
    write_json(args.output_dir / "audit.json", report)
    lines = [
        "# R15 postdeploy product audit", "",
        f"Snapshot: `{report['snapshot_at']}`  ",
        f"Deployment cutoff: `{cutoff.isoformat()}`  ",
        f"Tracked directed admissions: **{len(directed)}**.  ",
        f"Provider responses proven: **{report['directed_document_research']['provider_response_count']}**.  ",
        f"Actually settled-cost sends: **{report['directed_document_research']['actual_cost_settled_send_count']}**.  ",
        f"Atomic budget refusals proven no-send: **{report['directed_document_research']['atomic_budget_refusal_no_send_count']}**.  ",
        f"Fresh outcomes/promotions: **{fresh_outcomes}/{fresh_promotions}**.  ",
        f"New model specs/versions: **{len(models['new_specs_since_cutoff'])}/{len(models['new_models_since_cutoff'])}**.",
        f"Postdeploy planner Works: **{planner['work_count']}** (Scheduler location recorded per Work).",
        "", "Admission, budget wait, provider response, candidate outcome and canonical promotion "
        "remain separate states. Old DXC/EPAM admissions stay in scope even though their admission "
        "timestamps precede R15; only transitions at or after the deployment cutoff are fresh.",
    ]
    write = ("\n".join(lines) + "\n").encode()
    create(args.output_dir / "report.md", write)
    receipt = {
        "schema_version": "dalton-r15-postdeploy-product-audit-receipt-0.1",
        "status": "read_only_product_snapshot_complete", "source_commit": expected_source,
        "deployment_receipt_sha256": args.deploy_receipt_sha256,
        "baseline_sha256": baseline_sha,
        "audit_sha256": sha(args.output_dir / "audit.json"),
        "report_sha256": sha(args.output_dir / "report.md"),
        "tool_sha256": sha(Path(__file__)),
    }
    write_json(args.output_dir / "receipt.json", receipt)
    print(json.dumps({"output_dir": str(args.output_dir), **receipt,
                      "receipt_sha256": sha(args.output_dir / "receipt.json")}, indent=2))


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    sub = result.add_subparsers(dest="mode", required=True)
    for name in ("baseline", "postdeploy"):
        command = sub.add_parser(name)
        command.add_argument("--source-commit", required=True)
        command.add_argument("--state-dir", type=Path, default=DEFAULT_STATE)
        command.add_argument("--output-dir", type=Path, required=True)
        command.add_argument("--tracked-admission-ref", action="append", default=[])
    post = sub.choices["postdeploy"]
    post.add_argument(
        "--deployed-at", required=True,
        help="exact started_at timestamp from the deployment receipt",
    )
    post.add_argument("--deploy-receipt", required=True, type=Path)
    post.add_argument("--deploy-receipt-sha256", required=True)
    post.add_argument("--controller-pid", required=True, type=int)
    post.add_argument("--baseline", required=True, type=Path)
    post.add_argument("--baseline-sha256", required=True)
    return result


def main() -> None:
    os.umask(0o077)
    args = parser().parse_args()
    if not args.tracked_admission_ref:
        args.tracked_admission_ref = list(DEFAULT_TRACKED)
    if len(set(args.tracked_admission_ref)) != len(args.tracked_admission_ref):
        raise RuntimeError("tracked admission refs must be unique")
    if args.mode == "baseline":
        baseline(args)
    else:
        postdeploy(args)


if __name__ == "__main__":
    main()
