"""Read-only resolution of connector/source/artifact authority.

The connector runner deliberately keeps the source envelope, raw artifact,
scheduler result and research checkpoint in different authorities.  This
module is the narrow read-only join between them.  It never accepts a caller
supplied hash as proof: a reference is only used to locate an immutable row,
and every returned hash is recomputed from that row and its parent chain.

The resolver is intentionally a *reader*.  It does not run schema migration,
open a transaction, register an idempotency key, or write a projection.  In a
deployed reader process the supplied connections should be opened with
SQLite's ``mode=ro`` URI (or be immutable snapshots).
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from .connector import source_envelope_content_hash
from .connector_inventory import load_packaged_connector_inventory
from .connector_runner import (
    validate_adapter_transport_observation,
    validate_connector_adapter_request,
    validate_connector_runner_request,
    validate_connector_runner_response,
)
from .contracts import ExecutionInvocation, ExecutionKind, ResultEnvelope, WorkOrder
from .research_context import (
    validate_compiled_connector_plan,
    validate_compiled_connector_step,
    validate_context_pack,
    validate_runner_request_plan_binding,
)
from .research_coordinator import (
    validate_connector_completion_receipt,
    validate_research_checkpoint,
)
from .sec_public_adapter import SecPublicAdapterError, normalize_sec_submissions
from .store import canonical_json, content_hash


SCHEMA_VERSION = "0.1"
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_SOURCE_STATUS = {"complete", "empty", "partial", "error"}
_COMPLETENESS = {"enumerated", "ranked", "partial", "unknown"}


class AuthorityResolutionError(ValueError):
    """A row or cross-authority binding is malformed or unavailable."""


class AuthorityResolutionConflict(AuthorityResolutionError):
    """An immutable authority row has been tampered with or rebound."""


def _text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise AuthorityResolutionError(f"{name} must be a non-empty string")
    return value


def _hash(value: Any, name: str) -> str:
    value = _text(value, name)
    if _HASH_RE.fullmatch(value) is None:
        raise AuthorityResolutionError(f"{name} must be lowercase SHA-256 hex")
    return value


def _timestamp(value: Any, name: str, *, nullable: bool = False) -> str | None:
    if value is None and nullable:
        return None
    value = _text(value, name)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise AuthorityResolutionError(f"{name} must be RFC3339") from exc
    if parsed.tzinfo is None:
        raise AuthorityResolutionError(f"{name} must include a timezone")
    return parsed.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _json_bytes(raw: bytes) -> Any:
    if not isinstance(raw, bytes):
        raise AuthorityResolutionError("artifact reader must return bytes")

    def reject_duplicate(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise AuthorityResolutionConflict(
                    "raw artifact JSON contains duplicate object keys"
                )
            result[key] = value
        return result

    def reject_constant(value: str) -> Any:
        raise AuthorityResolutionConflict(f"raw artifact contains non-standard JSON number: {value}")

    try:
        return json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=reject_duplicate,
            parse_constant=reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AuthorityResolutionConflict("raw artifact is not strict UTF-8 JSON") from exc


def _canonical_row(row: sqlite3.Row, *, json_column: str, id_column: str, ref: str, table: str) -> dict[str, Any]:
    if row is None:
        raise AuthorityResolutionError(f"{table} record not found: {ref}")
    try:
        wire = json.loads(row[json_column])
    except (TypeError, json.JSONDecodeError) as exc:
        raise AuthorityResolutionConflict(f"{table} record_json is invalid") from exc
    if not isinstance(wire, dict):
        raise AuthorityResolutionConflict(f"{table} record_json is not an object")
    if wire.get("id") != ref and wire.get(id_column) != ref:
        raise AuthorityResolutionConflict(f"{table} row identity does not match {ref}")
    declared = row["content_hash"]
    if declared != wire.get("content_hash") or declared != content_hash(
        {key: value for key, value in wire.items() if key != "content_hash"}
    ):
        raise AuthorityResolutionConflict(f"{table} content hash is not canonical")
    return wire


def _record_ref_hash(ref: str, wire: Mapping[str, Any]) -> dict[str, str]:
    return {"ref": ref, "hash": _hash(wire["content_hash"], f"{ref}.content_hash")}


def _schema_matches(value: Any, schema: Mapping[str, Any], name: str = "value") -> None:
    """Small closed-schema evaluator for the inventory's value schemas."""

    if not isinstance(schema, Mapping):
        raise AuthorityResolutionError(f"{name} schema is not an object")
    if "enum" in schema and value not in schema["enum"]:
        raise AuthorityResolutionConflict(f"{name} is outside the frozen enum")
    expected = schema.get("type")
    types = expected if isinstance(expected, list) else [expected]
    if expected is not None:
        ok = False
        for kind in types:
            if kind == "null" and value is None:
                ok = True
            elif kind == "object" and isinstance(value, dict):
                ok = True
            elif kind == "array" and isinstance(value, list):
                ok = True
            elif kind == "string" and isinstance(value, str):
                ok = True
            elif kind == "integer" and isinstance(value, int) and not isinstance(value, bool):
                ok = True
            elif kind == "number" and isinstance(value, (int, float)) and not isinstance(value, bool):
                ok = True
            elif kind == "boolean" and isinstance(value, bool):
                ok = True
        if not ok:
            raise AuthorityResolutionConflict(f"{name} does not match schema type")
    if isinstance(value, str):
        if len(value) < int(schema.get("minLength", 0)):
            raise AuthorityResolutionConflict(f"{name} is shorter than the schema minimum")
        if "pattern" in schema and re.search(schema["pattern"], value) is None:
            raise AuthorityResolutionConflict(f"{name} does not match the schema pattern")
    if isinstance(value, int) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            raise AuthorityResolutionConflict(f"{name} is below the schema minimum")
    if isinstance(value, list):
        if len(value) < int(schema.get("minItems", 0)):
            raise AuthorityResolutionConflict(f"{name} has too few items")
        if schema.get("uniqueItems"):
            encoded = [canonical_json(item) for item in value]
            if len(encoded) != len(set(encoded)):
                raise AuthorityResolutionConflict(f"{name} contains duplicate items")
        item_schema = schema.get("items")
        if item_schema is not None:
            for index, item in enumerate(value):
                _schema_matches(item, item_schema, f"{name}[{index}]")
    if isinstance(value, dict):
        required = set(schema.get("required", ()))
        missing = required - set(value)
        if missing:
            raise AuthorityResolutionConflict(f"{name} is missing schema fields: {sorted(missing)}")
        properties = schema.get("properties", {})
        if schema.get("additionalProperties") is False:
            unknown = set(value) - set(properties)
            if unknown:
                raise AuthorityResolutionConflict(f"{name} contains unknown schema fields: {sorted(unknown)}")
        for key, child in value.items():
            if key in properties:
                _schema_matches(child, properties[key], f"{name}.{key}")


def _inventory_output_schema(profile: Mapping[str, Any], operation: str) -> Mapping[str, Any]:
    connector_ref = profile.get("connector_ref")
    inventory = load_packaged_connector_inventory()
    template = next(
        (value for value in inventory["templates"].values() if value.get("connector_ref") == connector_ref),
        None,
    )
    if template is None:
        raise AuthorityResolutionError("profile connector has no closed packaged schema registry")
    expected_hash = profile["output_schema_hashes"].get(operation)
    expected_ref = profile["output_schema_refs"].get(operation)
    for document in template["schema_documents"]:
        if document["schema_hash"] == expected_hash and document["schema_ref"] == expected_ref:
            return document["document"]
    raise AuthorityResolutionConflict("profile output schema is not the exact registered schema document")


def _normal_record_refs(source_ref: str, raw_payload: Any) -> list[str]:
    if not isinstance(raw_payload, Mapping):
        raise AuthorityResolutionConflict("structured source payload must be an object")
    records = raw_payload.get("records")
    refs = raw_payload.get("source_record_refs")
    if not isinstance(records, list) or not isinstance(refs, list):
        raise AuthorityResolutionConflict("structured source payload lacks normalized records")
    record_refs: list[str] = []
    for index, item in enumerate(records):
        if not isinstance(item, Mapping) or set(item) != {
            "record_ref", "revision_of_ref", "record_hash"
        }:
            raise AuthorityResolutionConflict(f"normalized record {index} is not closed")
        record_ref = _text(item["record_ref"], f"records[{index}].record_ref")
        _hash(item["record_hash"], f"records[{index}].record_hash")
        revision = item["revision_of_ref"]
        if revision is not None:
            _text(revision, f"records[{index}].revision_of_ref")
        record_refs.append(record_ref)
    if record_refs != refs or len(record_refs) != len(set(record_refs)):
        raise AuthorityResolutionConflict("normalized records/source_record_refs are not exact")
    return record_refs


def _time_before(left: str, right: str, name: str) -> None:
    if _timestamp(left, name) > _timestamp(right, right):
        raise AuthorityResolutionConflict(f"{name} is after {right}")


def _time_after(left: str, right: str, name: str) -> None:
    if _timestamp(left, name) < _timestamp(right, right):
        raise AuthorityResolutionConflict(f"{name} is before {right}")


_SUMMARY_FIELDS = {
    "schema_version", "id", "created_at", "source_envelope_ref", "source_envelope_hash",
    "artifact_ref", "artifact_hash", "source_ref", "operation", "execution_ref", "execution_hash",
    "connector_invocation_ref", "connector_invocation_hash", "connector_profile_ref", "connector_profile_hash",
    "call_spec_ref", "call_spec_hash", "work_order_ref", "work_order_hash", "result_envelope_ref",
    "result_envelope_hash", "scheduler_attempt_number", "physical_attempt_refs", "result_physical_attempt_ref",
    "reservation_ref", "reservation_hash", "usage_ref", "usage_hash", "cost_ref", "cost_hash",
    "settlement_ref", "settlement_hash", "checkpoint_ref", "checkpoint_hash", "plan_ref", "plan_hash",
    "context_pack_ref", "context_pack_hash", "step_ref", "step_hash", "runner_request_ref", "runner_request_hash",
    "actual_runner_request_ref", "actual_runner_request_hash",
    "receipt_ref", "receipt_hash", "source_record_refs", "raw_response_hash", "source_schema_hash",
    "source_content_hash", "published_at", "updated_at", "as_of", "retrieved_at", "completeness", "status",
    "access_policy_ref", "retention_policy_ref", "terms_policy_ref", "content_hash",
}


def validate_authority_resolution(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise AuthorityResolutionError("AuthorityResolution must be an object")
    unknown = set(value) - _SUMMARY_FIELDS
    missing = _SUMMARY_FIELDS - set(value)
    if missing or unknown:
        raise AuthorityResolutionError(
            f"AuthorityResolution closed shape mismatch: missing={sorted(missing)}, unknown={sorted(unknown)}"
        )
    wire = json.loads(canonical_json(value))
    if wire["schema_version"] != SCHEMA_VERSION:
        raise AuthorityResolutionError("unsupported AuthorityResolution schema_version")
    for field in ("id", "source_envelope_ref", "artifact_ref", "source_ref", "operation", "execution_ref", "connector_invocation_ref", "connector_profile_ref", "call_spec_ref", "work_order_ref", "result_envelope_ref", "result_physical_attempt_ref", "reservation_ref", "usage_ref", "cost_ref", "settlement_ref", "checkpoint_ref", "plan_ref", "context_pack_ref", "step_ref", "runner_request_ref", "actual_runner_request_ref", "receipt_ref"):
        _text(wire[field], field)
    for field in (key for key in wire if key.endswith("_hash") or key in {"raw_response_hash", "source_schema_hash", "source_content_hash"}):
        _hash(wire[field], field)
    wire["created_at"] = _timestamp(wire["created_at"], "created_at")
    for field in ("published_at", "updated_at", "as_of"):
        wire[field] = _timestamp(wire[field], field, nullable=True)
    wire["retrieved_at"] = _timestamp(wire["retrieved_at"], "retrieved_at")
    if wire["completeness"] not in _COMPLETENESS or wire["status"] not in _SOURCE_STATUS:
        raise AuthorityResolutionError("AuthorityResolution source status/completeness is invalid")
    refs = wire["physical_attempt_refs"]
    if not isinstance(refs, list) or not refs or len(refs) != len(set(refs)):
        raise AuthorityResolutionError("physical_attempt_refs must be a unique non-empty array")
    for ref in refs:
        _text(ref, "physical_attempt_refs[]")
    if not isinstance(wire["source_record_refs"], list) or len(wire["source_record_refs"]) != len(set(wire["source_record_refs"])):
        raise AuthorityResolutionError("source_record_refs must be unique")
    for ref in wire["source_record_refs"]:
        _text(ref, "source_record_refs[]")
    declared = _hash(wire["content_hash"], "content_hash")
    if declared != content_hash({key: value for key, value in wire.items() if key != "content_hash"}):
        raise AuthorityResolutionConflict("AuthorityResolution content_hash mismatch")
    return wire


@dataclass(frozen=True)
class ResolvedAuthority:
    """A closed summary plus private read results used by the verifier."""

    summary: dict[str, Any]
    records: dict[str, dict[str, Any]]
    raw_bytes: bytes
    raw_payload: Any

    def to_dict(self) -> dict[str, Any]:
        return dict(self.summary)


class ConnectorAuthorityResolver:
    """Resolve one successful SourceEnvelope from exact read-only authority rows."""

    def __init__(
        self,
        *,
        core: Any,
        connectors: Any,
        observability: Any,
        scheduler: Any,
        coordinator: Any,
        artifact_reader: Callable[[Mapping[str, Any]], bytes],
        runner_journal: Any | None = None,
    ) -> None:
        self.core = core
        self.connectors = connectors
        self.observability = observability
        self.scheduler = scheduler
        self.coordinator = coordinator
        self.artifact_reader = artifact_reader
        self.runner_journal = runner_journal
        for name, owner in (("core", core), ("connectors", connectors), ("observability", observability), ("scheduler", scheduler), ("coordinator", coordinator)):
            connection = getattr(owner, "connection", None)
            if not isinstance(connection, sqlite3.Connection):
                raise TypeError(f"{name} must expose a sqlite3 connection")
        if not callable(artifact_reader):
            raise TypeError("artifact_reader must be callable")

    @staticmethod
    def _row(owner: Any, sql: str, args: tuple[Any, ...], *, table: str, ref: str, json_column: str, id_column: str) -> dict[str, Any]:
        row = owner.connection.execute(sql, args).fetchone()
        return _canonical_row(row, json_column=json_column, id_column=id_column, ref=ref, table=table)

    def _source(self, ref: str) -> dict[str, Any]:
        return self._row(self.connectors, "SELECT * FROM connector_source_envelopes WHERE source_envelope_id=?", (ref,), table="connector_source_envelopes", ref=ref, json_column="record_json", id_column="source_envelope_id")

    def _profile(self, ref: str) -> dict[str, Any]:
        return self._row(self.connectors, "SELECT * FROM connector_profile_versions WHERE profile_version_id=?", (ref,), table="connector_profile_versions", ref=ref, json_column="record_json", id_column="profile_version_id")

    def _call(self, ref: str) -> dict[str, Any]:
        return self._row(self.connectors, "SELECT * FROM connector_call_specs WHERE call_spec_id=?", (ref,), table="connector_call_specs", ref=ref, json_column="record_json", id_column="call_spec_id")

    def _invocation(self, ref: str) -> dict[str, Any]:
        return self._row(self.connectors, "SELECT * FROM connector_invocations WHERE connector_invocation_id=?", (ref,), table="connector_invocations", ref=ref, json_column="record_json", id_column="connector_invocation_id")

    def _execution(self, ref: str) -> tuple[dict[str, Any], str]:
        row = self.core.connection.execute(
            "SELECT execution_id, execution_json, content_hash FROM execution_invocations WHERE execution_id=?",
            (ref,),
        ).fetchone()
        if row is None:
            raise AuthorityResolutionError(f"execution invocation not found: {ref}")
        try:
            wire = json.loads(row["execution_json"])
        except (TypeError, json.JSONDecodeError) as exc:
            raise AuthorityResolutionConflict("execution invocation JSON is invalid") from exc
        if not isinstance(wire, dict) or wire.get("id") != ref:
            raise AuthorityResolutionConflict("execution invocation identity does not match")
        if row["content_hash"] != content_hash(wire):
            raise AuthorityResolutionConflict("execution invocation hash is not canonical")
        return wire, row["content_hash"]

    def _artifact(self, ref: str) -> dict[str, Any]:
        row = self.observability.connection.execute(
            "SELECT i.*, v.record_json AS artifact_record_json, v.content_hash AS artifact_row_hash "
            "FROM observability_artifact_version_index i JOIN observability_artifact_versions_v2 v "
            "ON v.version_id=i.version_id WHERE i.version_id=? AND i.schema_version='0.2'",
            (ref,),
        ).fetchone()
        if row is None:
            raise AuthorityResolutionError(f"ArtifactVersion v0.2 not found: {ref}")
        try:
            wire = json.loads(row["artifact_record_json"])
        except (TypeError, json.JSONDecodeError) as exc:
            raise AuthorityResolutionConflict("artifact record_json is invalid") from exc
        if not isinstance(wire, dict) or wire.get("id") != ref:
            raise AuthorityResolutionConflict("artifact identity does not match index")
        if wire.get("content_hash") != row["artifact_row_hash"] or row["record_hash"] != row["artifact_row_hash"]:
            raise AuthorityResolutionConflict("artifact/index hash mismatch")
        if content_hash({key: value for key, value in wire.items() if key != "content_hash"}) != wire.get("content_hash"):
            raise AuthorityResolutionConflict("artifact content hash is not canonical")
        for field in ("artifact_ref", "version", "producer_execution_ref", "work_order_ref", "result_envelope_ref", "result_envelope_hash", "artifact_content_hash", "size_bytes", "created_at", "prior_version_ref"):
            if field not in wire:
                raise AuthorityResolutionConflict(f"artifact is missing {field}")
        if wire["schema_version"] != "0.2" or row["schema_version"] != "0.2":
            raise AuthorityResolutionConflict("artifact is not v0.2")
        if row["artifact_ref"] != wire["artifact_ref"] or int(row["version_number"]) != wire["version"] or row["producer_execution_ref"] != wire["producer_execution_ref"]:
            raise AuthorityResolutionConflict("artifact index fields do not match record")
        if row["prior_version_ref"] != wire["prior_version_ref"]:
            raise AuthorityResolutionConflict("artifact prior-version binding drifted")
        return wire

    def _work_order(self, ref: str) -> tuple[dict[str, Any], sqlite3.Row]:
        row = self.scheduler.connection.execute("SELECT * FROM scheduler_work_orders WHERE work_order_id=?", (ref,)).fetchone()
        if row is None:
            raise AuthorityResolutionError(f"WorkOrder not found: {ref}")
        try:
            wire = WorkOrder.from_dict(json.loads(row["work_order_json"])).to_dict()
        except Exception as exc:
            raise AuthorityResolutionConflict("scheduler WorkOrder is not valid") from exc
        if content_hash(wire) != row["work_order_hash"]:
            raise AuthorityResolutionConflict("scheduler WorkOrder hash mismatch")
        return wire, row

    def _result(self, ref: str) -> tuple[dict[str, Any], sqlite3.Row]:
        row = self.scheduler.connection.execute("SELECT * FROM scheduler_result_envelopes WHERE result_envelope_id=?", (ref,)).fetchone()
        if row is None:
            raise AuthorityResolutionError(f"ResultEnvelope not found: {ref}")
        try:
            wire = ResultEnvelope.from_dict(json.loads(row["result_envelope_json"])).to_dict()
        except Exception as exc:
            raise AuthorityResolutionConflict("scheduler ResultEnvelope is not valid") from exc
        if content_hash(wire) != row["result_envelope_hash"]:
            raise AuthorityResolutionConflict("ResultEnvelope hash mismatch")
        if row["content_hash"] != content_hash({
            "result_envelope_id": row["result_envelope_id"], "work_order_id": row["work_order_id"],
            "attempt_number": row["attempt_number"], "result_envelope_hash": row["result_envelope_hash"],
            "outcome": row["outcome"], "created_at": row["created_at"],
        }):
            raise AuthorityResolutionConflict("scheduler ResultEnvelope receipt hash mismatch")
        if wire["id"] != ref or wire["status"] != "succeeded" or row["outcome"] != "succeeded":
            raise AuthorityResolutionConflict("source resolution requires a successful ResultEnvelope")
        return wire, row

    def _connector_record(self, table: str, id_column: str, ref: str) -> dict[str, Any]:
        return self._row(self.connectors, f"SELECT * FROM {table} WHERE {id_column}=?", (ref,), table=table, ref=ref, json_column="record_json", id_column=id_column)

    def _checkpoint_records(self, checkpoint_ref: str) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
        row = self.coordinator.connection.execute(
            "SELECT * FROM research_checkpoints WHERE checkpoint_id=?", (checkpoint_ref,)
        ).fetchone()
        if row is None:
            raise AuthorityResolutionError(f"ResearchCheckpoint not found: {checkpoint_ref}")
        selected = validate_research_checkpoint(_canonical_row(
            row, json_column="checkpoint_json", id_column="checkpoint_id",
            ref=checkpoint_ref, table="research_checkpoints"
        ))
        plan_row = self.coordinator.connection.execute(
            "SELECT * FROM compiled_connector_plans WHERE plan_id=?",
            (selected["compiled_plan_ref"],),
        ).fetchone()
        context_row = self.coordinator.connection.execute(
            "SELECT * FROM context_packs WHERE context_pack_id=?",
            (selected["context_pack_ref"],),
        ).fetchone()
        if plan_row is None or context_row is None:
            raise AuthorityResolutionConflict("checkpoint plan/context authority is missing")
        plan = validate_compiled_connector_plan(_canonical_row(
            plan_row, json_column="plan_json", id_column="plan_id",
            ref=selected["compiled_plan_ref"], table="compiled_connector_plans"
        ))
        context = validate_context_pack(_canonical_row(
            context_row, json_column="context_pack_json", id_column="context_pack_id",
            ref=selected["context_pack_ref"], table="context_packs"
        ))
        checkpoint_rows = self.coordinator.connection.execute(
            "SELECT * FROM research_checkpoints WHERE run_ref=? ORDER BY sequence_number",
            (selected["run_ref"],),
        ).fetchall()
        checkpoints: list[dict[str, Any]] = []
        final_request: dict[str, Any] | None = None
        final_receipt: dict[str, Any] | None = None
        final_step: dict[str, Any] | None = None
        for index, checkpoint_row in enumerate(checkpoint_rows):
            current = validate_research_checkpoint(_canonical_row(
                checkpoint_row, json_column="checkpoint_json", id_column="checkpoint_id",
                ref=checkpoint_row["checkpoint_id"], table="research_checkpoints"
            ))
            if current["sequence"] != index + 1 or current["run_ref"] != selected["run_ref"]:
                raise AuthorityResolutionConflict("checkpoint sequence is not contiguous")
            expected_prior = (None, None) if index == 0 else (
                checkpoints[index - 1]["id"], checkpoints[index - 1]["content_hash"]
            )
            if (current["prior_checkpoint_ref"], current["prior_checkpoint_hash"]) != expected_prior:
                raise AuthorityResolutionConflict("checkpoint prior chain is not exact")
            for field in (
                "attempt_ref", "attempt_hash", "compiled_plan_ref", "compiled_plan_hash",
                "context_pack_ref", "context_pack_hash",
            ):
                if current[field] != selected[field]:
                    raise AuthorityResolutionConflict("checkpoint chain changed immutable run binding")
            step = next((item for item in plan["steps"] if item["id"] == current["step_ref"]), None)
            if step is None:
                raise AuthorityResolutionConflict("checkpoint step is not in its plan")
            step = validate_compiled_connector_step(step)
            request_row = self.coordinator.connection.execute(
                "SELECT * FROM research_runner_requests WHERE runner_request_id=?",
                (current["runner_request_ref"],),
            ).fetchone()
            receipt_row = self.coordinator.connection.execute(
                "SELECT * FROM research_completion_receipts WHERE receipt_id=?",
                (current["completion_receipt_ref"],),
            ).fetchone()
            if request_row is None or receipt_row is None:
                raise AuthorityResolutionConflict("checkpoint request/receipt authority is missing")
            request = validate_runner_request_plan_binding(_canonical_row(
                request_row, json_column="request_json", id_column="runner_request_id",
                ref=current["runner_request_ref"], table="research_runner_requests"
            ), plan, step)
            receipt = validate_connector_completion_receipt(_canonical_row(
                receipt_row, json_column="receipt_json", id_column="receipt_id",
                ref=current["completion_receipt_ref"], table="research_completion_receipts"
            ))
            if (
                current["runner_request_ref"] != request["id"]
                or current["runner_request_hash"] != request["content_hash"]
                or current["completion_receipt_ref"] != receipt["id"]
                or current["completion_receipt_hash"] != receipt["content_hash"]
                or current["connector_attempt_number"] != request["scheduler_attempt_number"]
                or current["outcome"] != receipt["status"]
                or current["source_envelopes"] != receipt["source_envelopes"]
                or current["artifacts"] != receipt["artifacts"]
                or current["next_cursor"] != receipt["next_cursor"]
                or current["retry_after_ms"] != receipt["retry_after_ms"]
                or receipt["runner_request_ref"] != request["id"]
                or receipt["runner_request_hash"] != request["content_hash"]
            ):
                raise AuthorityResolutionConflict("checkpoint request/receipt/outcome chain is not exact")
            expected_authority = {
                "connector_profile_ref": request["connector_profile_ref"],
                "connector_profile_hash": request["connector_profile_hash"],
                "capability_lease_ref": request["capability_lease_ref"],
                "capability_lease_hash": request["capability_lease_hash"],
                "source_ref": step["source_ref"],
                "source_hash": step["source_hash"],
            }
            if current["authority_bindings"] != expected_authority:
                raise AuthorityResolutionConflict("checkpoint authority bindings are not exact")
            checkpoints.append(current)
            final_request, final_receipt, final_step = request, receipt, step
        if not checkpoints or checkpoints[-1]["id"] != selected["id"]:
            raise AuthorityResolutionConflict("resolved checkpoint is not latest in its run")
        assert final_request is not None and final_receipt is not None and final_step is not None
        return selected, plan, context, final_step, final_request, final_receipt, checkpoints

    @staticmethod
    def _journal_event_hash(event: Mapping[str, Any]) -> str:
        base = {
            "runner_request_ref": event["runner_request_ref"],
            "request_ordinal": event["request_ordinal"],
            "state": event["state"],
            "reservation_ref": event["reservation_ref"],
            "event_at": event["event_at"],
            "recorded_at": event["recorded_at"],
            "payload": event["payload"],
        }
        return content_hash(base)

    def _journal_observation(
        self,
        request: Mapping[str, Any],
        *,
        result: Mapping[str, Any],
        artifact: Mapping[str, Any],
    ) -> tuple[
        dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]
    ]:
        """Read and verify the immutable observed journal event and response."""
        if self.runner_journal is None or not callable(getattr(self.runner_journal, "history", None)):
            raise AuthorityResolutionError("runner journal authority is required")
        try:
            journal_request = validate_connector_runner_request(
                self.runner_journal.request(request["id"])
            )
            # ConnectorRunnerRequest carries its own content_hash.  Rehashing
            # the complete wire would hash that embedded field a second time;
            # validator-side canonicalization is the authority check.
            if journal_request != dict(request):
                raise AuthorityResolutionConflict(
                    "runner journal request does not match trusted request authority"
                )
            history = self.runner_journal.history(request["id"])
        except Exception as exc:
            raise AuthorityResolutionError("runner request journal history is unavailable") from exc
        if not history:
            raise AuthorityResolutionConflict("runner request journal is empty")
        transitions = {
            None: {"admitted"}, "admitted": {"reserved"},
            "reserved": {"transport_started", "observed", "released_recovered"},
            "transport_started": {"observed", "indeterminate_recovered"},
            "observed": {"responded"}, "responded": set(),
            "released_recovered": set(), "indeterminate_recovered": {"responded"},
        }
        prior: str | None = None
        observed_events: list[Mapping[str, Any]] = []
        transport_events: list[Mapping[str, Any]] = []
        state_events: dict[str, Mapping[str, Any]] = {}
        for ordinal, event in enumerate(history, start=1):
            if event.get("runner_request_ref") != request["id"] or event.get("request_ordinal") != ordinal:
                raise AuthorityResolutionConflict("runner journal ordinal/request chain is not exact")
            if event.get("state") not in transitions.get(prior, set()):
                raise AuthorityResolutionConflict("runner journal state transition is invalid")
            if event.get("content_hash") != self._journal_event_hash(event):
                raise AuthorityResolutionConflict("runner journal event hash mismatch")
            expected_event_id = "runner-journal-event:" + content_hash({
                "runner_request_ref": request["id"],
                "request_ordinal": ordinal,
                "content_hash": event["content_hash"],
            })
            if event.get("id") != expected_event_id:
                raise AuthorityResolutionConflict("runner journal event identity mismatch")
            if event["state"] == "observed":
                observed_events.append(event)
            if event["state"] == "transport_started":
                transport_events.append(event)
            state_events[event["state"]] = event
            prior = event["state"]
        if prior != "responded" or len(observed_events) != 1 or len(transport_events) != 1:
            raise AuthorityResolutionConflict("runner journal does not contain one completed observation")
        observed = observed_events[0]
        observed_payload = observed["payload"]
        admitted_payload = state_events["admitted"]["payload"]
        reserved_payload = state_events["reserved"]["payload"]
        responded_payload = state_events["responded"]["payload"]
        if admitted_payload != {"request_hash": request["content_hash"]}:
            raise AuthorityResolutionConflict(
                "runner journal admission does not bind the exact request"
            )
        observation = observed_payload.get("observation")
        raw_object = observed_payload.get("raw_object")
        if not isinstance(raw_object, Mapping):
            raise AuthorityResolutionConflict("successful journal observation lacks raw object authority")
        try:
            observation = validate_adapter_transport_observation(observation)
        except Exception as exc:
            raise AuthorityResolutionConflict("journal observation is not a valid adapter observation") from exc
        if observation["outcome"] != "succeeded":
            raise AuthorityResolutionConflict("source resolution requires a succeeded adapter observation")
        transport_payload = transport_events[0]["payload"]
        try:
            adapter_request = validate_connector_adapter_request(transport_payload["adapter_request"])
        except Exception as exc:
            raise AuthorityResolutionConflict("transport barrier lacks a valid AdapterRequest") from exc
        if (
            set(transport_payload) != {
                "adapter_request", "adapter_request_hash", "physical_attempt_number",
                "raw_sink_ref", "reservation_hash", "reservation_ref", "started_at",
            }
            or set(observed_payload) != {
                "adapter_request_hash", "attempt_outcome", "commit_context",
                "completed_at", "error", "observation", "physical_attempt_number",
                "raw_object", "reservation_hash", "reservation_ref", "retry_at",
                "started_at",
            }
            or set(reserved_payload) != {
                "physical_attempt_number", "reservation_hash", "reservation_ref"
            }
            or set(responded_payload) != {"reservation_ref", "response"}
        ):
            raise AuthorityResolutionConflict("runner journal payload shape drifted")
        reservation_binding = (
            transport_payload["reservation_ref"],
            transport_payload["reservation_hash"],
            transport_payload["physical_attempt_number"],
        )
        if (
            observation["request_hash"] != adapter_request["content_hash"]
            or transport_payload["adapter_request_hash"] != adapter_request["content_hash"]
            or observed_payload["adapter_request_hash"] != adapter_request["content_hash"]
            or transport_payload["raw_sink_ref"] != adapter_request["raw_sink_ref"]
            or reservation_binding
            != (
                reserved_payload["reservation_ref"],
                reserved_payload["reservation_hash"],
                reserved_payload["physical_attempt_number"],
            )
            or reservation_binding
            != (
                observed_payload["reservation_ref"],
                observed_payload["reservation_hash"],
                observed_payload["physical_attempt_number"],
            )
            or responded_payload["reservation_ref"] != reservation_binding[0]
        ):
            raise AuthorityResolutionConflict("observation is not bound to the exact AdapterRequest")
        adapter_pairs = (
            (adapter_request["runner_request_ref"], request["id"]),
            (adapter_request["runner_request_hash"], request["content_hash"]),
            (adapter_request["connector_invocation_ref"], request["connector_invocation_ref"]),
            (adapter_request["profile_ref"], request["connector_profile_ref"]),
            (adapter_request["profile_hash"], request["connector_profile_hash"]),
            (adapter_request["call_spec_ref"], request["call_spec_ref"]),
            (adapter_request["call_spec_hash"], request["call_spec_hash"]),
        )
        if any(actual != expected for actual, expected in adapter_pairs):
            raise AuthorityResolutionConflict(
                "AdapterRequest does not bind the actual RunnerRequest authority"
            )
        if (
            raw_object.get("content_hash") != artifact["artifact_content_hash"]
            or int(raw_object.get("size_bytes", -1)) != int(artifact["size_bytes"])
            or raw_object.get("storage_locator") != artifact.get("storage_locator")
        ):
            raise AuthorityResolutionConflict("journal raw object is not the exact ArtifactVersion")
        response_event = history[-1]
        response = response_event["payload"].get("response")
        try:
            response_wire = validate_connector_runner_response(response)
        except Exception as exc:
            raise AuthorityResolutionConflict("runner response event is malformed") from exc
        if response_wire["result_envelope_ref"] != result["id"] or response_wire["result_envelope_hash"] != content_hash(result):
            raise AuthorityResolutionConflict("runner response does not bind the ResultEnvelope")
        commit_context = observed_payload.get("commit_context")
        if not isinstance(commit_context, Mapping):
            raise AuthorityResolutionConflict(
                "runner journal observation lacks a closed commit context"
            )
        return (
            observation,
            dict(raw_object),
            dict(response_wire),
            dict(adapter_request),
            dict(commit_context),
        )

    def resolve(self, source_envelope_ref: str, *, checkpoint_ref: str | None = None) -> ResolvedAuthority:
        source_envelope_ref = _text(source_envelope_ref, "source_envelope_ref")
        source = self._source(source_envelope_ref)
        if source["status"] not in {"complete", "empty"} or source["completeness"] == "unknown":
            raise AuthorityResolutionConflict("partial, error, or unknown-completeness source is not reviewable")
        invocation = self._invocation(source["connector_invocation_ref"])
        profile = self._profile(source["connector_profile_ref"])
        call = self._call(invocation["call_spec_ref"])
        execution, execution_hash = self._execution(invocation["execution_ref"])
        execution_obj = ExecutionInvocation.from_dict(execution)
        if execution_obj.kind is not ExecutionKind.CONNECTOR:
            raise AuthorityResolutionConflict("source producer execution is not a connector execution")
        if profile["source_hash"] != content_hash(profile["source_identity"]):
            raise AuthorityResolutionConflict("profile source identity hash is not canonical")
        for actual, expected, name in (
            (source["connector_profile_ref"], invocation["connector_profile_ref"], "source/profile"),
            (source["connector_invocation_ref"], invocation["id"], "source/invocation"),
            (source["operation"], call["operation"], "source/operation"),
            (source["source"], profile["source_identity"]["source_ref"], "source/profile identity"),
        ):
            if actual != expected:
                raise AuthorityResolutionConflict(f"{name} binding mismatch")
        if (
            invocation["connector_profile_hash"] != profile["content_hash"]
            or invocation["call_spec_hash"] != call["content_hash"]
            or execution_hash != invocation["execution_hash"]
        ):
            raise AuthorityResolutionConflict("invocation/profile/call/execution hash chain drifted")
        if call["query_hash"] != content_hash({"operation": call["operation"], "parameters": call["parameters"]}):
            raise AuthorityResolutionConflict("CallSpec query hash is not canonical")
        if (
            call["connector_profile_ref"] != profile["id"]
            or call["operation"] != source["operation"]
            or call["work_order_hash"] != invocation["work_order_hash"]
        ):
            raise AuthorityResolutionConflict("CallSpec does not bind SourceEnvelope invocation")
        if (
            execution_obj.id != invocation["id"]
            or execution_obj.work_order_ref != invocation["work_order_ref"]
            or execution_obj.profile_ref != profile["id"]
            or execution_obj.input_refs != (call["id"],)
            or len(execution_obj.output_refs) != 1
        ):
            raise AuthorityResolutionConflict("ExecutionInvocation does not exactly match connector subtype")

        artifact = self._artifact(source["raw_artifact_version_ref"])
        if (
            artifact["producer_execution_ref"] != execution_obj.id
            or artifact["artifact_ref"] != execution_obj.output_refs[0]
            or artifact["artifact_content_hash"] != source["raw_response_hash"]
            or artifact["work_order_ref"] != invocation["work_order_ref"]
        ):
            raise AuthorityResolutionConflict("ArtifactVersion producer/output/raw binding drifted")
        raw_bytes = self.artifact_reader(artifact)
        if _sha256(raw_bytes) != artifact["artifact_content_hash"] or len(raw_bytes) != int(artifact["size_bytes"]):
            raise AuthorityResolutionConflict("raw artifact bytes do not match ArtifactVersion authority")
        raw_payload = _json_bytes(raw_bytes)

        work_order, work_row = self._work_order(invocation["work_order_ref"])
        if (
            work_row["work_order_hash"] != invocation["work_order_hash"]
            or call["work_order_ref"] != work_order["id"]
            or content_hash(work_order) != call["work_order_hash"]
        ):
            raise AuthorityResolutionConflict("WorkOrder identity/hash chain drifted")
        result_ref = artifact["result_envelope_ref"]
        result, result_row = self._result(result_ref)
        trusted_effects = (
            ("read:public-http",)
            if source["source"] == "source:sec-edgar"
            else tuple(work_order["declared_side_effects"])
        )
        if tuple(work_order["declared_side_effects"]) != trusted_effects:
            raise AuthorityResolutionConflict(
                "WorkOrder is not the exact read-only source operation"
            )
        if (
            result["work_order_ref"] != work_order["id"]
            or result["invocation_ref"] != invocation["id"]
            or tuple(result["artifact_refs"]) != (artifact["artifact_ref"],)
            or tuple(result["actual_side_effects"]) != trusted_effects
        ):
            raise AuthorityResolutionConflict("ResultEnvelope does not bind exact connector output/effects")

        checkpoint_ref = checkpoint_ref or source.get("checkpoint_ref")
        if checkpoint_ref is None:
            raise AuthorityResolutionError("a persisted ResearchCheckpoint reference is required")
        checkpoint, plan, context, step, request, receipt, checkpoints = self._checkpoint_records(checkpoint_ref)
        for actual, expected, name in (
            (request["connector_invocation_ref"], invocation["id"], "request invocation"),
            (request["connector_invocation_hash"], invocation["content_hash"], "request invocation hash"),
            (request["execution_ref"], execution_obj.id, "request execution"),
            (request["execution_hash"], execution_hash, "request execution hash"),
            (request["connector_profile_ref"], profile["id"], "request profile"),
            (request["connector_profile_hash"], profile["content_hash"], "request profile hash"),
            (request["call_spec_ref"], call["id"], "request call spec"),
            (request["work_order_ref"], work_order["id"], "request work order"),
            (request["work_order_hash"], work_row["work_order_hash"], "request work order hash"),
        ):
            if actual != expected:
                raise AuthorityResolutionConflict(f"{name} does not bind exact authority")
        if (
            receipt["status"] != "succeeded"
            or receipt["result_ref"] != result["id"]
            or receipt["result_hash"] != content_hash(result)
            or receipt["source_envelopes"] != [{"ref": source["id"], "hash": source["content_hash"]}]
            or receipt["artifacts"] != [{"ref": artifact["id"], "hash": artifact["content_hash"]}]
        ):
            raise AuthorityResolutionConflict("completion receipt does not bind exact source/artifact/result authority")
        if receipt.get("schema_version") != "0.2":
            raise AuthorityResolutionConflict(
                "successful connector authority requires a v0.2 completion receipt"
            )
        actual_ref = _text(receipt.get("actual_runner_request_ref"), "actual_runner_request_ref")
        actual_hash = _hash(receipt.get("actual_runner_request_hash"), "actual_runner_request_hash")
        if self.runner_journal is None:
            raise AuthorityResolutionError("actual connector runner request authority is required")
        try:
            actual_request = validate_connector_runner_request(
                self.runner_journal.request(actual_ref)
            )
        except Exception as exc:
            raise AuthorityResolutionConflict(
                "actual connector runner request is not in the immutable journal"
            ) from exc
        if actual_request["content_hash"] != actual_hash:
            raise AuthorityResolutionConflict(
                "completion receipt actual runner request hash drifted"
            )
        # The coordinator request binds the compiled research plan and keeps
        # the logical query hash.  The journal request is the runner's
        # authority request and binds the immutable CallSpec hash.  A bridge
        # must preserve all other authority coordinates exactly.
        for field in (
            "connector_invocation_ref", "connector_invocation_hash", "execution_ref",
            "execution_hash", "work_order_ref", "work_order_hash", "scheduler_attempt_number",
            "scheduler_lease_revision_ref", "scheduler_lease_hash", "connector_profile_ref",
            "connector_profile_hash", "call_spec_ref", "capability_lease_ref",
            "capability_lease_hash", "principal_ref", "runner_runtime_ref", "runner_actor_ref",
            "runner_environment_hash",
        ):
            if actual_request[field] != request[field]:
                raise AuthorityResolutionConflict(
                    f"actual runner request {field} is not bridged from coordinator authority"
                )
        if actual_request["call_spec_hash"] != call["content_hash"]:
            raise AuthorityResolutionConflict(
                "actual runner request does not bind immutable CallSpec hash"
            )
        observation, raw_object, runner_response, adapter_request, commit_context = self._journal_observation(
            request=actual_request, result=result, artifact=artifact
        )
        expected_commit_context = {
            "request": actual_request,
            "work_order": work_order,
            "profile": profile,
            "call_spec": call,
            "invocation": invocation,
            "execution": execution,
            "binding_side_effects": list(trusted_effects),
        }
        if commit_context != expected_commit_context:
            raise AuthorityResolutionConflict(
                "runner journal commit context drifted from immutable authority"
            )
        adapter_pairs = (
            (adapter_request["source_identity"], profile["source_identity"]),
            (adapter_request["source_hash"], profile["source_hash"]),
            (adapter_request["adapter_ref"], profile["adapter_ref"]),
            (adapter_request["adapter_hash"], profile["adapter_hash"]),
            (adapter_request["operation"], call["operation"]),
            (adapter_request["parameters"], call["parameters"]),
            (adapter_request["query_hash"], call["query_hash"]),
            (adapter_request["input_schema_ref"], profile["input_schema_refs"][call["operation"]]),
            (adapter_request["input_schema_hash"], profile["input_schema_hashes"][call["operation"]]),
            (adapter_request["output_schema_ref"], profile["output_schema_refs"][call["operation"]]),
            (adapter_request["output_schema_hash"], profile["output_schema_hashes"][call["operation"]]),
            (adapter_request["allowed_hosts"], sorted(profile["allowed_hosts"])),
            (adapter_request["network_policy"], profile["network_policy"]),
            (adapter_request["credential_grant_ref"], None),
            (adapter_request["max_response_bytes"], profile["max_response_bytes"]),
            (adapter_request["max_records"], profile["max_records"]),
        )
        if any(actual != expected for actual, expected in adapter_pairs):
            raise AuthorityResolutionConflict(
                "AdapterRequest drifted from Profile/CallSpec authority"
            )
        if (
            observation["source_record_refs"] != source["source_record_refs"]
            or observation["cursor"] != source["cursor"]
            or observation["provider_request_id"] != source["provider_request_id"]
            or observation["structured_output"] is None
        ):
            raise AuthorityResolutionConflict("SourceEnvelope is not bound to the persisted adapter observation")
        try:
            if source["source"] == "source:sec-edgar" and source["operation"] == "list_filings":
                normalized = normalize_sec_submissions(
                    raw_payload, call["parameters"],
                    provider_status=int(observation["provider_status_code"] or 0),
                )
            else:
                raise SecPublicAdapterError("no source-specific authority normalizer for this source")
        except Exception as exc:
            raise AuthorityResolutionConflict("raw provider body cannot be normalized by the frozen adapter") from exc
        _schema_matches(
            observation["structured_output"],
            _inventory_output_schema(profile, source["operation"]),
            "adapter structured output",
        )
        if normalized != observation["structured_output"]:
            raise AuthorityResolutionConflict("raw provider body replay does not match adapter observation")
        if _normal_record_refs(source["source"], observation["structured_output"]) != source["source_record_refs"]:
            raise AuthorityResolutionConflict("SourceEnvelope record refs do not match structured observation")
        if source["source_schema_hash"] != profile["output_schema_hashes"][source["operation"]]:
            raise AuthorityResolutionConflict("SourceEnvelope schema hash does not match profile")
        if source["source_content_hash"] != source_envelope_content_hash(source):
            raise AuthorityResolutionConflict("SourceEnvelope structured content hash is not canonical")
        # Recompute the formal result and every scheduler event from the row
        # fields.  A matching result id/hash alone is not enough because the
        # formal record and event are separate immutable authorities.
        formal = self.scheduler.connection.execute(
            "SELECT * FROM scheduler_formal_results WHERE work_order_id=?",
            (work_order["id"],),
        ).fetchone()
        if formal is None or formal["terminal_state"] != "succeeded":
            raise AuthorityResolutionConflict("successful formal scheduler result is missing")
        if (
            formal["result_envelope_id"] != result["id"]
            or formal["result_envelope_hash"] != content_hash(result)
            or formal["result_envelope_json"] != canonical_json(result)
            or formal["content_hash"] != content_hash({
                "id": formal["result_record_id"],
                "work_order_id": formal["work_order_id"],
                "attempt_number": formal["attempt_number"],
                "result_envelope_id": formal["result_envelope_id"],
                "result_envelope_hash": formal["result_envelope_hash"],
                "terminal_state": formal["terminal_state"],
                "created_at": formal["created_at"],
            })
        ):
            raise AuthorityResolutionConflict("formal scheduler result hash or payload drifted")
        scheduler_events = self.scheduler.connection.execute(
            "SELECT * FROM scheduler_attempt_events WHERE work_order_id=? ORDER BY event_seq",
            (work_order["id"],),
        ).fetchall()
        if not scheduler_events:
            raise AuthorityResolutionConflict("scheduler attempt event chain is empty")
        scheduler_result_events: list[sqlite3.Row] = []
        prior_event_id: str | None = None
        for row in scheduler_events:
            if row["prior_event_id"] != prior_event_id:
                raise AuthorityResolutionConflict("scheduler attempt prior-event chain drifted")
            if row["wire_version"] == "0.2":
                event_wire = {
                    "schema_version": "0.1", "wire_version": "0.2", "id": row["event_id"],
                    "created_at": row["created_at"], "work_order_ref": row["work_order_id"],
                    "attempt_number": row["attempt_number"], "state": row["state"],
                    "lease_ref": row["lease_revision_id"],
                    "result_envelope_ref": row["result_envelope_id"],
                    "result_envelope_hash": row["result_envelope_hash"],
                    "reason": row["reason"], "not_before": row["not_before"],
                    "prior_event_ref": row["prior_event_id"],
                }
            else:
                event_wire = {
                    "schema_version": "0.1", "id": row["event_id"],
                    "created_at": row["created_at"], "work_order_ref": row["work_order_id"],
                    "attempt_number": row["attempt_number"], "state": row["state"],
                    "lease_ref": row["lease_revision_id"],
                    "result_envelope_ref": row["result_envelope_id"],
                    "result_envelope_hash": row["result_envelope_hash"],
                    "reason": row["reason"], "prior_event_ref": row["prior_event_id"],
                }
            if row["content_hash"] != content_hash(event_wire):
                raise AuthorityResolutionConflict("scheduler attempt event hash drifted")
            if row["state"] == "succeeded":
                scheduler_result_events.append(row)
            prior_event_id = row["event_id"]
        if len(scheduler_result_events) != 1 or (
            scheduler_result_events[0]["attempt_number"] != result_row["attempt_number"]
            or scheduler_result_events[0]["result_envelope_id"] != result["id"]
            or scheduler_result_events[0]["result_envelope_hash"] != content_hash(result)
        ):
            raise AuthorityResolutionConflict("scheduler successful attempt/result chain is not exact")

        result_attempt_ref = source["result_physical_attempt_ref"]
        physical_refs = list(source["physical_attempt_refs"])
        attempts: list[dict[str, Any]] = []
        for ref in physical_refs:
            attempt = self._connector_record("connector_physical_attempts", "physical_attempt_id", ref)
            if attempt["connector_invocation_ref"] != invocation["id"] or int(attempt["physical_attempt_number"]) < 1:
                raise AuthorityResolutionConflict("physical attempt does not belong to invocation")
            if attempt["outcome"] not in {"succeeded", "rate_limited", "timeout", "failed", "indeterminate"} or attempt["completed_at"] is None:
                raise AuthorityResolutionConflict("physical attempt is partial or non-terminal")
            attempts.append(attempt)
        if [item["id"] for item in sorted(attempts, key=lambda item: int(item["physical_attempt_number"]))] != physical_refs:
            raise AuthorityResolutionConflict("physical attempt refs are not in authority order")
        result_attempt = next((item for item in attempts if item["id"] == result_attempt_ref), None)
        if result_attempt is None or result_attempt["outcome"] != "succeeded" or result_attempt["provider_request_id"] != source["provider_request_id"]:
            raise AuthorityResolutionConflict("SourceEnvelope result attempt is not a successful exact attempt")
        if int(result_attempt["physical_attempt_number"]) != max(int(item["physical_attempt_number"]) for item in attempts):
            raise AuthorityResolutionConflict("SourceEnvelope result attempt is not the final physical attempt")
        reservation = self._connector_record("connector_quota_reservations", "reservation_id", result_attempt["reservation_ref"])
        if reservation["connector_invocation_ref"] != invocation["id"] or int(reservation["physical_attempt_number"]) != int(result_attempt["physical_attempt_number"]):
            raise AuthorityResolutionConflict("reservation/physical attempt identity mismatch")
        if (
            adapter_request["reservation_ref"] != reservation["id"]
            or adapter_request["reservation_hash"] != reservation["content_hash"]
            or int(adapter_request["physical_attempt_number"])
            != int(result_attempt["physical_attempt_number"])
            or result["metadata"] != {
                "runner_request_ref": actual_request["id"],
                "physical_attempt_outcome": "succeeded",
            }
        ):
            raise AuthorityResolutionConflict(
                "AdapterRequest/ResultEnvelope do not bind the exact physical attempt"
            )
        usage_rows = self.connectors.connection.execute(
            "SELECT * FROM connector_usage_entries WHERE physical_attempt_ref=? ORDER BY revision_number DESC",
            (result_attempt["id"],),
        ).fetchall()
        if not usage_rows:
            raise AuthorityResolutionConflict("successful attempt lacks Usage authority")
        usage = _canonical_row(usage_rows[0], json_column="record_json", id_column="usage_entry_id", ref=usage_rows[0]["usage_entry_id"], table="connector_usage_entries")
        cost_rows = self.connectors.connection.execute(
            "SELECT * FROM connector_cost_entries WHERE usage_entry_ref=? ORDER BY revision_number DESC",
            (usage["id"],),
        ).fetchall()
        if not cost_rows:
            raise AuthorityResolutionConflict("successful attempt lacks Cost authority")
        cost = _canonical_row(cost_rows[0], json_column="record_json", id_column="cost_entry_id", ref=cost_rows[0]["cost_entry_id"], table="connector_cost_entries")
        metrics = usage.get("metrics")
        if (
            usage["physical_attempt_ref"] != result_attempt["id"]
            or usage["connector_invocation_ref"] != invocation["id"]
            or usage["measurement_status"] != "final"
            or not isinstance(metrics, Mapping)
            or metrics.get("calls") != 1
            or metrics.get("bytes") != len(raw_bytes)
            or metrics.get("records") != len(source["source_record_refs"])
            or cost["usage_entry_ref"] != usage["id"]
            or cost["cost_status"] != "actual"
            or tuple(result["usage_refs"]) != (usage["id"],)
        ):
            raise AuthorityResolutionConflict("Usage/Cost does not measure the exact observed result")
        settlement_rows = self.connectors.connection.execute(
            "SELECT * FROM connector_quota_settlements WHERE reservation_ref=? ORDER BY revision_number DESC",
            (reservation["id"],),
        ).fetchall()
        if not settlement_rows:
            raise AuthorityResolutionConflict("successful attempt lacks quota settlement")
        settlement = _canonical_row(settlement_rows[0], json_column="record_json", id_column="settlement_id", ref=settlement_rows[0]["settlement_id"], table="connector_quota_settlements")
        if (
            settlement["state"] != "consumed"
            or settlement["usage_entry_ref"] != usage["id"]
            or settlement["cost_entry_ref"] != cost["id"]
            or settlement.get("actual") != metrics | {"cost_micros": 0 if cost.get("amount_micros") is None else cost["amount_micros"]}
        ):
            raise AuthorityResolutionConflict("quota settlement is not exact latest Usage/Cost")
        if result["outputs"] != {
            "connector_invocation_ref": invocation["id"],
            "physical_attempt_ref": result_attempt["id"],
            "quota_settlement_ref": settlement["id"],
            "source_envelope_ref": source["id"],
        }:
            raise AuthorityResolutionConflict(
                "ResultEnvelope outputs do not bind exact connector authority"
            )
        response_pairs = (
            (runner_response["runner_request_ref"], actual_request["id"], "runner response request ref"),
            (runner_response["runner_request_hash"], actual_request["content_hash"], "runner response request hash"),
            (runner_response["connector_invocation_ref"], invocation["id"], "runner response invocation ref"),
            (runner_response["connector_invocation_hash"], invocation["content_hash"], "runner response invocation hash"),
            (runner_response["physical_attempt_ref"], result_attempt["id"], "runner response attempt ref"),
            (runner_response["physical_attempt_hash"], result_attempt["content_hash"], "runner response attempt hash"),
            (runner_response["usage_entry_ref"], usage["id"], "runner response usage ref"),
            (runner_response["usage_entry_hash"], usage["content_hash"], "runner response usage hash"),
            (runner_response["cost_entry_ref"], cost["id"], "runner response cost ref"),
            (runner_response["cost_entry_hash"], cost["content_hash"], "runner response cost hash"),
            (runner_response["quota_settlement_ref"], settlement["id"], "runner response settlement ref"),
            (runner_response["quota_settlement_hash"], settlement["content_hash"], "runner response settlement hash"),
            (runner_response["raw_artifact_version_ref"], artifact["id"], "runner response artifact ref"),
            (runner_response["raw_artifact_version_hash"], artifact["content_hash"], "runner response artifact hash"),
            (runner_response["source_envelope_ref"], source["id"], "runner response source ref"),
            (runner_response["source_envelope_hash"], source["content_hash"], "runner response source hash"),
            (runner_response["result_envelope_ref"], result["id"], "runner response result ref"),
            (runner_response["result_envelope_hash"], content_hash(result), "runner response result hash"),
        )
        for actual, expected, name in response_pairs:
            if actual != expected:
                raise AuthorityResolutionConflict(f"{name} is not exact")
        incident = self.connectors.connection.execute(
            "SELECT i.incident_id FROM connector_incidents i "
            "WHERE i.connector_profile_ref=? AND i.severity='blocking' "
            "AND (SELECT e.state FROM connector_incident_events e "
            "WHERE e.incident_ref=i.incident_id ORDER BY e.rowid DESC LIMIT 1)='opened' LIMIT 1",
            (profile["id"],),
        ).fetchone()
        if incident is not None:
            raise AuthorityResolutionConflict("blocking connector incident prevents review")
        health = self.connectors.connection.execute(
            "SELECT state FROM connector_source_health_events WHERE connector_profile_ref=? "
            "ORDER BY rowid DESC LIMIT 1", (profile["id"],)
        ).fetchone()
        if health is not None and health["state"] == "open_circuit":
            raise AuthorityResolutionConflict("connector source health is open_circuit")

        if source["access_policy_ref"] != profile["access_policy_ref"] or source["retention_policy_ref"] != profile["retention_policy_ref"] or source["terms_policy_ref"] != profile["terms_policy_ref"]:
            raise AuthorityResolutionConflict("access/retention/terms policy binding drifted")
        retrieved = source["retrieved_at"]
        for field in ("published_at", "updated_at", "as_of"):
            if source[field] is not None:
                _time_before(source[field], retrieved, field)
        _time_before(execution_obj.started_at, retrieved, "execution.started_at")
        _time_before(result_attempt["started_at"], result_attempt["completed_at"], "physical_attempt.started_at")
        if result_attempt["completed_at"] != retrieved or source["created_at"] != retrieved:
            raise AuthorityResolutionConflict("source/retrieval/result attempt times are not exact")
        if result["created_at"] != retrieved:
            raise AuthorityResolutionConflict("ResultEnvelope created_at does not bind retrieval time")
        _time_after(artifact["created_at"], retrieved, "artifact.created_at")
        _time_after(formal["created_at"], result["created_at"], "formal_result.created_at")
        _time_after(checkpoint["created_at"], receipt["created_at"], "checkpoint.created_at")

        refs = {
            "source_envelope_ref": source["id"], "source_envelope_hash": source["content_hash"],
            "artifact_ref": artifact["id"], "artifact_hash": artifact["content_hash"],
            "source_ref": source["source"], "operation": source["operation"],
            "execution_ref": execution_obj.id, "execution_hash": execution_hash,
            "connector_invocation_ref": invocation["id"], "connector_invocation_hash": invocation["content_hash"],
            "connector_profile_ref": profile["id"], "connector_profile_hash": profile["content_hash"],
            "call_spec_ref": call["id"], "call_spec_hash": call["content_hash"],
            "work_order_ref": work_order["id"], "work_order_hash": work_row["work_order_hash"],
            "result_envelope_ref": result["id"], "result_envelope_hash": content_hash(result),
            "scheduler_attempt_number": int(result_row["attempt_number"]), "physical_attempt_refs": [item["id"] for item in attempts],
            "result_physical_attempt_ref": result_attempt["id"], "reservation_ref": reservation["id"], "reservation_hash": reservation["content_hash"],
            "usage_ref": usage["id"], "usage_hash": usage["content_hash"], "cost_ref": cost["id"], "cost_hash": cost["content_hash"],
            "settlement_ref": settlement["id"], "settlement_hash": settlement["content_hash"],
            "checkpoint_ref": checkpoint["id"], "checkpoint_hash": checkpoint["content_hash"], "plan_ref": plan["id"], "plan_hash": plan["content_hash"],
            "context_pack_ref": context["id"], "context_pack_hash": context["content_hash"], "step_ref": step["id"], "step_hash": step["content_hash"],
            "runner_request_ref": request["id"], "runner_request_hash": request["content_hash"],
            "actual_runner_request_ref": actual_request["id"], "actual_runner_request_hash": actual_request["content_hash"],
            "receipt_ref": receipt["id"], "receipt_hash": receipt["content_hash"],
            "source_record_refs": list(source["source_record_refs"]), "raw_response_hash": source["raw_response_hash"], "source_schema_hash": source["source_schema_hash"], "source_content_hash": source["source_content_hash"],
            "published_at": source["published_at"], "updated_at": source["updated_at"], "as_of": source["as_of"], "retrieved_at": source["retrieved_at"],
            "completeness": source["completeness"], "status": source["status"], "access_policy_ref": source["access_policy_ref"], "retention_policy_ref": source["retention_policy_ref"], "terms_policy_ref": source["terms_policy_ref"],
        }
        summary = {"schema_version": SCHEMA_VERSION, "id": "authority-resolution:" + content_hash(refs), "created_at": source["created_at"], **refs}
        summary["content_hash"] = content_hash(summary)
        summary = validate_authority_resolution(summary)
        records = {
            "source_envelope": source, "artifact": artifact, "profile": profile,
            "call_spec": call, "connector_invocation": invocation, "execution": execution,
            "work_order": work_order, "result_envelope": result,
            "physical_attempt": result_attempt, "physical_attempts": attempts,
            "reservation": reservation, "usage": usage, "cost": cost,
            "settlement": settlement, "checkpoint": checkpoint, "checkpoints": checkpoints,
            "plan": plan, "context_pack": context, "step": step,
            "runner_request": request, "actual_runner_request": actual_request, "receipt": receipt,
            "adapter_request": adapter_request, "observation": observation,
            "runner_response": runner_response,
            "raw_object": raw_object,
        }
        return ResolvedAuthority(summary=summary, records=records, raw_bytes=raw_bytes, raw_payload=raw_payload)


__all__ = [
    "AuthorityResolutionConflict", "AuthorityResolutionError", "ConnectorAuthorityResolver",
    "ResolvedAuthority", "validate_authority_resolution",
]
