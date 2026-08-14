"""Append-only workflow, usage, cost, and artifact authorities.

This module records source facts for future read-only projections. It does not
render a dashboard, aggregate different currencies, fetch artifact content, or
turn SQLite triggers into a hostile-process security claim.
"""

from __future__ import annotations

import json
import re
import sqlite3
import uuid
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from fractions import Fraction
from pathlib import Path
from typing import Any

from .store import canonical_json, content_hash


SCHEMA_VERSION = "0.1"
_SCHEMA_PATH = Path(__file__).with_name("observability_schema.sql")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_CURRENCY_RE = re.compile(r"^[A-Z]{3}$")
_RELATIONS = frozenset({"decomposed_from", "verifies", "follows_up"})
_METERING_SOURCES = frozenset(
    {"provider_reported", "launcher_measured", "worker_reported", "estimated"}
)
_MEASUREMENT_STATUSES = frozenset({"final", "partial", "estimated", "unavailable"})
_CHARGE_TYPES = frozenset(
    {
        "input_tokens",
        "output_tokens",
        "reasoning_tokens",
        "cache_read_tokens",
        "cache_write_tokens",
        "request",
        "duration_ms",
        "input_bytes",
        "output_bytes",
        "custom",
    }
)
_COST_STATUSES = frozenset({"actual", "estimated", "unpriced", "waived"})
_ACCESS_CLASSES = frozenset({"public", "internal", "restricted"})
_PREVIEW_STATUSES = frozenset({"unavailable", "available", "redacted"})
_USAGE_METRICS = (
    "input_tokens",
    "output_tokens",
    "reasoning_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "total_tokens",
    "requests",
    "duration_ms",
    "input_bytes",
    "output_bytes",
)


class ObservabilityError(Exception):
    """Base error for observability authority operations."""


class ObservabilityValidationError(ObservabilityError):
    pass


class ObservabilityConflict(ObservabilityError):
    pass


class ObservabilityNotFound(ObservabilityError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _id(value: Any, name: str) -> str:
    if value is None:
        return uuid.uuid4().hex
    return _nonempty(value, name)


def _nonempty(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ObservabilityValidationError(f"{name} must be a non-empty string")
    return value


def _optional_ref(value: Any, name: str) -> str | None:
    if value is None:
        return None
    return _nonempty(value, name)


def _sha256(value: Any, name: str) -> str:
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        raise ObservabilityValidationError(
            f"{name} must be 64 lowercase SHA-256 hex characters"
        )
    return value


def _currency(value: Any) -> str:
    if not isinstance(value, str) or not _CURRENCY_RE.fullmatch(value):
        raise ObservabilityValidationError("currency must be an uppercase ISO-4217 code")
    return value


def _timestamp(value: Any, name: str) -> str:
    if not isinstance(value, str):
        raise ObservabilityValidationError(f"{name} must be RFC3339")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ObservabilityValidationError(f"{name} must be RFC3339") from exc
    if parsed.tzinfo is None:
        raise ObservabilityValidationError(f"{name} must include timezone")
    return parsed.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _strings(value: Any, name: str, *, allow_empty: bool = True) -> list[str]:
    if not isinstance(value, (list, tuple)):
        raise ObservabilityValidationError(f"{name} must be an array")
    result = list(value)
    if not allow_empty and not result:
        raise ObservabilityValidationError(f"{name} must not be empty")
    if not all(isinstance(item, str) and item for item in result):
        raise ObservabilityValidationError(f"{name} must contain non-empty strings")
    if len(result) != len(set(result)):
        raise ObservabilityValidationError(f"{name} must not contain duplicates")
    return result


def _json_object(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ObservabilityValidationError(f"{name} must be an object")
    try:
        return json.loads(canonical_json(value))
    except (TypeError, ValueError) as exc:
        raise ObservabilityValidationError(f"{name} must be finite JSON") from exc


def _nonnegative_int(value: Any, name: str, *, nullable: bool = False) -> int | None:
    if value is None and nullable:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        suffix = " or null" if nullable else ""
        raise ObservabilityValidationError(f"{name} must be a non-negative integer{suffix}")
    return value


def _positive_int(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ObservabilityValidationError(f"{name} must be a positive integer")
    return value


def _record(base: Mapping[str, Any]) -> dict[str, Any]:
    wire = dict(base)
    wire["content_hash"] = content_hash(wire)
    return wire


class ObservabilityStore:
    """Own observability authority tables on an existing ``DaltonStore``.

    The supplied store owns authentication, connection, and transaction
    boundaries. Callers outside trusted Core must not receive this connection
    or the database path; they will later consume a separate read projection.
    """

    def __init__(self, store: Any):
        if not hasattr(store, "connection") or not hasattr(store, "_transaction"):
            raise TypeError("ObservabilityStore requires a DaltonStore")
        self.store = store
        self.connection: sqlite3.Connection = store.connection
        self.connection.executescript(_SCHEMA_PATH.read_text(encoding="utf-8"))
        self._backfill_artifact_version_index()

    @property
    def conn(self) -> sqlite3.Connection:
        return self.connection

    @staticmethod
    def _row_record(row: sqlite3.Row | None, name: str) -> dict[str, Any]:
        if row is None:
            raise ObservabilityNotFound(f"{name} not found")
        return json.loads(row["record_json"])

    def _backfill_artifact_version_index(self) -> None:
        """Add an immutable cross-generation index without rewriting artifacts."""

        rows = self.connection.execute(
            "SELECT version_id,artifact_ref,version_number,prior_version_id AS prior_version_ref,"
            "producer_invocation_ref AS producer_execution_ref,content_hash,created_at,'0.1' AS schema_version "
            "FROM observability_artifact_versions "
            "UNION ALL "
            "SELECT version_id,artifact_ref,version_number,prior_version_ref,producer_execution_ref,"
            "content_hash,created_at,'0.2' AS schema_version "
            "FROM observability_artifact_versions_v2 "
            "ORDER BY artifact_ref,version_number"
        ).fetchall()
        missing = [
            row
            for row in rows
            if self.connection.execute(
                "SELECT 1 FROM observability_artifact_version_index WHERE version_id=?",
                (row["version_id"],),
            ).fetchone()
            is None
        ]
        if not missing:
            return
        with self.store._transaction() as cur:
            for row in missing:
                existing = cur.execute(
                    "SELECT version_id,schema_version,record_hash FROM observability_artifact_version_index "
                    "WHERE artifact_ref=? AND version_number=?",
                    (row["artifact_ref"], row["version_number"]),
                ).fetchone()
                if existing is not None:
                    raise ObservabilityConflict(
                        "artifact version index conflicts with historical authority"
                    )
                if row["prior_version_ref"] is not None and cur.execute(
                    "SELECT 1 FROM observability_artifact_version_index WHERE version_id=?",
                    (row["prior_version_ref"],),
                ).fetchone() is None:
                    raise ObservabilityConflict(
                        "artifact version index cannot resolve historical prior version"
                    )
                cur.execute(
                    "INSERT INTO observability_artifact_version_index"
                    "(version_id,artifact_ref,version_number,schema_version,prior_version_ref,"
                    "producer_execution_ref,record_hash,created_at) VALUES(?,?,?,?,?,?,?,?)",
                    (
                        row["version_id"],
                        row["artifact_ref"],
                        row["version_number"],
                        row["schema_version"],
                        row["prior_version_ref"],
                        row["producer_execution_ref"],
                        row["content_hash"],
                        row["created_at"],
                    ),
                )

    def _idempotency(
        self,
        cur: sqlite3.Cursor,
        key: str | None,
        operation: str,
        request_hash: str,
    ) -> dict[str, Any] | None:
        if key is None:
            return None
        _nonempty(key, "idempotency_key")
        row = cur.execute(
            "SELECT * FROM observability_idempotency_keys WHERE idempotency_key=?", (key,)
        ).fetchone()
        if row is None:
            return None
        if row["operation"] != operation or row["request_hash"] != request_hash:
            return {
                "status": "conflict",
                "idempotency_key": key,
                "request_hash": request_hash,
                "existing_request_hash": row["request_hash"],
            }
        result = json.loads(row["result_json"])
        return {**result, "status": "duplicate", "idempotency_key": key}

    def _save_idempotency(
        self,
        cur: sqlite3.Cursor,
        key: str | None,
        operation: str,
        request_hash: str,
        result: Mapping[str, Any],
    ) -> None:
        if key is None:
            return
        cur.execute(
            "INSERT INTO observability_idempotency_keys"
            "(idempotency_key,operation,request_hash,result_json,created_at) VALUES(?,?,?,?,?)",
            (key, operation, request_hash, canonical_json(result), _now()),
        )

    @staticmethod
    def _explicit_id_for_retry(value: Any, key: str | None, name: str) -> str:
        if key is not None and value is None:
            raise ObservabilityValidationError(
                f"{name} is required when idempotency_key is supplied"
            )
        return _id(value, name)

    def create_workflow_version(
        self,
        workflow_ref: str,
        *,
        title: str,
        objective: str,
        scope_refs: Sequence[str] = (),
        root_work_order_refs: Sequence[str] = (),
        governance_policy_ref: str | None = None,
        actor_ref: str,
        prior_version_ref: str | None = None,
        version_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        workflow_ref = _nonempty(workflow_ref, "workflow_ref")
        version_id = self._explicit_id_for_retry(version_id, idempotency_key, "version_id")
        title = _nonempty(title, "title")
        objective = _nonempty(objective, "objective")
        scopes = _strings(scope_refs, "scope_refs")
        roots = _strings(root_work_order_refs, "root_work_order_refs")
        governance_policy_ref = _optional_ref(governance_policy_ref, "governance_policy_ref")
        actor_ref = _nonempty(actor_ref, "actor_ref")
        prior_version_ref = _optional_ref(prior_version_ref, "prior_version_ref")
        request = {
            "operation": "create_workflow_version",
            "version_id": version_id,
            "workflow_ref": workflow_ref,
            "title": title,
            "objective": objective,
            "scope_refs": scopes,
            "root_work_order_refs": roots,
            "governance_policy_ref": governance_policy_ref,
            "actor_ref": actor_ref,
            "prior_version_ref": prior_version_ref,
        }
        request_hash = content_hash(request)
        with self.store._transaction() as cur:
            idem = self._idempotency(
                cur, idempotency_key, "create_workflow_version", request_hash
            )
            if idem is not None:
                return idem
            if cur.execute(
                "SELECT 1 FROM observability_workflow_versions WHERE version_id=?",
                (version_id,),
            ).fetchone():
                raise ObservabilityConflict(f"workflow version id collision: {version_id}")
            latest = cur.execute(
                "SELECT * FROM observability_workflow_versions WHERE workflow_ref=? "
                "ORDER BY version_number DESC LIMIT 1",
                (workflow_ref,),
            ).fetchone()
            if latest is None:
                if prior_version_ref is not None:
                    raise ObservabilityConflict("first workflow version cannot have a prior version")
                version_number = 1
            else:
                if prior_version_ref != latest["version_id"]:
                    raise ObservabilityConflict(
                        "workflow version must continue the latest immutable version"
                    )
                version_number = int(latest["version_number"]) + 1
            for root_ref in roots:
                if cur.execute(
                    "SELECT 1 FROM observability_work_order_links "
                    "WHERE workflow_ref=? AND child_work_order_ref=?",
                    (workflow_ref, root_ref),
                ).fetchone():
                    raise ObservabilityConflict(
                        f"root work order already has a parent: {root_ref}"
                    )
            structural_roots = {
                row["parent_work_order_ref"]
                for row in cur.execute(
                    "SELECT DISTINCT parent.parent_work_order_ref "
                    "FROM observability_work_order_links AS parent "
                    "WHERE parent.workflow_ref=? AND NOT EXISTS ("
                    " SELECT 1 FROM observability_work_order_links AS child"
                    " WHERE child.workflow_ref=parent.workflow_ref"
                    " AND child.child_work_order_ref=parent.parent_work_order_ref"
                    ")",
                    (workflow_ref,),
                ).fetchall()
            }
            if not structural_roots.issubset(set(roots)):
                raise ObservabilityConflict(
                    "workflow version cannot remove a root that anchors existing links"
                )
            created_at = _now()
            wire = _record(
                {
                    "schema_version": SCHEMA_VERSION,
                    "id": version_id,
                    "created_at": created_at,
                    "workflow_ref": workflow_ref,
                    "version": version_number,
                    "title": title,
                    "objective": objective,
                    "scope_refs": scopes,
                    "root_work_order_refs": roots,
                    "governance_policy_ref": governance_policy_ref,
                    "actor_ref": actor_ref,
                    "prior_version_ref": prior_version_ref,
                }
            )
            cur.execute(
                "INSERT INTO observability_workflow_versions"
                "(version_id,workflow_ref,version_number,prior_version_id,root_work_order_refs_json,record_json,content_hash,created_at) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (
                    version_id,
                    workflow_ref,
                    version_number,
                    prior_version_ref,
                    canonical_json(roots),
                    canonical_json(wire),
                    wire["content_hash"],
                    created_at,
                ),
            )
            result = {"status": "fresh", **wire}
            self._save_idempotency(
                cur, idempotency_key, "create_workflow_version", request_hash, result
            )
            return {**result, **({"idempotency_key": idempotency_key} if idempotency_key else {})}

    def link_work_order(
        self,
        workflow_ref: str,
        parent_work_order_ref: str,
        child_work_order_ref: str,
        *,
        relation: str = "decomposed_from",
        sequence: int = 0,
        actor_ref: str,
        link_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        workflow_ref = _nonempty(workflow_ref, "workflow_ref")
        parent = _nonempty(parent_work_order_ref, "parent_work_order_ref")
        child = _nonempty(child_work_order_ref, "child_work_order_ref")
        if parent == child:
            raise ObservabilityConflict("a work order cannot be its own parent")
        if relation not in _RELATIONS:
            raise ObservabilityValidationError("relation is invalid")
        sequence = _nonnegative_int(sequence, "sequence")
        actor_ref = _nonempty(actor_ref, "actor_ref")
        link_id = self._explicit_id_for_retry(link_id, idempotency_key, "link_id")
        request = {
            "operation": "link_work_order",
            "link_id": link_id,
            "workflow_ref": workflow_ref,
            "parent_work_order_ref": parent,
            "child_work_order_ref": child,
            "relation": relation,
            "sequence": sequence,
            "actor_ref": actor_ref,
        }
        request_hash = content_hash(request)
        with self.store._transaction() as cur:
            idem = self._idempotency(cur, idempotency_key, "link_work_order", request_hash)
            if idem is not None:
                return idem
            latest = cur.execute(
                "SELECT root_work_order_refs_json FROM observability_workflow_versions "
                "WHERE workflow_ref=? ORDER BY version_number DESC LIMIT 1",
                (workflow_ref,),
            ).fetchone()
            if latest is None:
                raise ObservabilityNotFound(f"workflow not found: {workflow_ref}")
            if child in json.loads(latest["root_work_order_refs_json"]):
                raise ObservabilityConflict("a declared root work order cannot acquire a parent")
            current_roots = set(json.loads(latest["root_work_order_refs_json"]))
            known_parent = parent in current_roots or cur.execute(
                "SELECT 1 FROM observability_work_order_links "
                "WHERE workflow_ref=? AND child_work_order_ref=?",
                (workflow_ref, parent),
            ).fetchone()
            if not known_parent:
                raise ObservabilityConflict(
                    "link parent must be a declared root or an existing child node"
                )
            existing_parent = cur.execute(
                "SELECT parent_work_order_ref FROM observability_work_order_links "
                "WHERE workflow_ref=? AND child_work_order_ref=?",
                (workflow_ref, child),
            ).fetchone()
            if existing_parent is not None:
                raise ObservabilityConflict("a work order cannot have multiple parents")
            creates_cycle = cur.execute(
                "WITH RECURSIVE ancestors(node) AS ("
                " SELECT ? UNION ALL"
                " SELECT links.parent_work_order_ref FROM observability_work_order_links AS links"
                " JOIN ancestors ON links.child_work_order_ref=ancestors.node"
                " WHERE links.workflow_ref=?"
                ") SELECT 1 FROM ancestors WHERE node=? LIMIT 1",
                (parent, workflow_ref, child),
            ).fetchone()
            if creates_cycle is not None:
                raise ObservabilityConflict("work-order link would create a cycle")
            if cur.execute(
                "SELECT 1 FROM observability_work_order_links WHERE link_id=?", (link_id,)
            ).fetchone():
                raise ObservabilityConflict(f"work-order link id collision: {link_id}")
            created_at = _now()
            wire = _record(
                {
                    "schema_version": SCHEMA_VERSION,
                    "id": link_id,
                    "created_at": created_at,
                    "workflow_ref": workflow_ref,
                    "parent_work_order_ref": parent,
                    "child_work_order_ref": child,
                    "relation": relation,
                    "sequence": sequence,
                    "actor_ref": actor_ref,
                }
            )
            cur.execute(
                "INSERT INTO observability_work_order_links"
                "(link_id,workflow_ref,parent_work_order_ref,child_work_order_ref,relation,sequence_number,record_json,content_hash,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    link_id,
                    workflow_ref,
                    parent,
                    child,
                    relation,
                    sequence,
                    canonical_json(wire),
                    wire["content_hash"],
                    created_at,
                ),
            )
            result = {"status": "fresh", **wire}
            self._save_idempotency(cur, idempotency_key, "link_work_order", request_hash, result)
            return {**result, **({"idempotency_key": idempotency_key} if idempotency_key else {})}

    def record_usage(
        self,
        invocation_ref: str,
        *,
        occurred_at: str,
        metering_source: str,
        measurement_status: str,
        raw_usage: Mapping[str, Any],
        workflow_ref: str | None = None,
        provider_usage_ref: str | None = None,
        correction_of_ref: str | None = None,
        actor_ref: str,
        entry_id: str | None = None,
        idempotency_key: str | None = None,
        **metrics: Any,
    ) -> dict[str, Any]:
        invocation_ref = _nonempty(invocation_ref, "invocation_ref")
        occurred_at = _timestamp(occurred_at, "occurred_at")
        if metering_source not in _METERING_SOURCES:
            raise ObservabilityValidationError("metering_source is invalid")
        if measurement_status not in _MEASUREMENT_STATUSES:
            raise ObservabilityValidationError("measurement_status is invalid")
        unknown_metrics = set(metrics) - set(_USAGE_METRICS)
        if unknown_metrics:
            raise ObservabilityValidationError(
                f"unknown usage metric(s): {sorted(unknown_metrics)}"
            )
        normalized_metrics = {
            name: _nonnegative_int(metrics.get(name), name, nullable=True)
            for name in _USAGE_METRICS
        }
        if measurement_status == "unavailable":
            if any(value is not None for value in normalized_metrics.values()):
                raise ObservabilityValidationError(
                    "unavailable usage must keep every unknown metric null"
                )
        elif all(value is None for value in normalized_metrics.values()):
            raise ObservabilityValidationError(
                "measured usage must contain at least one normalized metric"
            )
        if all(
            normalized_metrics[name] is not None
            for name in ("input_tokens", "output_tokens", "total_tokens")
        ) and normalized_metrics["total_tokens"] != (
            normalized_metrics["input_tokens"] + normalized_metrics["output_tokens"]
        ):
            raise ObservabilityValidationError(
                "total_tokens must equal input_tokens plus output_tokens when all are known"
            )
        raw_usage = _json_object(raw_usage, "raw_usage")
        workflow_ref = _optional_ref(workflow_ref, "workflow_ref")
        provider_usage_ref = _optional_ref(provider_usage_ref, "provider_usage_ref")
        correction_of_ref = _optional_ref(correction_of_ref, "correction_of_ref")
        actor_ref = _nonempty(actor_ref, "actor_ref")
        entry_id = self._explicit_id_for_retry(entry_id, idempotency_key, "entry_id")
        request = {
            "operation": "record_usage",
            "entry_id": entry_id,
            "invocation_ref": invocation_ref,
            "occurred_at": occurred_at,
            "metering_source": metering_source,
            "measurement_status": measurement_status,
            "raw_usage": raw_usage,
            "workflow_ref": workflow_ref,
            "provider_usage_ref": provider_usage_ref,
            "correction_of_ref": correction_of_ref,
            "actor_ref": actor_ref,
            **normalized_metrics,
        }
        request_hash = content_hash(request)
        with self.store._transaction() as cur:
            idem = self._idempotency(cur, idempotency_key, "record_usage", request_hash)
            if idem is not None:
                return idem
            invocation = cur.execute(
                "SELECT * FROM model_invocations WHERE invocation_id=?", (invocation_ref,)
            ).fetchone()
            if invocation is None:
                raise ObservabilityNotFound(f"invocation not found: {invocation_ref}")
            latest = cur.execute(
                "SELECT * FROM observability_usage_entries WHERE invocation_ref=? "
                "ORDER BY revision_number DESC LIMIT 1",
                (invocation_ref,),
            ).fetchone()
            if latest is None:
                if correction_of_ref is not None:
                    raise ObservabilityConflict("first usage entry cannot be a correction")
                revision = 1
            else:
                if correction_of_ref != latest["usage_entry_id"]:
                    raise ObservabilityConflict(
                        "usage correction must continue the latest immutable entry"
                    )
                if workflow_ref != latest["workflow_ref"]:
                    raise ObservabilityConflict(
                        "usage correction cannot change workflow attribution"
                    )
                revision = int(latest["revision_number"]) + 1
            if cur.execute(
                "SELECT 1 FROM observability_usage_entries WHERE usage_entry_id=?", (entry_id,)
            ).fetchone():
                raise ObservabilityConflict(f"usage entry id collision: {entry_id}")
            invocation_wire = json.loads(invocation["invocation_json"])
            if workflow_ref is not None:
                workflow = cur.execute(
                    "SELECT root_work_order_refs_json FROM observability_workflow_versions "
                    "WHERE workflow_ref=? ORDER BY version_number DESC LIMIT 1",
                    (workflow_ref,),
                ).fetchone()
                if workflow is None:
                    raise ObservabilityNotFound(f"workflow not found: {workflow_ref}")
                work_order_ref = invocation_wire["work_order_ref"]
                belongs = work_order_ref in json.loads(
                    workflow["root_work_order_refs_json"]
                ) or cur.execute(
                    "SELECT 1 FROM observability_work_order_links "
                    "WHERE workflow_ref=? AND (parent_work_order_ref=? OR child_work_order_ref=?)",
                    (workflow_ref, work_order_ref, work_order_ref),
                ).fetchone()
                if not belongs:
                    raise ObservabilityConflict(
                        "invocation work order does not belong to the declared workflow tree"
                    )
            created_at = _now()
            wire = _record(
                {
                    "schema_version": SCHEMA_VERSION,
                    "id": entry_id,
                    "created_at": created_at,
                    "invocation_ref": invocation_ref,
                    "work_order_ref": invocation_wire["work_order_ref"],
                    "workflow_ref": workflow_ref,
                    "revision": revision,
                    "occurred_at": occurred_at,
                    "provider": invocation_wire["provider"],
                    "model": invocation_wire["model"],
                    "model_family": invocation_wire["model_family"],
                    "profile_ref": invocation_wire["profile_ref"],
                    "runtime_ref": invocation_wire["runtime_ref"],
                    "capability": invocation_wire["capability"],
                    "granularity": invocation_wire["granularity"],
                    **normalized_metrics,
                    "raw_usage": raw_usage,
                    "provider_usage_ref": provider_usage_ref,
                    "metering_source": metering_source,
                    "measurement_status": measurement_status,
                    "correction_of_ref": correction_of_ref,
                    "actor_ref": actor_ref,
                }
            )
            cur.execute(
                "INSERT INTO observability_usage_entries"
                "(usage_entry_id,invocation_ref,work_order_ref,workflow_ref,revision_number,correction_of_ref,occurred_at,metering_source,measurement_status,record_json,content_hash,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    entry_id,
                    invocation_ref,
                    invocation_wire["work_order_ref"],
                    workflow_ref,
                    revision,
                    correction_of_ref,
                    occurred_at,
                    metering_source,
                    measurement_status,
                    canonical_json(wire),
                    wire["content_hash"],
                    created_at,
                ),
            )
            result = {"status": "fresh", **wire}
            self._save_idempotency(cur, idempotency_key, "record_usage", request_hash, result)
            return {**result, **({"idempotency_key": idempotency_key} if idempotency_key else {})}

    def create_price_rate_version(
        self,
        price_rate_ref: str,
        *,
        provider: str,
        model: str,
        charge_type: str,
        unit_quantity: int,
        unit_price_micros: int,
        currency: str,
        effective_from: str,
        effective_until: str | None,
        source_ref: str,
        actor_ref: str,
        prior_version_ref: str | None = None,
        version_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        price_rate_ref = _nonempty(price_rate_ref, "price_rate_ref")
        version_id = self._explicit_id_for_retry(version_id, idempotency_key, "version_id")
        provider = _nonempty(provider, "provider")
        model = _nonempty(model, "model")
        if charge_type not in _CHARGE_TYPES:
            raise ObservabilityValidationError("charge_type is invalid")
        unit_quantity = _positive_int(unit_quantity, "unit_quantity")
        unit_price_micros = _nonnegative_int(unit_price_micros, "unit_price_micros")
        currency = _currency(currency)
        effective_from = _timestamp(effective_from, "effective_from")
        effective_until = (
            None if effective_until is None else _timestamp(effective_until, "effective_until")
        )
        if effective_until is not None and effective_until <= effective_from:
            raise ObservabilityValidationError("effective_until must be after effective_from")
        source_ref = _nonempty(source_ref, "source_ref")
        actor_ref = _nonempty(actor_ref, "actor_ref")
        prior_version_ref = _optional_ref(prior_version_ref, "prior_version_ref")
        request = {
            "operation": "create_price_rate_version",
            "version_id": version_id,
            "price_rate_ref": price_rate_ref,
            "provider": provider,
            "model": model,
            "charge_type": charge_type,
            "unit_quantity": unit_quantity,
            "unit_price_micros": unit_price_micros,
            "currency": currency,
            "effective_from": effective_from,
            "effective_until": effective_until,
            "source_ref": source_ref,
            "actor_ref": actor_ref,
            "prior_version_ref": prior_version_ref,
        }
        request_hash = content_hash(request)
        with self.store._transaction() as cur:
            idem = self._idempotency(
                cur, idempotency_key, "create_price_rate_version", request_hash
            )
            if idem is not None:
                return idem
            latest = cur.execute(
                "SELECT * FROM observability_price_rate_versions WHERE price_rate_ref=? "
                "ORDER BY version_number DESC LIMIT 1",
                (price_rate_ref,),
            ).fetchone()
            if latest is None:
                if prior_version_ref is not None:
                    raise ObservabilityConflict("first price-rate version cannot have a prior version")
                version = 1
            else:
                if prior_version_ref != latest["version_id"]:
                    raise ObservabilityConflict(
                        "price-rate version must continue the latest immutable version"
                    )
                prior = json.loads(latest["record_json"])
                for field, value in (
                    ("provider", provider),
                    ("model", model),
                    ("charge_type", charge_type),
                    ("unit_quantity", unit_quantity),
                    ("currency", currency),
                ):
                    if prior[field] != value:
                        raise ObservabilityConflict(
                            f"price-rate identity field cannot change: {field}"
                        )
                if effective_from <= prior["effective_from"]:
                    raise ObservabilityConflict(
                        "new price-rate version must have a later effective_from"
                    )
                version = int(latest["version_number"]) + 1
            if cur.execute(
                "SELECT 1 FROM observability_price_rate_versions WHERE version_id=?",
                (version_id,),
            ).fetchone():
                raise ObservabilityConflict(f"price-rate version id collision: {version_id}")
            created_at = _now()
            wire = _record(
                {
                    "schema_version": SCHEMA_VERSION,
                    "id": version_id,
                    "created_at": created_at,
                    "price_rate_ref": price_rate_ref,
                    "version": version,
                    "provider": provider,
                    "model": model,
                    "charge_type": charge_type,
                    "unit_quantity": unit_quantity,
                    "unit_price_micros": unit_price_micros,
                    "currency": currency,
                    "effective_from": effective_from,
                    "effective_until": effective_until,
                    "source_ref": source_ref,
                    "actor_ref": actor_ref,
                    "prior_version_ref": prior_version_ref,
                }
            )
            cur.execute(
                "INSERT INTO observability_price_rate_versions"
                "(version_id,price_rate_ref,version_number,prior_version_id,provider,model,charge_type,unit_quantity,unit_price_micros,currency,effective_from,effective_until,record_json,content_hash,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    version_id,
                    price_rate_ref,
                    version,
                    prior_version_ref,
                    provider,
                    model,
                    charge_type,
                    unit_quantity,
                    unit_price_micros,
                    currency,
                    effective_from,
                    effective_until,
                    canonical_json(wire),
                    wire["content_hash"],
                    created_at,
                ),
            )
            result = {"status": "fresh", **wire}
            self._save_idempotency(
                cur, idempotency_key, "create_price_rate_version", request_hash, result
            )
            return {**result, **({"idempotency_key": idempotency_key} if idempotency_key else {})}

    def record_cost(
        self,
        usage_entry_ref: str,
        *,
        price_rate_refs: Sequence[str],
        amount_micros: int | None,
        currency: str,
        cost_status: str,
        calculation_ref: str,
        actor_ref: str,
        correction_of_ref: str | None = None,
        cost_entry_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        usage_entry_ref = _nonempty(usage_entry_ref, "usage_entry_ref")
        refs = _strings(price_rate_refs, "price_rate_refs")
        currency = _currency(currency)
        if cost_status not in _COST_STATUSES:
            raise ObservabilityValidationError("cost_status is invalid")
        amount_micros = _nonnegative_int(
            amount_micros, "amount_micros", nullable=True
        )
        if cost_status in {"actual", "estimated"}:
            if amount_micros is None or not refs:
                raise ObservabilityValidationError(
                    "priced cost requires integer amount_micros and exact price_rate_refs"
                )
        elif cost_status == "unpriced":
            if amount_micros is not None or refs:
                raise ObservabilityValidationError(
                    "unpriced cost must have null amount and no price-rate refs"
                )
        elif cost_status == "waived" and amount_micros != 0:
            raise ObservabilityValidationError("waived cost must have amount_micros=0")
        calculation_ref = _nonempty(calculation_ref, "calculation_ref")
        actor_ref = _nonempty(actor_ref, "actor_ref")
        correction_of_ref = _optional_ref(correction_of_ref, "correction_of_ref")
        cost_entry_id = self._explicit_id_for_retry(
            cost_entry_id, idempotency_key, "cost_entry_id"
        )
        request = {
            "operation": "record_cost",
            "cost_entry_id": cost_entry_id,
            "usage_entry_ref": usage_entry_ref,
            "price_rate_refs": refs,
            "amount_micros": amount_micros,
            "currency": currency,
            "cost_status": cost_status,
            "calculation_ref": calculation_ref,
            "actor_ref": actor_ref,
            "correction_of_ref": correction_of_ref,
        }
        request_hash = content_hash(request)
        with self.store._transaction() as cur:
            idem = self._idempotency(cur, idempotency_key, "record_cost", request_hash)
            if idem is not None:
                return idem
            usage = cur.execute(
                "SELECT * FROM observability_usage_entries WHERE usage_entry_id=?",
                (usage_entry_ref,),
            ).fetchone()
            if usage is None:
                raise ObservabilityNotFound(f"usage entry not found: {usage_entry_ref}")
            usage_wire = json.loads(usage["record_json"])
            calculated = Fraction(0, 1)
            seen_charge_types: set[str] = set()
            for rate_ref in refs:
                rate = cur.execute(
                    "SELECT * FROM observability_price_rate_versions WHERE version_id=?",
                    (rate_ref,),
                ).fetchone()
                if rate is None:
                    raise ObservabilityNotFound(f"price-rate version not found: {rate_ref}")
                rate_wire = json.loads(rate["record_json"])
                if rate_wire["charge_type"] in seen_charge_types:
                    raise ObservabilityConflict(
                        "one cost entry cannot charge the same usage metric twice"
                    )
                seen_charge_types.add(rate_wire["charge_type"])
                if rate_wire["currency"] != currency:
                    raise ObservabilityConflict(
                        "cost cannot combine price rates from different currencies"
                    )
                if (
                    rate_wire["provider"] != usage_wire["provider"]
                    or rate_wire["model"] != usage_wire["model"]
                ):
                    raise ObservabilityConflict(
                        "price rate does not match the immutable usage model identity"
                    )
                occurred = usage_wire["occurred_at"]
                if occurred < rate_wire["effective_from"] or (
                    rate_wire["effective_until"] is not None
                    and occurred >= rate_wire["effective_until"]
                ):
                    raise ObservabilityConflict(
                        "price-rate version was not effective when usage occurred"
                    )
                charge_type = rate_wire["charge_type"]
                if charge_type == "custom":
                    raise ObservabilityConflict(
                        "custom rates require a future versioned calculation contract"
                    )
                metric_field = "requests" if charge_type == "request" else charge_type
                metric = usage_wire[metric_field]
                if metric is None:
                    raise ObservabilityConflict(
                        f"price rate requires unavailable usage metric: {charge_type}"
                    )
                calculated += Fraction(
                    metric * rate_wire["unit_price_micros"],
                    rate_wire["unit_quantity"],
                )
            if cost_status in {"actual", "estimated"}:
                # Positive monetary values are rounded to the nearest integer
                # micro-unit, with exact halves rounded up. The stored contract
                # names this rule so historical replay never depends on a
                # caller's language/runtime defaults.
                expected_amount = (
                    2 * calculated.numerator + calculated.denominator
                ) // (2 * calculated.denominator)
                if amount_micros != expected_amount:
                    raise ObservabilityConflict(
                        "amount_micros does not match immutable usage and price-rate versions"
                    )
            latest = cur.execute(
                "SELECT * FROM observability_cost_entries WHERE usage_entry_ref=? "
                "ORDER BY revision_number DESC LIMIT 1",
                (usage_entry_ref,),
            ).fetchone()
            if latest is None:
                if correction_of_ref is not None:
                    raise ObservabilityConflict("first cost entry cannot be a correction")
                revision = 1
            else:
                if correction_of_ref != latest["cost_entry_id"]:
                    raise ObservabilityConflict(
                        "cost correction must continue the latest immutable entry"
                    )
                revision = int(latest["revision_number"]) + 1
            if cur.execute(
                "SELECT 1 FROM observability_cost_entries WHERE cost_entry_id=?",
                (cost_entry_id,),
            ).fetchone():
                raise ObservabilityConflict(f"cost entry id collision: {cost_entry_id}")
            created_at = _now()
            wire = _record(
                {
                    "schema_version": SCHEMA_VERSION,
                    "id": cost_entry_id,
                    "created_at": created_at,
                    "usage_entry_ref": usage_entry_ref,
                    "revision": revision,
                    "price_rate_refs": refs,
                    "amount_micros": amount_micros,
                    "currency": currency,
                    "cost_status": cost_status,
                    "calculation_ref": calculation_ref,
                    "rounding_mode": "half_up",
                    "correction_of_ref": correction_of_ref,
                    "actor_ref": actor_ref,
                }
            )
            cur.execute(
                "INSERT INTO observability_cost_entries"
                "(cost_entry_id,usage_entry_ref,revision_number,correction_of_ref,amount_micros,currency,cost_status,record_json,content_hash,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    cost_entry_id,
                    usage_entry_ref,
                    revision,
                    correction_of_ref,
                    amount_micros,
                    currency,
                    cost_status,
                    canonical_json(wire),
                    wire["content_hash"],
                    created_at,
                ),
            )
            result = {"status": "fresh", **wire}
            self._save_idempotency(cur, idempotency_key, "record_cost", request_hash, result)
            return {**result, **({"idempotency_key": idempotency_key} if idempotency_key else {})}

    def register_artifact_version(
        self,
        artifact_ref: str,
        *,
        title: str,
        kind: str,
        media_type: str,
        artifact_content_hash: str,
        size_bytes: int,
        storage_locator: str,
        producer_invocation_ref: str,
        result_envelope_ref: str,
        result_envelope_hash: str,
        access_class: str,
        preview_status: str,
        actor_ref: str,
        prior_version_ref: str | None = None,
        version_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        artifact_ref = _nonempty(artifact_ref, "artifact_ref")
        version_id = self._explicit_id_for_retry(version_id, idempotency_key, "version_id")
        title = _nonempty(title, "title")
        kind = _nonempty(kind, "kind")
        media_type = _nonempty(media_type, "media_type")
        artifact_content_hash = _sha256(
            artifact_content_hash, "artifact_content_hash"
        )
        size_bytes = _nonnegative_int(size_bytes, "size_bytes")
        storage_locator = _nonempty(storage_locator, "storage_locator")
        if storage_locator.lower().startswith("data:") or any(
            ord(char) < 32 for char in storage_locator
        ):
            raise ObservabilityValidationError(
                "storage_locator must identify metadata, not embed artifact content"
            )
        producer_invocation_ref = _nonempty(
            producer_invocation_ref, "producer_invocation_ref"
        )
        result_envelope_ref = _nonempty(result_envelope_ref, "result_envelope_ref")
        result_envelope_hash = _sha256(result_envelope_hash, "result_envelope_hash")
        if access_class not in _ACCESS_CLASSES:
            raise ObservabilityValidationError("access_class is invalid")
        if preview_status not in _PREVIEW_STATUSES:
            raise ObservabilityValidationError("preview_status is invalid")
        actor_ref = _nonempty(actor_ref, "actor_ref")
        prior_version_ref = _optional_ref(prior_version_ref, "prior_version_ref")
        request = {
            "operation": "register_artifact_version",
            "version_id": version_id,
            "artifact_ref": artifact_ref,
            "title": title,
            "kind": kind,
            "media_type": media_type,
            "artifact_content_hash": artifact_content_hash,
            "size_bytes": size_bytes,
            "storage_locator": storage_locator,
            "producer_invocation_ref": producer_invocation_ref,
            "result_envelope_ref": result_envelope_ref,
            "result_envelope_hash": result_envelope_hash,
            "access_class": access_class,
            "preview_status": preview_status,
            "actor_ref": actor_ref,
            "prior_version_ref": prior_version_ref,
        }
        request_hash = content_hash(request)
        with self.store._transaction() as cur:
            idem = self._idempotency(
                cur, idempotency_key, "register_artifact_version", request_hash
            )
            if idem is not None:
                return idem
            invocation = cur.execute(
                "SELECT invocation_json FROM model_invocations WHERE invocation_id=?",
                (producer_invocation_ref,),
            ).fetchone()
            if invocation is None:
                raise ObservabilityNotFound(
                    f"producer invocation not found: {producer_invocation_ref}"
                )
            invocation_wire = json.loads(invocation["invocation_json"])
            if artifact_ref not in invocation_wire["output_refs"]:
                raise ObservabilityConflict(
                    "artifact_ref is not declared by the producer invocation output_refs"
                )
            latest = cur.execute(
                "SELECT version_id,version_number,schema_version "
                "FROM observability_artifact_version_index WHERE artifact_ref=? "
                "ORDER BY version_number DESC LIMIT 1",
                (artifact_ref,),
            ).fetchone()
            if latest is None:
                if prior_version_ref is not None:
                    raise ObservabilityConflict("first artifact version cannot have a prior version")
                version = 1
            else:
                if latest["schema_version"] == "0.2":
                    raise ObservabilityConflict(
                        "artifact history has migrated to v0.2 and cannot append v0.1"
                    )
                if prior_version_ref != latest["version_id"]:
                    raise ObservabilityConflict(
                        "artifact version must continue the latest immutable version"
                    )
                version = int(latest["version_number"]) + 1
            if cur.execute(
                "SELECT 1 FROM observability_artifact_versions WHERE version_id=?",
                (version_id,),
            ).fetchone():
                raise ObservabilityConflict(f"artifact version id collision: {version_id}")
            created_at = _now()
            wire = _record(
                {
                    "schema_version": SCHEMA_VERSION,
                    "id": version_id,
                    "created_at": created_at,
                    "artifact_ref": artifact_ref,
                    "version": version,
                    "title": title,
                    "kind": kind,
                    "media_type": media_type,
                    "artifact_content_hash": artifact_content_hash,
                    "size_bytes": size_bytes,
                    "storage_locator": storage_locator,
                    "producer_invocation_ref": producer_invocation_ref,
                    "work_order_ref": invocation_wire["work_order_ref"],
                    "result_envelope_ref": result_envelope_ref,
                    "result_envelope_hash": result_envelope_hash,
                    "access_class": access_class,
                    "preview_status": preview_status,
                    "actor_ref": actor_ref,
                    "prior_version_ref": prior_version_ref,
                }
            )
            cur.execute(
                "INSERT INTO observability_artifact_versions"
                "(version_id,artifact_ref,version_number,prior_version_id,artifact_content_hash,producer_invocation_ref,work_order_ref,result_envelope_ref,result_envelope_hash,storage_locator,record_json,content_hash,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    version_id,
                    artifact_ref,
                    version,
                    prior_version_ref,
                    artifact_content_hash,
                    producer_invocation_ref,
                    invocation_wire["work_order_ref"],
                    result_envelope_ref,
                    result_envelope_hash,
                    storage_locator,
                    canonical_json(wire),
                    wire["content_hash"],
                    created_at,
                ),
            )
            cur.execute(
                "INSERT INTO observability_artifact_version_index"
                "(version_id,artifact_ref,version_number,schema_version,prior_version_ref,"
                "producer_execution_ref,record_hash,created_at) VALUES(?,?,?,?,?,?,?,?)",
                (
                    version_id,
                    artifact_ref,
                    version,
                    "0.1",
                    prior_version_ref,
                    producer_invocation_ref,
                    wire["content_hash"],
                    created_at,
                ),
            )
            result = {"status": "fresh", **wire}
            self._save_idempotency(
                cur, idempotency_key, "register_artifact_version", request_hash, result
            )
            return {**result, **({"idempotency_key": idempotency_key} if idempotency_key else {})}

    def register_artifact_version_v2(
        self,
        artifact_ref: str,
        *,
        title: str,
        kind: str,
        media_type: str,
        artifact_content_hash: str,
        size_bytes: int,
        storage_locator: str,
        producer_execution_ref: str,
        result_envelope_ref: str,
        result_envelope_hash: str,
        access_class: str,
        preview_status: str,
        actor_ref: str,
        prior_version_ref: str | None = None,
        version_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Append ArtifactVersion v0.2 without fabricating a model invocation."""

        artifact_ref = _nonempty(artifact_ref, "artifact_ref")
        version_id = self._explicit_id_for_retry(version_id, idempotency_key, "version_id")
        title = _nonempty(title, "title")
        kind = _nonempty(kind, "kind")
        media_type = _nonempty(media_type, "media_type")
        artifact_content_hash = _sha256(artifact_content_hash, "artifact_content_hash")
        size_bytes = _nonnegative_int(size_bytes, "size_bytes")
        storage_locator = _nonempty(storage_locator, "storage_locator")
        if storage_locator.lower().startswith("data:") or any(
            ord(char) < 32 for char in storage_locator
        ):
            raise ObservabilityValidationError(
                "storage_locator must identify metadata, not embed artifact content"
            )
        producer_execution_ref = _nonempty(
            producer_execution_ref, "producer_execution_ref"
        )
        result_envelope_ref = _nonempty(result_envelope_ref, "result_envelope_ref")
        result_envelope_hash = _sha256(result_envelope_hash, "result_envelope_hash")
        if access_class not in _ACCESS_CLASSES:
            raise ObservabilityValidationError("access_class is invalid")
        if preview_status not in _PREVIEW_STATUSES:
            raise ObservabilityValidationError("preview_status is invalid")
        actor_ref = _nonempty(actor_ref, "actor_ref")
        prior_version_ref = _optional_ref(prior_version_ref, "prior_version_ref")
        request = {
            "operation": "register_artifact_version_v2",
            "version_id": version_id,
            "artifact_ref": artifact_ref,
            "title": title,
            "kind": kind,
            "media_type": media_type,
            "artifact_content_hash": artifact_content_hash,
            "size_bytes": size_bytes,
            "storage_locator": storage_locator,
            "producer_execution_ref": producer_execution_ref,
            "result_envelope_ref": result_envelope_ref,
            "result_envelope_hash": result_envelope_hash,
            "access_class": access_class,
            "preview_status": preview_status,
            "actor_ref": actor_ref,
            "prior_version_ref": prior_version_ref,
        }
        request_hash = content_hash(request)
        with self.store._transaction() as cur:
            idem = self._idempotency(
                cur, idempotency_key, "register_artifact_version_v2", request_hash
            )
            if idem is not None:
                return idem
            execution = cur.execute(
                "SELECT execution_json FROM execution_invocations WHERE execution_id=?",
                (producer_execution_ref,),
            ).fetchone()
            if execution is None:
                raise ObservabilityNotFound(
                    f"producer execution not found: {producer_execution_ref}"
                )
            execution_wire = json.loads(execution["execution_json"])
            if artifact_ref not in execution_wire["output_refs"]:
                raise ObservabilityConflict(
                    "artifact_ref is not declared by the producer execution output_refs"
                )
            latest = cur.execute(
                "SELECT version_id,version_number FROM observability_artifact_version_index "
                "WHERE artifact_ref=? ORDER BY version_number DESC LIMIT 1",
                (artifact_ref,),
            ).fetchone()
            if latest is None:
                if prior_version_ref is not None:
                    raise ObservabilityConflict(
                        "first artifact version cannot have a prior version"
                    )
                version = 1
            else:
                if prior_version_ref != latest["version_id"]:
                    raise ObservabilityConflict(
                        "artifact version must continue the latest immutable version"
                    )
                version = int(latest["version_number"]) + 1
            if cur.execute(
                "SELECT 1 FROM observability_artifact_versions_v2 WHERE version_id=?",
                (version_id,),
            ).fetchone() or cur.execute(
                "SELECT 1 FROM observability_artifact_versions WHERE version_id=?",
                (version_id,),
            ).fetchone():
                raise ObservabilityConflict(f"artifact version id collision: {version_id}")
            created_at = _now()
            wire = _record(
                {
                    "schema_version": "0.2",
                    "id": version_id,
                    "created_at": created_at,
                    "artifact_ref": artifact_ref,
                    "version": version,
                    "title": title,
                    "kind": kind,
                    "media_type": media_type,
                    "artifact_content_hash": artifact_content_hash,
                    "size_bytes": size_bytes,
                    "storage_locator": storage_locator,
                    "producer_execution_ref": producer_execution_ref,
                    "work_order_ref": execution_wire["work_order_ref"],
                    "result_envelope_ref": result_envelope_ref,
                    "result_envelope_hash": result_envelope_hash,
                    "access_class": access_class,
                    "preview_status": preview_status,
                    "actor_ref": actor_ref,
                    "prior_version_ref": prior_version_ref,
                }
            )
            cur.execute(
                "INSERT INTO observability_artifact_versions_v2"
                "(version_id,artifact_ref,version_number,prior_version_ref,artifact_content_hash,"
                "producer_execution_ref,work_order_ref,result_envelope_ref,result_envelope_hash,"
                "storage_locator,record_json,content_hash,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    version_id,
                    artifact_ref,
                    version,
                    prior_version_ref,
                    artifact_content_hash,
                    producer_execution_ref,
                    execution_wire["work_order_ref"],
                    result_envelope_ref,
                    result_envelope_hash,
                    storage_locator,
                    canonical_json(wire),
                    wire["content_hash"],
                    created_at,
                ),
            )
            cur.execute(
                "INSERT INTO observability_artifact_version_index"
                "(version_id,artifact_ref,version_number,schema_version,prior_version_ref,"
                "producer_execution_ref,record_hash,created_at) VALUES(?,?,?,?,?,?,?,?)",
                (
                    version_id,
                    artifact_ref,
                    version,
                    "0.2",
                    prior_version_ref,
                    producer_execution_ref,
                    wire["content_hash"],
                    created_at,
                ),
            )
            result = {"status": "fresh", **wire}
            self._save_idempotency(
                cur, idempotency_key, "register_artifact_version_v2", request_hash, result
            )
            return {
                **result,
                **({"idempotency_key": idempotency_key} if idempotency_key else {}),
            }

    def get_workflow_version(self, version_id: str) -> dict[str, Any]:
        row = self.connection.execute(
            "SELECT record_json FROM observability_workflow_versions WHERE version_id=?",
            (_nonempty(version_id, "version_id"),),
        ).fetchone()
        return self._row_record(row, "workflow version")

    def latest_workflow_version(self, workflow_ref: str) -> dict[str, Any]:
        row = self.connection.execute(
            "SELECT record_json FROM observability_workflow_versions WHERE workflow_ref=? "
            "ORDER BY version_number DESC LIMIT 1",
            (_nonempty(workflow_ref, "workflow_ref"),),
        ).fetchone()
        return self._row_record(row, "workflow")

    def work_order_links(self, workflow_ref: str) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT record_json FROM observability_work_order_links WHERE workflow_ref=? "
            "ORDER BY sequence_number, created_at, link_id",
            (_nonempty(workflow_ref, "workflow_ref"),),
        ).fetchall()
        return [json.loads(row["record_json"]) for row in rows]

    def get_usage(self, entry_id: str) -> dict[str, Any]:
        row = self.connection.execute(
            "SELECT record_json FROM observability_usage_entries WHERE usage_entry_id=?",
            (_nonempty(entry_id, "entry_id"),),
        ).fetchone()
        return self._row_record(row, "usage entry")

    def latest_usage(self, invocation_ref: str) -> dict[str, Any]:
        row = self.connection.execute(
            "SELECT record_json FROM observability_usage_entries WHERE invocation_ref=? "
            "ORDER BY revision_number DESC LIMIT 1",
            (_nonempty(invocation_ref, "invocation_ref"),),
        ).fetchone()
        return self._row_record(row, "usage entry")

    def get_price_rate(self, version_id: str) -> dict[str, Any]:
        row = self.connection.execute(
            "SELECT record_json FROM observability_price_rate_versions WHERE version_id=?",
            (_nonempty(version_id, "version_id"),),
        ).fetchone()
        return self._row_record(row, "price-rate version")

    def get_cost(self, entry_id: str) -> dict[str, Any]:
        row = self.connection.execute(
            "SELECT record_json FROM observability_cost_entries WHERE cost_entry_id=?",
            (_nonempty(entry_id, "entry_id"),),
        ).fetchone()
        return self._row_record(row, "cost entry")

    def sum_costs(self, cost_entry_refs: Sequence[str], *, currency: str) -> int:
        """Sum one explicit currency; never fold unlike currencies together."""
        refs = _strings(cost_entry_refs, "cost_entry_refs", allow_empty=False)
        currency = _currency(currency)
        total = 0
        for ref in refs:
            cost = self.get_cost(ref)
            if cost["currency"] != currency:
                raise ObservabilityConflict("cost aggregation cannot cross currencies")
            if cost["amount_micros"] is None:
                raise ObservabilityConflict("unpriced cost cannot be silently aggregated")
            total += cost["amount_micros"]
        return total

    def get_artifact_version(self, version_id: str) -> dict[str, Any]:
        """Return immutable metadata from either artifact schema generation."""

        version_id = _nonempty(version_id, "version_id")
        indexed = self.connection.execute(
            "SELECT schema_version FROM observability_artifact_version_index WHERE version_id=?",
            (version_id,),
        ).fetchone()
        if indexed is None:
            raise ObservabilityNotFound("artifact version not found")
        table = (
            "observability_artifact_versions"
            if indexed["schema_version"] == "0.1"
            else "observability_artifact_versions_v2"
        )
        row = self.connection.execute(
            f"SELECT record_json FROM {table} WHERE version_id=?", (version_id,)
        ).fetchone()
        return self._row_record(row, "artifact version")

    def get_artifact_version_v2(self, version_id: str) -> dict[str, Any]:
        row = self.connection.execute(
            "SELECT record_json FROM observability_artifact_versions_v2 WHERE version_id=?",
            (_nonempty(version_id, "version_id"),),
        ).fetchone()
        return self._row_record(row, "artifact version v0.2")

    def latest_artifact_version(self, artifact_ref: str) -> dict[str, Any]:
        row = self.connection.execute(
            "SELECT version_id FROM observability_artifact_version_index WHERE artifact_ref=? "
            "ORDER BY version_number DESC LIMIT 1",
            (_nonempty(artifact_ref, "artifact_ref"),),
        ).fetchone()
        if row is None:
            raise ObservabilityNotFound("artifact not found")
        return self.get_artifact_version(row["version_id"])


ObservabilityAuthority = ObservabilityStore
