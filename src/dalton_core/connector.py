"""Connector Fabric authority, quota admission, and provenance records.

This module does not perform network calls or resolve credentials.  It owns
the append-only facts that a future trusted Connector Runner must present
before and after every physical provider attempt.
"""

from __future__ import annotations

import json
import ipaddress
import re
import sqlite3
import uuid
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from .contracts import ExecutionInvocation, ExecutionKind
from .store import canonical_json, content_hash


SCHEMA_VERSION = "0.1"
_SCHEMA_PATH = Path(__file__).with_name("connector_schema.sql")
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_HOST_RE = re.compile(r"^(?:[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?)$")
_CURRENCY_RE = re.compile(r"^[A-Z]{3}$")
_COMPLETENESS = frozenset({"enumerated", "ranked", "partial", "unknown"})
_METRICS = ("calls", "bytes", "records", "cost_micros")
_ATTEMPT_OUTCOMES = frozenset(
    {"succeeded", "rate_limited", "timeout", "failed", "indeterminate"}
)
_SETTLEMENT_STATES = frozenset({"consumed", "released", "indeterminate"})
_INCIDENT_TYPES = frozenset(
    {"quota_drift", "schema_drift", "credential_auth", "source_outage", "policy_violation"}
)
_HEALTH_STATES = frozenset({"healthy", "degraded", "open_circuit", "recovered"})
_SOURCE_TYPES = frozenset(
    {
        "official_filing", "authenticated_library", "social_enumeration",
        "social_search", "public_web", "market_data",
    }
)
_SENSITIVE_PARAMETER_KEYS = frozenset(
    {
        "api_key", "apikey", "authorization", "client_secret", "cookie",
        "credential", "credentials", "password", "refresh_token", "secret",
        "token", "access_token",
    }
)


class ConnectorError(Exception):
    pass


class ConnectorValidationError(ConnectorError, ValueError):
    pass


class ConnectorConflict(ConnectorError):
    pass


class ConnectorNotFound(ConnectorError):
    pass


class ConnectorQuotaExceeded(ConnectorError):
    pass


class ConnectorBlocked(ConnectorError):
    pass


def _closed(value: Any, fields: set[str], name: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ConnectorValidationError(f"{name} must be an object")
    unknown = set(value) - fields
    missing = fields - set(value)
    if unknown or missing:
        raise ConnectorValidationError(
            f"{name} has invalid closed shape; missing={sorted(missing)}, unknown={sorted(unknown)}"
        )
    try:
        return json.loads(canonical_json(value))
    except (TypeError, ValueError) as exc:
        raise ConnectorValidationError(f"{name} must be finite JSON") from exc


def _text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConnectorValidationError(f"{name} must be a non-empty string")
    return value.strip()


def _hash(value: Any, name: str) -> str:
    value = _text(value, name)
    if not _HASH_RE.fullmatch(value):
        raise ConnectorValidationError(f"{name} must be lowercase SHA-256 hex")
    return value


def _integer(value: Any, name: str, *, minimum: int = 0, nullable: bool = False) -> int | None:
    if value is None and nullable:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        suffix = " or null" if nullable else ""
        raise ConnectorValidationError(f"{name} must be an integer >= {minimum}{suffix}")
    return value


def _timestamp(value: Any, name: str, *, nullable: bool = False) -> str | None:
    if value is None and nullable:
        return None
    value = _text(value, name)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ConnectorValidationError(f"{name} must be RFC3339") from exc
    if parsed.tzinfo is None:
        raise ConnectorValidationError(f"{name} must include timezone")
    return parsed.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _refs(value: Any, name: str, *, nonempty: bool = False) -> list[str]:
    if not isinstance(value, (list, tuple)):
        raise ConnectorValidationError(f"{name} must be an array")
    result = [_text(item, f"{name}[]") for item in value]
    if nonempty and not result:
        raise ConnectorValidationError(f"{name} must not be empty")
    if len(set(result)) != len(result):
        raise ConnectorValidationError(f"{name} must contain unique values")
    return result


def _metric_map(value: Any, name: str, *, calls_required: bool = False) -> dict[str, int]:
    obj = _closed(value, set(_METRICS), name)
    result = {metric: int(_integer(obj[metric], f"{name}.{metric}")) for metric in _METRICS}
    if calls_required and result["calls"] != 1:
        raise ConnectorValidationError("each physical attempt must reserve exactly one call")
    return result


def _public_host(value: Any, name: str) -> str:
    host = _text(value, name).lower().rstrip(".")
    if not _HOST_RE.fullmatch(host) or "/" in host or "://" in host:
        raise ConnectorValidationError(f"{name} must be a literal hostname")
    if host == "localhost" or host.endswith(".local"):
        raise ConnectorValidationError(f"{name} must be public")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return host
    if not address.is_global:
        raise ConnectorValidationError(f"{name} must not be a private IP")
    return host


def _contains_sensitive_parameter(value: Any) -> bool:
    if isinstance(value, Mapping):
        for key, child in value.items():
            normalized = re.sub(r"[^a-z0-9]+", "_", str(key).strip().lower()).strip("_")
            if normalized in _SENSITIVE_PARAMETER_KEYS or _contains_sensitive_parameter(child):
                return True
        return False
    if isinstance(value, list):
        return any(_contains_sensitive_parameter(child) for child in value)
    return False


def _profile_schema_bundle(wire: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "allowed_operations": wire["allowed_operations"],
        "input_schema_refs": wire["input_schema_refs"],
        "input_schema_hashes": wire["input_schema_hashes"],
        "output_schema_refs": wire["output_schema_refs"],
        "output_schema_hashes": wire["output_schema_hashes"],
    }


def _price_book_projection(wire: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "price_rate_refs": wire["price_rate_refs"],
        "required_price_meters": wire["required_price_meters"],
    }


def _source_content_projection(wire: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "source": wire["source"],
        "operation": wire["operation"],
        "source_record_refs": wire["source_record_refs"],
        "published_at": wire["published_at"],
        "updated_at": wire["updated_at"],
        "as_of": wire["as_of"],
        "retrieved_at": wire["retrieved_at"],
        "cursor": wire["cursor"],
        "provider_request_id": wire["provider_request_id"],
        "completeness": wire["completeness"],
        "status": wire["status"],
        "error": wire["error"],
    }


def source_envelope_content_hash(spec: Mapping[str, Any]) -> str:
    """Hash the normalized, provider-facing content carried by a SourceEnvelope."""

    normalized = json.loads(canonical_json(spec))
    for name in ("source", "operation"):
        normalized[name] = _text(normalized[name], name)
    normalized["source_record_refs"] = _refs(
        normalized["source_record_refs"], "source_record_refs"
    )
    for name in ("published_at", "updated_at", "as_of"):
        normalized[name] = _timestamp(normalized[name], name, nullable=True)
    normalized["retrieved_at"] = _timestamp(normalized["retrieved_at"], "retrieved_at")
    normalized["cursor"] = (
        None if normalized["cursor"] is None else _text(normalized["cursor"], "cursor")
    )
    normalized["provider_request_id"] = (
        None
        if normalized["provider_request_id"] is None
        else _text(normalized["provider_request_id"], "provider_request_id")
    )
    return content_hash(_source_content_projection(normalized))


def validate_connector_proposal_manifest(
    spec: Mapping[str, Any], *, now: datetime | None = None
) -> dict[str, Any]:
    """Validate the immutable self-generated connector proposal boundary."""

    fields = {
        "schema_version", "id", "created_at", "capability_proposal_ref", "connector_ref",
        "source_identity", "adapter_package_ref", "adapter_source_hash",
        "profile_template_ref", "profile_template_hash", "operations",
        "fixture_manifest_ref", "fixture_manifest_hash", "offline_attestation_policy_ref",
        "requested_canary", "promotion_policy_ref", "builder_ref", "content_hash",
    }
    wire = _closed(spec, fields, "ConnectorProposalManifest")
    if wire["schema_version"] != SCHEMA_VERSION:
        raise ConnectorValidationError("unsupported ConnectorProposalManifest schema_version")
    for name in (
        "id", "capability_proposal_ref", "connector_ref", "adapter_package_ref",
        "profile_template_ref", "fixture_manifest_ref", "offline_attestation_policy_ref",
        "promotion_policy_ref", "builder_ref",
    ):
        wire[name] = _text(wire[name], name)
    wire["created_at"] = _timestamp(wire["created_at"], "created_at")
    for name in (
        "adapter_source_hash", "profile_template_hash", "fixture_manifest_hash", "content_hash",
    ):
        wire[name] = _hash(wire[name], name)
    identity = _closed(
        wire["source_identity"],
        {"source", "adapter", "source_version", "adapter_version"},
        "source_identity",
    )
    for name in identity:
        identity[name] = _text(identity[name], f"source_identity.{name}")
    wire["source_identity"] = identity
    if not isinstance(wire["operations"], list) or not wire["operations"]:
        raise ConnectorValidationError("operations must be a non-empty array")
    operations: list[dict[str, Any]] = []
    names: set[str] = set()
    for index, item in enumerate(wire["operations"]):
        operation = _closed(
            item,
            {
                "operation", "input_schema_ref", "input_schema_hash", "output_schema_ref",
                "output_schema_hash", "completeness", "pagination", "side_effects",
            },
            f"operations[{index}]",
        )
        operation["operation"] = _text(operation["operation"], "operation")
        if operation["operation"] in names:
            raise ConnectorValidationError("operations must have unique operation names")
        names.add(operation["operation"])
        for name in ("input_schema_ref", "output_schema_ref"):
            operation[name] = _text(operation[name], name)
        for name in ("input_schema_hash", "output_schema_hash"):
            operation[name] = _hash(operation[name], name)
        if operation["completeness"] not in _COMPLETENESS:
            raise ConnectorValidationError("operation completeness is invalid")
        if operation["pagination"] not in {"none", "cursor", "page"}:
            raise ConnectorValidationError("operation pagination is invalid")
        operation["side_effects"] = _refs(operation["side_effects"], "side_effects")
        operations.append(operation)
    wire["operations"] = operations
    canary = _closed(
        wire["requested_canary"],
        {
            "allowed_hosts", "credential_slot_refs", "max_calls", "max_bytes",
            "max_records", "max_cost_micros", "expires_at",
        },
        "requested_canary",
    )
    canary["allowed_hosts"] = [
        _public_host(host, "requested_canary.allowed_hosts[]")
        for host in _refs(canary["allowed_hosts"], "requested_canary.allowed_hosts", nonempty=True)
    ]
    canary["credential_slot_refs"] = _refs(
        canary["credential_slot_refs"], "credential_slot_refs"
    )
    for name, minimum in (
        ("max_calls", 1), ("max_bytes", 0), ("max_records", 0), ("max_cost_micros", 0)
    ):
        canary[name] = _integer(canary[name], name, minimum=minimum)
    canary["expires_at"] = _timestamp(canary["expires_at"], "expires_at")
    now_value = now or datetime.now(timezone.utc)
    if now_value.tzinfo is None:
        raise ConnectorValidationError("manifest validation clock must be timezone-aware")
    if canary["expires_at"] <= now_value.astimezone(timezone.utc).isoformat(timespec="microseconds"):
        raise ConnectorValidationError("requested canary is expired")
    wire["requested_canary"] = canary
    declared_hash = wire.pop("content_hash")
    expected_hash = content_hash(wire)
    if declared_hash != expected_hash:
        raise ConnectorConflict("ConnectorProposalManifest content_hash mismatch")
    wire["content_hash"] = declared_hash
    return wire


class ConnectorStore:
    """Append-only connector authority on the trusted ``DaltonStore`` connection."""

    def __init__(self, store: Any, *, clock: Callable[[], datetime] | None = None):
        if not hasattr(store, "connection") or not hasattr(store, "_transaction"):
            raise TypeError("ConnectorStore requires a DaltonStore")
        self.store = store
        self.connection: sqlite3.Connection = store.connection
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.connection.executescript(_SCHEMA_PATH.read_text(encoding="utf-8"))

    @property
    def conn(self) -> sqlite3.Connection:
        return self.connection

    def _now_dt(self) -> datetime:
        value = self.clock()
        if not isinstance(value, datetime) or value.tzinfo is None:
            raise ConnectorValidationError("connector clock must return aware datetime")
        return value.astimezone(timezone.utc)

    def _now(self) -> str:
        return self._now_dt().isoformat(timespec="microseconds")

    @staticmethod
    def _record(value: Mapping[str, Any]) -> dict[str, Any]:
        wire = dict(value)
        wire["content_hash"] = content_hash(wire)
        return wire

    @staticmethod
    def _id(prefix: str, supplied: str | None = None) -> str:
        return _text(supplied, prefix) if supplied is not None else f"{prefix}:{uuid.uuid4().hex}"

    @staticmethod
    def _idempotent_id(prefix: str, supplied: str | None, key: str) -> str:
        if supplied is not None:
            return _text(supplied, prefix)
        digest = content_hash({"kind": prefix, "idempotency_key": _text(key, "idempotency_key")})
        return f"{prefix}:{digest}"

    def _idem(
        self, cur: sqlite3.Cursor, key: str | None, operation: str, request_hash: str
    ) -> dict[str, Any] | None:
        if key is None:
            return None
        key = _text(key, "idempotency_key")
        row = cur.execute(
            "SELECT operation,request_hash,result_json FROM connector_idempotency_keys "
            "WHERE idempotency_key=?",
            (key,),
        ).fetchone()
        if row is None:
            return None
        if row["operation"] == operation and row["request_hash"] == request_hash:
            result = json.loads(row["result_json"])
            result["write_status"] = "duplicate"
            return result
        raise ConnectorConflict(f"idempotency key conflict: {key}")

    def _save_idem(
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
            "INSERT INTO connector_idempotency_keys"
            "(idempotency_key,operation,request_hash,result_json,created_at) VALUES(?,?,?,?,?)",
            (_text(key, "idempotency_key"), operation, request_hash, canonical_json(result), self._now()),
        )

    @staticmethod
    def _row_record(row: sqlite3.Row | None, name: str) -> dict[str, Any]:
        if row is None:
            raise ConnectorNotFound(name)
        return json.loads(row["record_json"])

    def register_profile(
        self, spec: Mapping[str, Any], *, idempotency_key: str | None = None
    ) -> dict[str, Any]:
        fields = {
            "schema_version", "id", "created_at", "connector_ref", "version",
            "prior_version_ref", "capability_id", "descriptor_revision_ref",
            "descriptor_hash", "source_identity", "source_hash", "schema_hash", "catalog_epoch",
            "adapter_ref", "adapter_hash", "runner_runtime_ref", "runner_actor_ref",
            "runner_environment_hash",
            "allowed_operations", "allowed_hosts",
            "auth_mode", "credential_slot_refs", "input_schema_refs",
            "input_schema_hashes", "output_schema_refs", "output_schema_hashes",
            "pagination", "completeness", "max_response_bytes",
            "max_records", "access_policy_ref", "retention_policy_ref",
            "terms_policy_ref", "network_policy",
        }
        wire = _closed(spec, fields, "ConnectorProfileVersion")
        if wire["schema_version"] != SCHEMA_VERSION:
            raise ConnectorValidationError("unsupported connector profile schema_version")
        for name in (
            "id", "connector_ref", "capability_id", "descriptor_revision_ref", "adapter_ref",
            "runner_runtime_ref", "runner_actor_ref", "access_policy_ref",
            "retention_policy_ref", "terms_policy_ref",
        ):
            wire[name] = _text(wire[name], name)
        wire["created_at"] = _timestamp(wire["created_at"], "created_at")
        wire["prior_version_ref"] = (
            None if wire["prior_version_ref"] is None else _text(wire["prior_version_ref"], "prior_version_ref")
        )
        wire["version"] = _integer(wire["version"], "version", minimum=1)
        wire["catalog_epoch"] = _integer(wire["catalog_epoch"], "catalog_epoch", minimum=1)
        wire["max_response_bytes"] = _integer(
            wire["max_response_bytes"], "max_response_bytes", minimum=1
        )
        wire["max_records"] = _integer(wire["max_records"], "max_records", minimum=1)
        for name in (
            "descriptor_hash", "source_hash", "schema_hash", "adapter_hash",
            "runner_environment_hash",
        ):
            wire[name] = _hash(wire[name], name)
        source_identity = _closed(
            wire["source_identity"], {"source_ref", "source_type", "source_version"},
            "source_identity",
        )
        source_identity["source_ref"] = _text(source_identity["source_ref"], "source_ref")
        source_identity["source_type"] = _text(source_identity["source_type"], "source_type")
        source_identity["source_version"] = _text(
            source_identity["source_version"], "source_version"
        )
        if source_identity["source_type"] not in _SOURCE_TYPES:
            raise ConnectorValidationError("source_identity.source_type is invalid")
        wire["source_identity"] = source_identity
        if wire["source_hash"] != content_hash(source_identity):
            raise ConnectorValidationError("source_hash does not bind source_identity")
        operations = _refs(wire["allowed_operations"], "allowed_operations", nonempty=True)
        hosts = [
            _public_host(host, "allowed_hosts[]")
            for host in _refs(wire["allowed_hosts"], "allowed_hosts", nonempty=True)
        ]
        wire["allowed_operations"] = operations
        wire["allowed_hosts"] = hosts
        auth_mode = _text(wire["auth_mode"], "auth_mode")
        if auth_mode not in {"none", "credential_slot", "mcp_managed"}:
            raise ConnectorValidationError("auth_mode is invalid")
        wire["auth_mode"] = auth_mode
        slots = _refs(wire["credential_slot_refs"], "credential_slot_refs")
        if auth_mode == "none" and slots:
            raise ConnectorValidationError("auth_mode none cannot declare credential slots")
        if auth_mode != "none" and not slots:
            raise ConnectorValidationError("authenticated profiles require credential slots")
        wire["credential_slot_refs"] = slots
        for name in (
            "input_schema_refs", "input_schema_hashes", "output_schema_refs",
            "output_schema_hashes", "completeness",
        ):
            if not isinstance(wire[name], Mapping) or set(wire[name]) != set(operations):
                raise ConnectorValidationError(f"{name} must map every allowed operation exactly once")
        for mapping_name in ("input_schema_refs", "output_schema_refs"):
            wire[mapping_name] = {
                op: _text(wire[mapping_name][op], f"{mapping_name}.{op}") for op in operations
            }
        for mapping_name in ("input_schema_hashes", "output_schema_hashes"):
            wire[mapping_name] = {
                op: _hash(wire[mapping_name][op], f"{mapping_name}.{op}") for op in operations
            }
        wire["completeness"] = {
            op: _text(wire["completeness"][op], f"completeness.{op}") for op in operations
        }
        if any(value not in _COMPLETENESS for value in wire["completeness"].values()):
            raise ConnectorValidationError("completeness is invalid")
        if wire["schema_hash"] != content_hash(_profile_schema_bundle(wire)):
            raise ConnectorValidationError("schema_hash does not bind operation schemas")
        pagination = _closed(wire["pagination"], {"mode", "cursor_field", "max_pages"}, "pagination")
        if pagination["mode"] not in {"none", "cursor", "page"}:
            raise ConnectorValidationError("pagination.mode is invalid")
        if pagination["mode"] == "none" and pagination["cursor_field"] is not None:
            raise ConnectorValidationError("non-paginated profiles cannot declare cursor_field")
        if pagination["mode"] != "none":
            pagination["cursor_field"] = _text(pagination["cursor_field"], "cursor_field")
        pagination["max_pages"] = _integer(pagination["max_pages"], "max_pages", minimum=1)
        wire["pagination"] = pagination
        network = _closed(
            wire["network_policy"],
            {"allowed_schemes", "allow_redirects", "max_redirects", "resolve_public_only"},
            "network_policy",
        )
        schemes = _refs(network["allowed_schemes"], "allowed_schemes", nonempty=True)
        if any(scheme not in {"https"} for scheme in schemes):
            raise ConnectorValidationError("only https is allowed in Connector P0")
        if type(network["allow_redirects"]) is not bool or type(network["resolve_public_only"]) is not bool:
            raise ConnectorValidationError("network policy flags must be boolean")
        network["max_redirects"] = _integer(network["max_redirects"], "max_redirects")
        if not network["allow_redirects"] and network["max_redirects"] != 0:
            raise ConnectorValidationError("disabled redirects require max_redirects=0")
        if not network["resolve_public_only"]:
            raise ConnectorValidationError("Connector P0 requires public-only DNS resolution")
        network["allowed_schemes"] = schemes
        wire["network_policy"] = network
        wire = self._record(wire)
        request_hash = content_hash(wire)
        with self.store._transaction() as cur:
            duplicate = self._idem(cur, idempotency_key, "register_connector_profile", request_hash)
            if duplicate is not None:
                return duplicate
            latest = cur.execute(
                "SELECT profile_version_id,version_number FROM connector_profile_versions "
                "WHERE connector_ref=? ORDER BY version_number DESC LIMIT 1",
                (wire["connector_ref"],),
            ).fetchone()
            expected_version = 1 if latest is None else int(latest["version_number"]) + 1
            expected_prior = None if latest is None else latest["profile_version_id"]
            if wire["version"] != expected_version or wire["prior_version_ref"] != expected_prior:
                raise ConnectorConflict("connector profile version chain is not contiguous")
            cur.execute(
                "INSERT INTO connector_profile_versions"
                "(profile_version_id,connector_ref,version_number,prior_version_ref,capability_id,"
                "descriptor_revision_ref,descriptor_hash,source_hash,schema_hash,catalog_epoch,"
                "adapter_ref,adapter_hash,record_json,content_hash,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    wire["id"], wire["connector_ref"], wire["version"], wire["prior_version_ref"],
                    wire["capability_id"], wire["descriptor_revision_ref"], wire["descriptor_hash"],
                    wire["source_hash"], wire["schema_hash"], wire["catalog_epoch"],
                    wire["adapter_ref"], wire["adapter_hash"], canonical_json(wire),
                    wire["content_hash"], wire["created_at"],
                ),
            )
            result = {"write_status": "fresh", **wire}
            self._save_idem(cur, idempotency_key, "register_connector_profile", request_hash, result)
            return result

    def register_call_spec(
        self, spec: Mapping[str, Any], *, idempotency_key: str | None = None
    ) -> dict[str, Any]:
        fields = {
            "schema_version", "id", "created_at", "work_order_ref", "work_order_hash",
            "connector_profile_ref", "operation", "parameters", "query_hash",
        }
        wire = _closed(spec, fields, "ConnectorCallSpec")
        if wire["schema_version"] != SCHEMA_VERSION:
            raise ConnectorValidationError("unsupported ConnectorCallSpec schema_version")
        for name in ("id", "work_order_ref", "connector_profile_ref", "operation"):
            wire[name] = _text(wire[name], name)
        wire["created_at"] = _timestamp(wire["created_at"], "created_at")
        wire["work_order_hash"] = _hash(wire["work_order_hash"], "work_order_hash")
        if not isinstance(wire["parameters"], Mapping):
            raise ConnectorValidationError("parameters must be an object")
        wire["parameters"] = json.loads(canonical_json(wire["parameters"]))
        if _contains_sensitive_parameter(wire["parameters"]):
            raise ConnectorValidationError(
                "parameters cannot contain credential-shaped fields; use credential slots"
            )
        expected_query_hash = content_hash(
            {"operation": wire["operation"], "parameters": wire["parameters"]}
        )
        if wire["query_hash"] != expected_query_hash:
            raise ConnectorValidationError("query_hash does not match canonical operation/parameters")
        profile = self.get_profile(wire["connector_profile_ref"])
        if wire["operation"] not in profile["allowed_operations"]:
            raise ConnectorValidationError("operation is not allowed by connector profile")
        wire = self._record(wire)
        request_hash = content_hash(wire)
        with self.store._transaction() as cur:
            duplicate = self._idem(cur, idempotency_key, "register_connector_call_spec", request_hash)
            if duplicate is not None:
                return duplicate
            cur.execute(
                "INSERT INTO connector_call_specs"
                "(call_spec_id,work_order_ref,work_order_hash,connector_profile_ref,operation,"
                "query_hash,record_json,content_hash,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    wire["id"], wire["work_order_ref"], wire["work_order_hash"],
                    wire["connector_profile_ref"], wire["operation"], wire["query_hash"],
                    canonical_json(wire), wire["content_hash"], wire["created_at"],
                ),
            )
            result = {"write_status": "fresh", **wire}
            self._save_idem(cur, idempotency_key, "register_connector_call_spec", request_hash, result)
            return result

    def register_invocation(
        self,
        spec: Mapping[str, Any],
        *,
        execution: ExecutionInvocation | Mapping[str, Any],
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        fields = {
            "schema_version", "id", "created_at", "work_order_ref", "work_order_hash",
            "connector_profile_ref", "connector_profile_hash", "call_spec_ref",
            "call_spec_hash", "capability_lease_ref", "capability_lease_hash",
            "descriptor_revision_ref", "catalog_epoch", "logical_invocation_key",
        }
        wire = _closed(spec, fields, "ConnectorInvocation")
        if wire["schema_version"] != SCHEMA_VERSION:
            raise ConnectorValidationError("unsupported ConnectorInvocation schema_version")
        for name in (
            "id", "work_order_ref", "connector_profile_ref", "call_spec_ref",
            "capability_lease_ref", "descriptor_revision_ref", "logical_invocation_key",
        ):
            wire[name] = _text(wire[name], name)
        if not wire["id"].startswith("connector-invocation:"):
            raise ConnectorValidationError("connector invocation ids require connector-invocation namespace")
        wire["created_at"] = _timestamp(wire["created_at"], "created_at")
        for name in (
            "work_order_hash", "connector_profile_hash", "call_spec_hash", "capability_lease_hash"
        ):
            wire[name] = _hash(wire[name], name)
        wire["catalog_epoch"] = _integer(wire["catalog_epoch"], "catalog_epoch", minimum=1)
        profile = self.get_profile(wire["connector_profile_ref"])
        call = self.get_call_spec(wire["call_spec_ref"])
        if wire["connector_profile_hash"] != profile["content_hash"]:
            raise ConnectorConflict("connector profile hash is stale")
        if wire["call_spec_hash"] != call["content_hash"]:
            raise ConnectorConflict("connector call spec hash is stale")
        if (
            call["connector_profile_ref"] != wire["connector_profile_ref"]
            or call["work_order_ref"] != wire["work_order_ref"]
            or call["work_order_hash"] != wire["work_order_hash"]
            or profile["descriptor_revision_ref"] != wire["descriptor_revision_ref"]
            or profile["catalog_epoch"] != wire["catalog_epoch"]
        ):
            raise ConnectorConflict("invocation refs do not match frozen profile/call spec")
        expected_logical_key = "connector-logical:" + content_hash(
            {
                "work_order_ref": wire["work_order_ref"],
                "work_order_hash": wire["work_order_hash"],
                "connector_profile_hash": wire["connector_profile_hash"],
                "call_spec_hash": wire["call_spec_hash"],
            }
        )
        if wire["logical_invocation_key"] != expected_logical_key:
            raise ConnectorConflict("logical_invocation_key is not canonically derived")
        execution_obj = (
            execution
            if isinstance(execution, ExecutionInvocation)
            else ExecutionInvocation.from_dict(execution)
        )
        if (
            execution_obj.kind is not ExecutionKind.CONNECTOR
            or execution_obj.id != wire["id"]
            or _timestamp(execution_obj.created_at, "execution.created_at") != wire["created_at"]
            or _timestamp(execution_obj.started_at, "execution.started_at") != wire["created_at"]
            or execution_obj.work_order_ref != wire["work_order_ref"]
            or execution_obj.profile_ref != wire["connector_profile_ref"]
            or execution_obj.capability != profile["capability_id"]
            or execution_obj.input_refs != (wire["call_spec_ref"],)
            or len(execution_obj.output_refs) != 1
            or execution_obj.side_effects
            or execution_obj.completed_at is not None
            or execution_obj.runtime_ref != profile["runner_runtime_ref"]
            or execution_obj.actor_ref != profile["runner_actor_ref"]
            or execution_obj.environment_hash != profile["runner_environment_hash"]
            or execution_obj.parent_ref is not None
        ):
            raise ConnectorConflict("generic execution and connector subtype are not canonically equal")
        wire["execution_ref"] = execution_obj.id
        wire["execution_hash"] = content_hash(execution_obj.to_dict())
        wire = self._record(wire)
        request_hash = content_hash(wire)
        with self.store._transaction() as cur:
            duplicate = self._idem(cur, idempotency_key, "register_connector_invocation", request_hash)
            if duplicate is not None:
                return duplicate
            self.store._ensure_execution_invocation(cur, execution_obj)
            cur.execute(
                "INSERT INTO connector_invocations"
                "(connector_invocation_id,execution_ref,execution_hash,work_order_ref,work_order_hash,"
                "connector_profile_ref,connector_profile_hash,call_spec_ref,call_spec_hash,"
                "capability_lease_ref,capability_lease_hash,descriptor_revision_ref,catalog_epoch,"
                "logical_invocation_key,record_json,content_hash,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    wire["id"], wire["execution_ref"], wire["execution_hash"], wire["work_order_ref"],
                    wire["work_order_hash"], wire["connector_profile_ref"],
                    wire["connector_profile_hash"], wire["call_spec_ref"], wire["call_spec_hash"],
                    wire["capability_lease_ref"], wire["capability_lease_hash"],
                    wire["descriptor_revision_ref"], wire["catalog_epoch"],
                    wire["logical_invocation_key"], canonical_json(wire), wire["content_hash"],
                    wire["created_at"],
                ),
            )
            result = {"write_status": "fresh", **wire}
            self._save_idem(cur, idempotency_key, "register_connector_invocation", request_hash, result)
            return result

    def register_rate_policy(
        self, spec: Mapping[str, Any], *, idempotency_key: str | None = None
    ) -> dict[str, Any]:
        fields = {
            "schema_version", "id", "created_at", "policy_ref", "quota_scope_ref", "version",
            "prior_version_ref", "connector_profile_ref", "window_seconds",
            "reset_timezone", "max_concurrency", "quota_currency", "price_rate_refs",
            "required_price_meters", "price_book_hash", "limits", "effective_from",
            "effective_until", "actor_ref",
        }
        wire = _closed(spec, fields, "ConnectorRatePolicyVersion")
        if wire["schema_version"] != SCHEMA_VERSION:
            raise ConnectorValidationError("unsupported ConnectorRatePolicyVersion schema_version")
        for name in ("id", "policy_ref", "quota_scope_ref", "connector_profile_ref", "actor_ref"):
            wire[name] = _text(wire[name], name)
        wire["prior_version_ref"] = (
            None if wire["prior_version_ref"] is None else _text(wire["prior_version_ref"], "prior_version_ref")
        )
        wire["created_at"] = _timestamp(wire["created_at"], "created_at")
        wire["effective_from"] = _timestamp(wire["effective_from"], "effective_from")
        wire["effective_until"] = _timestamp(
            wire["effective_until"], "effective_until", nullable=True
        )
        if wire["effective_until"] is not None and wire["effective_until"] <= wire["effective_from"]:
            raise ConnectorValidationError("effective_until must follow effective_from")
        wire["version"] = _integer(wire["version"], "version", minimum=1)
        wire["window_seconds"] = _integer(wire["window_seconds"], "window_seconds", minimum=1)
        wire["max_concurrency"] = _integer(wire["max_concurrency"], "max_concurrency", minimum=1)
        if not isinstance(wire["quota_currency"], str) or not _CURRENCY_RE.fullmatch(
            wire["quota_currency"]
        ):
            raise ConnectorValidationError("quota_currency must be uppercase ISO-4217")
        if wire["reset_timezone"] != "UTC":
            raise ConnectorValidationError("Connector P0 supports UTC quota windows only")
        wire["price_rate_refs"] = sorted(_refs(wire["price_rate_refs"], "price_rate_refs"))
        wire["required_price_meters"] = sorted(
            _refs(wire["required_price_meters"], "required_price_meters")
        )
        if not wire["price_rate_refs"] or not wire["required_price_meters"]:
            raise ConnectorValidationError(
                "rate policy requires an explicit price rate for every priced meter"
            )
        if any(
            meter not in {"calls", "bytes", "records"}
            for meter in wire["required_price_meters"]
        ):
            raise ConnectorValidationError("required_price_meters contains an invalid meter")
        wire["price_book_hash"] = _hash(wire["price_book_hash"], "price_book_hash")
        if wire["price_book_hash"] != content_hash(_price_book_projection(wire)):
            raise ConnectorValidationError("price_book_hash does not bind exact rates and meters")
        if len(wire["price_rate_refs"]) != len(wire["required_price_meters"]):
            raise ConnectorValidationError("price book requires exactly one rate per required meter")
        wire["limits"] = _metric_map(wire["limits"], "limits")
        if all(value == 0 for value in wire["limits"].values()):
            raise ConnectorValidationError("rate policy must allow at least one measurable unit")
        self.get_profile(wire["connector_profile_ref"])
        wire = self._record(wire)
        request_hash = content_hash(wire)
        with self.store._transaction() as cur:
            duplicate = self._idem(cur, idempotency_key, "register_connector_rate_policy", request_hash)
            if duplicate is not None:
                return duplicate
            latest = cur.execute(
                "SELECT policy_version_id,version_number,quota_scope_ref,connector_profile_ref,"
                "window_seconds,quota_currency FROM connector_rate_policy_versions "
                "WHERE policy_ref=? ORDER BY version_number DESC LIMIT 1",
                (wire["policy_ref"],),
            ).fetchone()
            expected_version = 1 if latest is None else int(latest["version_number"]) + 1
            expected_prior = None if latest is None else latest["policy_version_id"]
            if wire["version"] != expected_version or wire["prior_version_ref"] != expected_prior:
                raise ConnectorConflict("rate policy version chain is not contiguous")
            if latest is not None and (
                latest["quota_scope_ref"] != wire["quota_scope_ref"]
                or latest["connector_profile_ref"] != wire["connector_profile_ref"]
                or int(latest["window_seconds"]) != wire["window_seconds"]
                or latest["quota_currency"] != wire["quota_currency"]
            ):
                raise ConnectorConflict("rate policy scope/profile/window are stable across versions")
            resolved_meters: set[str] = set()
            for rate_ref in wire["price_rate_refs"]:
                rate = cur.execute(
                    "SELECT connector_profile_ref,meter,currency,effective_from,effective_until "
                    "FROM connector_price_rate_versions WHERE price_rate_version_id=?",
                    (rate_ref,),
                ).fetchone()
                if rate is None:
                    raise ConnectorNotFound(rate_ref)
                if (
                    rate["connector_profile_ref"] != wire["connector_profile_ref"]
                    or rate["currency"] != wire["quota_currency"]
                ):
                    raise ConnectorConflict("price book rate profile/currency mismatch")
                if rate["meter"] in resolved_meters:
                    raise ConnectorConflict("price book has duplicate meter")
                resolved_meters.add(rate["meter"])
                if rate["effective_from"] > wire["effective_from"] or (
                    rate["effective_until"] is not None
                    and (
                        wire["effective_until"] is None
                        or rate["effective_until"] < wire["effective_until"]
                    )
                ):
                    raise ConnectorConflict("price book rate does not cover policy interval")
            if resolved_meters != set(wire["required_price_meters"]):
                raise ConnectorConflict("price book meters do not match required_price_meters")
            canonical_refs, canonical_meters = self._canonical_price_book_for_interval(
                cur,
                connector_profile_ref=wire["connector_profile_ref"],
                currency=wire["quota_currency"],
                effective_from=wire["effective_from"],
                effective_until=wire["effective_until"],
            )
            if (
                canonical_refs != wire["price_rate_refs"]
                or canonical_meters != wire["required_price_meters"]
            ):
                raise ConnectorConflict(
                    "rate policy must freeze the complete canonical price book"
                )
            cur.execute(
                "INSERT INTO connector_rate_policy_versions"
                "(policy_version_id,policy_ref,quota_scope_ref,version_number,prior_version_ref,"
                "connector_profile_ref,window_seconds,max_concurrency,quota_currency,price_book_hash,"
                "effective_from,effective_until,record_json,content_hash,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    wire["id"], wire["policy_ref"], wire["quota_scope_ref"], wire["version"],
                    wire["prior_version_ref"],
                    wire["connector_profile_ref"], wire["window_seconds"], wire["max_concurrency"],
                    wire["quota_currency"], wire["price_book_hash"], wire["effective_from"], wire["effective_until"],
                    canonical_json(wire), wire["content_hash"], wire["created_at"],
                ),
            )
            prior_activation = cur.execute(
                "SELECT event_id FROM connector_rate_policy_activation_events "
                "WHERE policy_ref=? ORDER BY rowid DESC LIMIT 1",
                (wire["policy_ref"],),
            ).fetchone()
            activation = self._record(
                {
                    "schema_version": SCHEMA_VERSION,
                    "id": self._idempotent_id(
                        "connector-rate-policy-activation", None,
                        f"{idempotency_key or wire['id']}:activation",
                    ),
                    "created_at": self._now(),
                    "policy_ref": wire["policy_ref"],
                    "quota_scope_ref": wire["quota_scope_ref"],
                    "policy_version_ref": wire["id"],
                    "policy_version_hash": wire["content_hash"],
                    "effective_at": wire["effective_from"],
                    "prior_event_ref": (
                        None if prior_activation is None else prior_activation["event_id"]
                    ),
                    "actor_ref": wire["actor_ref"],
                }
            )
            cur.execute(
                "INSERT INTO connector_rate_policy_activation_events"
                "(event_id,policy_ref,quota_scope_ref,policy_version_ref,policy_version_hash,"
                "effective_at,prior_event_ref,actor_ref,record_json,content_hash,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    activation["id"], activation["policy_ref"], activation["quota_scope_ref"],
                    activation["policy_version_ref"], activation["policy_version_hash"],
                    activation["effective_at"], activation["prior_event_ref"],
                    activation["actor_ref"], canonical_json(activation),
                    activation["content_hash"], activation["created_at"],
                ),
            )
            result = {"write_status": "fresh", **wire}
            self._save_idem(cur, idempotency_key, "register_connector_rate_policy", request_hash, result)
            return result

    @staticmethod
    def _active_rate_policy(
        cur: sqlite3.Cursor, policy_ref: str, now: str
    ) -> dict[str, Any]:
        activation = cur.execute(
            "SELECT policy_version_ref,policy_version_hash FROM "
            "connector_rate_policy_activation_events WHERE policy_ref=? AND effective_at<=? "
            "ORDER BY effective_at DESC,rowid DESC LIMIT 1",
            (policy_ref, now),
        ).fetchone()
        if activation is None:
            raise ConnectorBlocked("rate policy is not active yet")
        row = cur.execute(
            "SELECT record_json,content_hash FROM connector_rate_policy_versions "
            "WHERE policy_version_id=?",
            (activation["policy_version_ref"],),
        ).fetchone()
        if row is None or row["content_hash"] != activation["policy_version_hash"]:
            raise ConnectorConflict("active rate policy hash mismatch")
        policy = json.loads(row["record_json"])
        if now < policy["effective_from"] or (
            policy["effective_until"] is not None and now >= policy["effective_until"]
        ):
            raise ConnectorBlocked("active rate policy is outside its effective interval")
        return policy

    @staticmethod
    def _canonical_price_book_for_interval(
        cur: sqlite3.Cursor,
        *,
        connector_profile_ref: str,
        currency: str,
        effective_from: str,
        effective_until: str | None,
    ) -> tuple[list[str], list[str]]:
        rows = cur.execute(
            "SELECT price_rate_version_id,meter,effective_from,effective_until "
            "FROM connector_price_rate_versions WHERE connector_profile_ref=? AND currency=? "
            "AND effective_from < COALESCE(?, '9999-12-31T23:59:59.999999+00:00') "
            "AND COALESCE(effective_until, '9999-12-31T23:59:59.999999+00:00') > ? "
            "ORDER BY price_rate_version_id",
            (connector_profile_ref, currency, effective_until, effective_from),
        ).fetchall()
        if not rows:
            raise ConnectorConflict("rate policy requires an explicit canonical price book")
        meters: set[str] = set()
        for row in rows:
            if row["meter"] in meters:
                raise ConnectorConflict("canonical price book has duplicate meter")
            meters.add(row["meter"])
            if row["effective_from"] > effective_from or (
                row["effective_until"] is not None
                and (effective_until is None or row["effective_until"] < effective_until)
            ):
                raise ConnectorConflict("rate policy interval crosses a price-rate boundary")
        return (
            sorted(row["price_rate_version_id"] for row in rows),
            sorted(meters),
        )

    @staticmethod
    def _canonical_price_book_at(
        cur: sqlite3.Cursor,
        *,
        connector_profile_ref: str,
        currency: str,
        observed_at: str,
    ) -> tuple[list[str], list[str]]:
        rows = cur.execute(
            "SELECT price_rate_version_id,meter FROM connector_price_rate_versions "
            "WHERE connector_profile_ref=? AND currency=? AND effective_from<=? "
            "AND (effective_until IS NULL OR effective_until>?) "
            "ORDER BY price_rate_version_id",
            (connector_profile_ref, currency, observed_at, observed_at),
        ).fetchall()
        return (
            sorted(row["price_rate_version_id"] for row in rows),
            sorted(row["meter"] for row in rows),
        )

    @classmethod
    def _price_book_matches_at(
        cls, cur: sqlite3.Cursor, policy: Mapping[str, Any], observed_at: str
    ) -> tuple[bool, list[str], list[str]]:
        refs, meters = cls._canonical_price_book_at(
            cur,
            connector_profile_ref=policy["connector_profile_ref"],
            currency=policy["quota_currency"],
            observed_at=observed_at,
        )
        return (
            refs == sorted(policy["price_rate_refs"])
            and meters == sorted(policy["required_price_meters"]),
            refs,
            meters,
        )

    @staticmethod
    def _conservative_price_book_cost(
        cur: sqlite3.Cursor, policy: Mapping[str, Any], profile: Mapping[str, Any]
    ) -> int:
        quantities = {
            "calls": 1,
            "bytes": int(profile["max_response_bytes"]),
            "records": int(profile["max_records"]),
        }
        total = 0
        resolved_meters: set[str] = set()
        for rate_ref in policy["price_rate_refs"]:
            rate = cur.execute(
                "SELECT meter,unit_quantity,unit_price_micros,rounding_mode,currency "
                "FROM connector_price_rate_versions WHERE price_rate_version_id=?",
                (rate_ref,),
            ).fetchone()
            if rate is None:
                raise ConnectorNotFound(rate_ref)
            if rate["currency"] != policy["quota_currency"] or rate["rounding_mode"] != "ceiling":
                raise ConnectorConflict("frozen price book is incompatible with quota policy")
            if rate["meter"] in resolved_meters:
                raise ConnectorConflict("frozen price book has duplicate meter")
            resolved_meters.add(rate["meter"])
            quantity = quantities[rate["meter"]]
            total += (
                quantity * int(rate["unit_price_micros"])
                + int(rate["unit_quantity"]) - 1
            ) // int(rate["unit_quantity"])
        if resolved_meters != set(policy["required_price_meters"]):
            raise ConnectorConflict("frozen price book no longer matches required meters")
        return total

    def reserve_quota(
        self,
        connector_invocation_ref: str,
        policy_version_ref: str,
        physical_attempt_number: int,
        reserved: Mapping[str, Any],
        *,
        ttl_seconds: int,
        reservation_id: str | None = None,
        idempotency_key: str,
    ) -> dict[str, Any]:
        connector_invocation_ref = _text(connector_invocation_ref, "connector_invocation_ref")
        policy_version_ref = _text(policy_version_ref, "policy_version_ref")
        attempt_number = int(_integer(physical_attempt_number, "physical_attempt_number", minimum=1))
        ttl = int(_integer(ttl_seconds, "ttl_seconds", minimum=1))
        metrics = _metric_map(reserved, "reserved", calls_required=True)
        reservation_id = self._idempotent_id(
            "connector-reservation", reservation_id, idempotency_key
        )
        now_dt = self._now_dt()
        now = now_dt.isoformat(timespec="microseconds")
        now_epoch = int(now_dt.timestamp())
        drift_reason: str | None = None
        result: dict[str, Any] | None = None
        with self.store._transaction() as cur:
            policy_row = cur.execute(
                "SELECT policy_ref,record_json,content_hash FROM connector_rate_policy_versions "
                "WHERE policy_version_id=?",
                (policy_version_ref,),
            ).fetchone()
            if policy_row is None:
                raise ConnectorNotFound(policy_version_ref)
            policy = json.loads(policy_row["record_json"])
            active_policy = self._active_rate_policy(cur, policy_row["policy_ref"], now)
            if (
                active_policy["id"] != policy_version_ref
                or active_policy["content_hash"] != policy_row["content_hash"]
            ):
                raise ConnectorBlocked("only the exact active rate policy may admit a call")
            invocation_row = cur.execute(
                "SELECT connector_profile_ref FROM connector_invocations WHERE connector_invocation_id=?",
                (connector_invocation_ref,),
            ).fetchone()
            if invocation_row is None:
                raise ConnectorNotFound(connector_invocation_ref)
            if invocation_row["connector_profile_ref"] != policy["connector_profile_ref"]:
                raise ConnectorConflict("rate policy does not belong to invocation profile")
            profile_row = cur.execute(
                "SELECT record_json FROM connector_profile_versions WHERE profile_version_id=?",
                (invocation_row["connector_profile_ref"],),
            ).fetchone()
            if profile_row is None:
                raise ConnectorNotFound(invocation_row["connector_profile_ref"])
            profile = json.loads(profile_row["record_json"])
            price_book_matches, canonical_rate_refs, canonical_meters = (
                self._price_book_matches_at(cur, policy, now)
            )
            if not price_book_matches:
                drift_reason = "active canonical price book no longer matches quota policy"
                self._open_price_book_drift_incident(
                    cur,
                    connector_invocation_ref=connector_invocation_ref,
                    policy_version_ref=policy_version_ref,
                    policy_rate_refs=policy["price_rate_refs"],
                    canonical_rate_refs=canonical_rate_refs,
                    policy_meters=policy["required_price_meters"],
                    canonical_meters=canonical_meters,
                )
            else:
                minimum_cost = self._conservative_price_book_cost(cur, policy, profile)
                if metrics["cost_micros"] < minimum_cost:
                    raise ConnectorQuotaExceeded(
                        "reserved cost is below the frozen price-book maximum"
                    )
            request = {
                "connector_invocation_ref": connector_invocation_ref,
                "policy_version_ref": policy_version_ref,
                "physical_attempt_number": attempt_number,
                "reserved": metrics,
                "ttl_seconds": ttl,
                "reservation_id": reservation_id,
            }
            request_hash = content_hash(request)
            duplicate = self._idem(cur, idempotency_key, "reserve_connector_quota", request_hash)
            if duplicate is not None and drift_reason is None:
                return duplicate
            blocking = cur.execute(
                "SELECT 1 FROM connector_incidents i WHERE i.connector_profile_ref=? "
                "AND i.severity='blocking' AND (SELECT e.state FROM connector_incident_events e "
                "WHERE e.incident_ref=i.incident_id ORDER BY e.rowid DESC LIMIT 1)='opened' "
                "LIMIT 1",
                (policy["connector_profile_ref"],),
            ).fetchone()
            if blocking is not None and drift_reason is None:
                raise ConnectorBlocked("blocking connector incident is open")
            latest_health = cur.execute(
                "SELECT state FROM connector_source_health_events WHERE connector_profile_ref=? "
                "ORDER BY rowid DESC LIMIT 1",
                (policy["connector_profile_ref"],),
            ).fetchone()
            if latest_health is not None and latest_health["state"] == "open_circuit":
                raise ConnectorBlocked("connector source circuit is open")
            window_seconds = int(policy["window_seconds"])
            window_start_epoch = now_epoch - (now_epoch % window_seconds)
            window_start = datetime.fromtimestamp(window_start_epoch, timezone.utc)
            window_end = window_start + timedelta(seconds=window_seconds)
            expires = min(now_dt + timedelta(seconds=ttl), window_end)
            rows = cur.execute(
                "SELECT r.reservation_id,r.connector_invocation_ref,r.reserved_json,r.expires_at "
                "FROM connector_quota_reservations r WHERE r.quota_scope_ref=? "
                "AND r.window_started_at=?",
                (policy["quota_scope_ref"], window_start.isoformat(timespec="microseconds")),
            ).fetchall()
            active = 0
            totals = {name: 0 for name in _METRICS}
            for row in rows:
                prior = json.loads(row["reserved_json"])
                settlement = cur.execute(
                    "SELECT settlement_id,state,usage_entry_ref,cost_entry_ref,actual_json "
                    "FROM connector_quota_settlements "
                    "WHERE reservation_ref=? ORDER BY revision_number DESC LIMIT 1",
                    (row["reservation_id"],),
                ).fetchone()
                if settlement is not None and settlement["state"] != "released":
                    attempt = cur.execute(
                        "SELECT physical_attempt_id FROM connector_physical_attempts "
                        "WHERE reservation_ref=?",
                        (row["reservation_id"],),
                    ).fetchone()
                    latest_usage = None
                    if attempt is not None:
                        latest_usage = cur.execute(
                            "SELECT usage_entry_id FROM connector_usage_entries "
                            "WHERE physical_attempt_ref=? ORDER BY revision_number DESC LIMIT 1",
                            (attempt["physical_attempt_id"],),
                        ).fetchone()
                    latest_cost = None
                    if latest_usage is not None:
                        latest_cost = cur.execute(
                            "SELECT cost_entry_id FROM connector_cost_entries WHERE usage_entry_ref=? "
                            "ORDER BY revision_number DESC LIMIT 1",
                            (latest_usage["usage_entry_id"],),
                        ).fetchone()
                    if (
                        latest_usage is None
                        or settlement["usage_entry_ref"] != latest_usage["usage_entry_id"]
                        or latest_cost is None
                        or settlement["cost_entry_ref"] != latest_cost["cost_entry_id"]
                    ):
                        drift_reason = "latest Usage/Cost no longer matches quota settlement"
                        self._open_quota_drift_incident(
                            cur,
                            reservation_ref=row["reservation_id"],
                            connector_invocation_ref=row["connector_invocation_ref"],
                            settlement_ref=settlement["settlement_id"],
                            details={
                                "drift_kind": "measurement_revision_drift",
                                "settled_usage_ref": settlement["usage_entry_ref"],
                                "latest_usage_ref": (
                                    None if latest_usage is None else latest_usage["usage_entry_id"]
                                ),
                                "settled_cost_ref": settlement["cost_entry_ref"],
                                "latest_cost_ref": (
                                    None if latest_cost is None else latest_cost["cost_entry_id"]
                                ),
                            },
                        )
                        break
                if settlement is not None and settlement["state"] == "released":
                    projected = {name: 0 for name in _METRICS}
                elif settlement is not None and settlement["state"] == "consumed":
                    projected = json.loads(settlement["actual_json"])
                elif settlement is not None and settlement["state"] == "indeterminate":
                    actual = json.loads(settlement["actual_json"])
                    projected = {name: max(int(prior[name]), int(actual[name])) for name in _METRICS}
                else:
                    projected = prior
                for name in _METRICS:
                    totals[name] += int(projected[name])
                if settlement is None and _timestamp(row["expires_at"], "expires_at") > now:
                    active += 1
            if drift_reason is None:
                if active >= int(policy["max_concurrency"]):
                    raise ConnectorQuotaExceeded("connector concurrency quota exceeded")
                for name in _METRICS:
                    limit = int(policy["limits"][name])
                    if totals[name] + metrics[name] > limit:
                        raise ConnectorQuotaExceeded(f"connector {name} quota exceeded")
                created_at = now_dt.isoformat(timespec="microseconds")
                wire = self._record(
                    {
                        "schema_version": SCHEMA_VERSION,
                        "id": reservation_id,
                        "created_at": created_at,
                        "connector_invocation_ref": connector_invocation_ref,
                        "policy_version_ref": policy_version_ref,
                        "policy_version_hash": policy["content_hash"],
                        "quota_scope_ref": policy["quota_scope_ref"],
                        "physical_attempt_number": attempt_number,
                        "window_started_at": window_start.isoformat(timespec="microseconds"),
                        "window_ends_at": window_end.isoformat(timespec="microseconds"),
                        "expires_at": expires.isoformat(timespec="microseconds"),
                        "reserved": metrics,
                    }
                )
                cur.execute(
                    "INSERT INTO connector_quota_reservations"
                    "(reservation_id,connector_invocation_ref,policy_version_ref,policy_version_hash,"
                    "quota_scope_ref,physical_attempt_number,"
                    "window_started_at,window_ends_at,expires_at,reserved_json,record_json,content_hash,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        wire["id"], connector_invocation_ref, policy_version_ref,
                        wire["policy_version_hash"], wire["quota_scope_ref"], attempt_number,
                        wire["window_started_at"], wire["window_ends_at"], wire["expires_at"],
                        canonical_json(metrics), canonical_json(wire), wire["content_hash"], created_at,
                    ),
                )
                result = {"write_status": "fresh", **wire}
                self._save_idem(
                    cur, idempotency_key, "reserve_connector_quota", request_hash, result
                )
        if drift_reason is not None:
            raise ConnectorBlocked(drift_reason)
        if result is None:
            raise ConnectorConflict("quota admission produced no result")
        return result

    def record_physical_attempt(
        self,
        connector_invocation_ref: str,
        reservation_ref: str,
        physical_attempt_number: int,
        outcome: str,
        *,
        started_at: str,
        completed_at: str | None,
        provider_request_id: str | None = None,
        retry_at: str | None = None,
        physical_attempt_id: str | None = None,
        idempotency_key: str,
    ) -> dict[str, Any]:
        connector_invocation_ref = _text(connector_invocation_ref, "connector_invocation_ref")
        reservation_ref = _text(reservation_ref, "reservation_ref")
        attempt_number = int(_integer(physical_attempt_number, "physical_attempt_number", minimum=1))
        outcome = _text(outcome, "outcome")
        if outcome not in _ATTEMPT_OUTCOMES:
            raise ConnectorValidationError("physical attempt outcome is invalid")
        started_at = _timestamp(started_at, "started_at")  # type: ignore[assignment]
        completed_at = _timestamp(completed_at, "completed_at", nullable=True)
        retry_at = _timestamp(retry_at, "retry_at", nullable=True)
        if completed_at is None:
            raise ConnectorValidationError("terminal physical attempts require completed_at")
        if outcome == "rate_limited" and retry_at is None:
            raise ConnectorValidationError("rate_limited attempts require retry_at")
        if outcome == "rate_limited" and retry_at <= completed_at:
            raise ConnectorValidationError("retry_at must follow completed_at")
        if outcome != "rate_limited" and retry_at is not None:
            raise ConnectorValidationError("retry_at is only valid for rate_limited attempts")
        if completed_at is not None and completed_at < started_at:
            raise ConnectorValidationError("completed_at precedes started_at")
        provider_request_id = (
            None if provider_request_id is None else _text(provider_request_id, "provider_request_id")
        )
        physical_attempt_id = self._idempotent_id(
            "connector-attempt", physical_attempt_id, idempotency_key
        )
        request = {
            "connector_invocation_ref": connector_invocation_ref,
            "reservation_ref": reservation_ref,
            "physical_attempt_number": attempt_number,
            "outcome": outcome,
            "started_at": started_at,
            "completed_at": completed_at,
            "provider_request_id": provider_request_id,
            "retry_at": retry_at,
            "physical_attempt_id": physical_attempt_id,
        }
        request_hash = content_hash(request)
        with self.store._transaction() as cur:
            duplicate = self._idem(cur, idempotency_key, "record_connector_attempt", request_hash)
            if duplicate is not None:
                return duplicate
            reservation = cur.execute(
                "SELECT connector_invocation_ref,physical_attempt_number,created_at,expires_at,"
                "window_ends_at FROM connector_quota_reservations "
                "WHERE reservation_id=?",
                (reservation_ref,),
            ).fetchone()
            if reservation is None:
                raise ConnectorNotFound(reservation_ref)
            if (
                reservation["connector_invocation_ref"] != connector_invocation_ref
                or int(reservation["physical_attempt_number"]) != attempt_number
            ):
                raise ConnectorConflict("physical attempt does not match quota reservation")
            if not (
                reservation["created_at"] <= started_at
                and started_at < min(reservation["expires_at"], reservation["window_ends_at"])
            ):
                raise ConnectorConflict("physical attempt started outside reservation validity")
            settlement = cur.execute(
                "SELECT state FROM connector_quota_settlements WHERE reservation_ref=? "
                "ORDER BY revision_number DESC LIMIT 1",
                (reservation_ref,),
            ).fetchone()
            if settlement is not None and settlement["state"] == "released":
                raise ConnectorConflict("released reservation cannot acquire a physical attempt")
            created_at = self._now()
            if completed_at > created_at:
                raise ConnectorConflict("terminal attempt cannot be completed in the future")
            wire = self._record({"schema_version": SCHEMA_VERSION, "id": physical_attempt_id,
                                 "created_at": created_at, **{k: v for k, v in request.items() if k != "physical_attempt_id"}})
            cur.execute(
                "INSERT INTO connector_physical_attempts"
                "(physical_attempt_id,connector_invocation_ref,physical_attempt_number,reservation_ref,"
                "outcome,started_at,completed_at,provider_request_id,retry_at,record_json,content_hash,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    wire["id"], connector_invocation_ref, attempt_number, reservation_ref, outcome,
                    started_at, completed_at, provider_request_id, retry_at, canonical_json(wire),
                    wire["content_hash"], created_at,
                ),
            )
            result = {"write_status": "fresh", **wire}
            self._save_idem(cur, idempotency_key, "record_connector_attempt", request_hash, result)
            return result

    def record_usage(
        self,
        physical_attempt_ref: str,
        metrics: Mapping[str, Any],
        *,
        measurement_status: str,
        metering_source: str,
        provider_usage_ref: str | None = None,
        usage_entry_id: str | None = None,
        correction_of_ref: str | None = None,
        idempotency_key: str,
    ) -> dict[str, Any]:
        physical_attempt_ref = _text(physical_attempt_ref, "physical_attempt_ref")
        metrics = _metric_map(metrics, "metrics")
        if metrics["calls"] != 1:
            raise ConnectorValidationError("physical attempt usage must account for exactly one call")
        measurement_status = _text(measurement_status, "measurement_status")
        if measurement_status not in {"final", "partial", "estimated", "unavailable"}:
            raise ConnectorValidationError("measurement_status is invalid")
        metering_source = _text(metering_source, "metering_source")
        if metering_source not in {"provider_reported", "runner_measured", "estimated"}:
            raise ConnectorValidationError("metering_source is invalid")
        provider_usage_ref = None if provider_usage_ref is None else _text(provider_usage_ref, "provider_usage_ref")
        usage_entry_id = self._idempotent_id(
            "connector-usage", usage_entry_id, idempotency_key
        )
        correction_of_ref = None if correction_of_ref is None else _text(correction_of_ref, "correction_of_ref")
        request = {
            "physical_attempt_ref": physical_attempt_ref, "metrics": metrics,
            "measurement_status": measurement_status, "metering_source": metering_source,
            "provider_usage_ref": provider_usage_ref, "usage_entry_id": usage_entry_id,
            "correction_of_ref": correction_of_ref,
        }
        request_hash = content_hash(request)
        with self.store._transaction() as cur:
            duplicate = self._idem(cur, idempotency_key, "record_connector_usage", request_hash)
            if duplicate is not None:
                return duplicate
            attempt = cur.execute(
                "SELECT connector_invocation_ref FROM connector_physical_attempts WHERE physical_attempt_id=?",
                (physical_attempt_ref,),
            ).fetchone()
            if attempt is None:
                raise ConnectorNotFound(physical_attempt_ref)
            latest = cur.execute(
                "SELECT usage_entry_id,revision_number FROM connector_usage_entries "
                "WHERE physical_attempt_ref=? ORDER BY revision_number DESC LIMIT 1",
                (physical_attempt_ref,),
            ).fetchone()
            revision = 1 if latest is None else int(latest["revision_number"]) + 1
            expected_correction = None if latest is None else latest["usage_entry_id"]
            if correction_of_ref != expected_correction:
                raise ConnectorConflict("usage correction must point to latest revision")
            created_at = self._now()
            wire = self._record(
                {
                    "schema_version": SCHEMA_VERSION, "id": usage_entry_id,
                    "created_at": created_at, "physical_attempt_ref": physical_attempt_ref,
                    "connector_invocation_ref": attempt["connector_invocation_ref"],
                    "revision": revision, "correction_of_ref": correction_of_ref,
                    "measurement_status": measurement_status, "metering_source": metering_source,
                    "provider_usage_ref": provider_usage_ref, "metrics": metrics,
                }
            )
            cur.execute(
                "INSERT INTO connector_usage_entries"
                "(usage_entry_id,physical_attempt_ref,connector_invocation_ref,revision_number,"
                "correction_of_ref,measurement_status,metering_source,record_json,content_hash,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    usage_entry_id, physical_attempt_ref, attempt["connector_invocation_ref"], revision,
                    correction_of_ref, measurement_status, metering_source, canonical_json(wire),
                    wire["content_hash"], created_at,
                ),
            )
            self._mark_measurement_revision_drift(
                cur,
                physical_attempt_ref=physical_attempt_ref,
                latest_usage_ref=wire["id"],
                latest_cost_ref=None,
            )
            result = {"write_status": "fresh", **wire}
            self._save_idem(cur, idempotency_key, "record_connector_usage", request_hash, result)
            return result

    def settle_quota(
        self,
        reservation_ref: str,
        state: str,
        actual: Mapping[str, Any],
        *,
        usage_entry_ref: str | None = None,
        cost_entry_ref: str | None = None,
        settlement_id: str | None = None,
        correction_of_ref: str | None = None,
        idempotency_key: str,
    ) -> dict[str, Any]:
        reservation_ref = _text(reservation_ref, "reservation_ref")
        state = _text(state, "state")
        if state not in _SETTLEMENT_STATES:
            raise ConnectorValidationError("quota settlement state is invalid")
        actual = _metric_map(actual, "actual")
        if state == "released" and any(actual.values()):
            raise ConnectorValidationError("released reservations require zero actual usage")
        usage_entry_ref = None if usage_entry_ref is None else _text(usage_entry_ref, "usage_entry_ref")
        cost_entry_ref = None if cost_entry_ref is None else _text(cost_entry_ref, "cost_entry_ref")
        if state != "released" and (usage_entry_ref is None or cost_entry_ref is None):
            raise ConnectorValidationError(
                "consumed/indeterminate settlements require usage_entry_ref and cost_entry_ref"
            )
        if state == "released" and (usage_entry_ref is not None or cost_entry_ref is not None):
            raise ConnectorValidationError("released settlement cannot reference usage or cost")
        settlement_id = self._idempotent_id(
            "connector-settlement", settlement_id, idempotency_key
        )
        correction_of_ref = None if correction_of_ref is None else _text(correction_of_ref, "correction_of_ref")
        request = {
            "reservation_ref": reservation_ref, "state": state, "actual": actual,
            "usage_entry_ref": usage_entry_ref, "cost_entry_ref": cost_entry_ref,
            "settlement_id": settlement_id,
            "correction_of_ref": correction_of_ref,
        }
        request_hash = content_hash(request)
        with self.store._transaction() as cur:
            duplicate = self._idem(cur, idempotency_key, "settle_connector_quota", request_hash)
            if duplicate is not None:
                return duplicate
            reservation = cur.execute(
                "SELECT connector_invocation_ref,policy_version_ref,quota_scope_ref,window_started_at,"
                "reserved_json FROM connector_quota_reservations WHERE reservation_id=?",
                (reservation_ref,),
            ).fetchone()
            if reservation is None:
                raise ConnectorNotFound(reservation_ref)
            attempt = cur.execute(
                "SELECT physical_attempt_id FROM connector_physical_attempts WHERE reservation_ref=?",
                (reservation_ref,),
            ).fetchone()
            if state == "released":
                if attempt is not None:
                    raise ConnectorConflict("a reservation with a physical attempt cannot be released")
            else:
                if attempt is None:
                    raise ConnectorConflict("consumed/indeterminate settlement requires exact attempt")
                usage = cur.execute(
                    "SELECT physical_attempt_ref,measurement_status,record_json "
                    "FROM connector_usage_entries "
                    "WHERE usage_entry_id=?",
                    (usage_entry_ref,),
                ).fetchone()
                if usage is None or usage["physical_attempt_ref"] != attempt["physical_attempt_id"]:
                    raise ConnectorConflict("settlement usage must belong to the reserved physical attempt")
                latest_usage = cur.execute(
                    "SELECT usage_entry_id FROM connector_usage_entries WHERE physical_attempt_ref=? "
                    "ORDER BY revision_number DESC LIMIT 1",
                    (attempt["physical_attempt_id"],),
                ).fetchone()
                if latest_usage is None or latest_usage["usage_entry_id"] != usage_entry_ref:
                    raise ConnectorConflict("settlement must reference the latest usage revision")
                if state == "consumed" and usage["measurement_status"] != "final":
                    raise ConnectorConflict("consumed settlement requires final usage")
                usage_wire = json.loads(usage["record_json"])
                for metric in ("calls", "bytes", "records"):
                    if actual[metric] != usage_wire["metrics"][metric]:
                        raise ConnectorConflict(
                            "settlement physical metrics must equal the referenced usage"
                        )
                latest_cost = cur.execute(
                    "SELECT cost_entry_id FROM connector_cost_entries WHERE usage_entry_ref=? "
                    "ORDER BY revision_number DESC LIMIT 1",
                    (usage_entry_ref,),
                ).fetchone()
                if latest_cost is None or latest_cost["cost_entry_id"] != cost_entry_ref:
                    raise ConnectorConflict("settlement must reference the latest cost revision")
                cost = cur.execute(
                    "SELECT amount_micros,currency,cost_status FROM connector_cost_entries "
                    "WHERE cost_entry_id=? AND usage_entry_ref=?",
                    (cost_entry_ref, usage_entry_ref),
                ).fetchone()
                if cost is None:
                    raise ConnectorConflict("settlement cost does not belong to usage")
                policy = json.loads(
                    cur.execute(
                        "SELECT record_json FROM connector_rate_policy_versions "
                        "WHERE policy_version_id=?",
                        (reservation["policy_version_ref"],),
                    ).fetchone()["record_json"]
                )
                if cost["currency"] != policy["quota_currency"]:
                    raise ConnectorConflict("settlement cost currency does not match quota policy")
                if state == "consumed" and cost["cost_status"] not in {"actual", "waived"}:
                    raise ConnectorConflict("consumed settlement requires actual or waived cost")
                expected_cost = 0 if cost["amount_micros"] is None else int(cost["amount_micros"])
                if actual["cost_micros"] != expected_cost:
                    raise ConnectorConflict("settlement cost must equal the latest CostEntry")
            latest = cur.execute(
                "SELECT settlement_id,revision_number FROM connector_quota_settlements "
                "WHERE reservation_ref=? ORDER BY revision_number DESC LIMIT 1",
                (reservation_ref,),
            ).fetchone()
            revision = 1 if latest is None else int(latest["revision_number"]) + 1
            expected_correction = None if latest is None else latest["settlement_id"]
            if correction_of_ref != expected_correction:
                raise ConnectorConflict("quota correction must point to latest settlement")
            created_at = self._now()
            wire = self._record(
                {
                    "schema_version": SCHEMA_VERSION, "id": settlement_id,
                    "created_at": created_at, "reservation_ref": reservation_ref,
                    "revision": revision, "correction_of_ref": correction_of_ref,
                    "state": state, "usage_entry_ref": usage_entry_ref,
                    "cost_entry_ref": cost_entry_ref, "actual": actual,
                }
            )
            cur.execute(
                "INSERT INTO connector_quota_settlements"
                "(settlement_id,reservation_ref,revision_number,correction_of_ref,state,"
                "usage_entry_ref,cost_entry_ref,actual_json,record_json,content_hash,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    settlement_id, reservation_ref, revision, correction_of_ref, state,
                    usage_entry_ref, cost_entry_ref, canonical_json(actual), canonical_json(wire),
                    wire["content_hash"],
                    created_at,
                ),
            )
            if state != "released":
                reserved_metrics = json.loads(reservation["reserved_json"])
                policy_row = cur.execute(
                    "SELECT record_json FROM connector_rate_policy_versions WHERE policy_version_id=?",
                    (reservation["policy_version_ref"],),
                ).fetchone()
                if policy_row is None:
                    raise ConnectorNotFound(reservation["policy_version_ref"])
                policy = json.loads(policy_row["record_json"])
                scope_rows = cur.execute(
                    "SELECT reservation_id,reserved_json FROM connector_quota_reservations "
                    "WHERE quota_scope_ref=? AND window_started_at=?",
                    (reservation["quota_scope_ref"], reservation["window_started_at"]),
                ).fetchall()
                totals = {name: 0 for name in _METRICS}
                for scope_row in scope_rows:
                    scope_reserved = json.loads(scope_row["reserved_json"])
                    scope_settlement = cur.execute(
                        "SELECT state,actual_json FROM connector_quota_settlements "
                        "WHERE reservation_ref=? ORDER BY revision_number DESC LIMIT 1",
                        (scope_row["reservation_id"],),
                    ).fetchone()
                    if scope_settlement is None:
                        projected = scope_reserved
                    elif scope_settlement["state"] == "released":
                        projected = {name: 0 for name in _METRICS}
                    elif scope_settlement["state"] == "consumed":
                        projected = json.loads(scope_settlement["actual_json"])
                    else:
                        measured = json.loads(scope_settlement["actual_json"])
                        projected = {
                            name: max(int(scope_reserved[name]), int(measured[name]))
                            for name in _METRICS
                        }
                    for name in _METRICS:
                        totals[name] += int(projected[name])
                exceeded = sorted(
                    name
                    for name in _METRICS
                    if actual[name] > reserved_metrics[name]
                    or totals[name] > int(policy["limits"][name])
                )
                if exceeded:
                    self._open_quota_drift_incident(
                        cur,
                        reservation_ref=reservation_ref,
                        connector_invocation_ref=reservation["connector_invocation_ref"],
                        settlement_ref=settlement_id,
                        details={
                            "exceeded_metrics": exceeded,
                            "reserved": reserved_metrics,
                            "actual": actual,
                            "window_totals": totals,
                            "window_limits": policy["limits"],
                        },
                    )
            result = {"write_status": "fresh", **wire}
            self._save_idem(cur, idempotency_key, "settle_connector_quota", request_hash, result)
            return result

    def register_price_rate(
        self, spec: Mapping[str, Any], *, idempotency_key: str | None = None
    ) -> dict[str, Any]:
        fields = {
            "schema_version", "id", "created_at", "price_rate_ref", "version",
            "prior_version_ref", "connector_profile_ref", "meter", "unit_quantity",
            "unit_price_micros", "rounding_mode", "currency", "effective_from", "effective_until",
            "source_ref", "actor_ref",
        }
        wire = _closed(spec, fields, "ConnectorPriceRateVersion")
        if wire["schema_version"] != SCHEMA_VERSION:
            raise ConnectorValidationError("unsupported ConnectorPriceRateVersion schema_version")
        for name in (
            "id", "price_rate_ref", "connector_profile_ref", "meter", "source_ref", "actor_ref"
        ):
            wire[name] = _text(wire[name], name)
        if wire["meter"] not in {"calls", "bytes", "records"}:
            raise ConnectorValidationError("price meter is invalid")
        if wire["rounding_mode"] != "ceiling":
            raise ConnectorValidationError("Connector P0 price rounding_mode must be ceiling")
        wire["prior_version_ref"] = (
            None if wire["prior_version_ref"] is None else _text(wire["prior_version_ref"], "prior_version_ref")
        )
        wire["created_at"] = _timestamp(wire["created_at"], "created_at")
        wire["effective_from"] = _timestamp(wire["effective_from"], "effective_from")
        wire["effective_until"] = _timestamp(
            wire["effective_until"], "effective_until", nullable=True
        )
        if wire["effective_until"] is not None and wire["effective_until"] <= wire["effective_from"]:
            raise ConnectorValidationError("effective_until must be after effective_from")
        wire["version"] = _integer(wire["version"], "version", minimum=1)
        wire["unit_quantity"] = _integer(wire["unit_quantity"], "unit_quantity", minimum=1)
        wire["unit_price_micros"] = _integer(wire["unit_price_micros"], "unit_price_micros")
        if not isinstance(wire["currency"], str) or not _CURRENCY_RE.fullmatch(wire["currency"]):
            raise ConnectorValidationError("currency must be uppercase ISO-4217")
        self.get_profile(wire["connector_profile_ref"])
        wire = self._record(wire)
        request_hash = content_hash(wire)
        with self.store._transaction() as cur:
            duplicate = self._idem(cur, idempotency_key, "register_connector_price_rate", request_hash)
            if duplicate is not None:
                return duplicate
            latest = cur.execute(
                "SELECT price_rate_version_id,version_number FROM connector_price_rate_versions "
                "WHERE price_rate_ref=? ORDER BY version_number DESC LIMIT 1",
                (wire["price_rate_ref"],),
            ).fetchone()
            expected_version = 1 if latest is None else int(latest["version_number"]) + 1
            expected_prior = None if latest is None else latest["price_rate_version_id"]
            if wire["version"] != expected_version or wire["prior_version_ref"] != expected_prior:
                raise ConnectorConflict("price rate version chain is not contiguous")
            overlapping = cur.execute(
                "SELECT price_rate_version_id FROM connector_price_rate_versions "
                "WHERE connector_profile_ref=? AND meter=? AND currency=? "
                "AND effective_from < COALESCE(?, '9999-12-31T23:59:59.999999+00:00') "
                "AND COALESCE(effective_until, '9999-12-31T23:59:59.999999+00:00') > ? LIMIT 1",
                (
                    wire["connector_profile_ref"], wire["meter"], wire["currency"],
                    wire["effective_until"], wire["effective_from"],
                ),
            ).fetchone()
            if overlapping is not None:
                raise ConnectorConflict("price rate versions cannot overlap")
            cur.execute(
                "INSERT INTO connector_price_rate_versions"
                "(price_rate_version_id,price_rate_ref,version_number,prior_version_ref,"
                "connector_profile_ref,meter,unit_quantity,unit_price_micros,rounding_mode,currency,"
                "effective_from,effective_until,record_json,content_hash,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    wire["id"], wire["price_rate_ref"], wire["version"], wire["prior_version_ref"],
                    wire["connector_profile_ref"], wire["meter"], wire["unit_quantity"],
                    wire["unit_price_micros"], wire["rounding_mode"], wire["currency"], wire["effective_from"],
                    wire["effective_until"], canonical_json(wire), wire["content_hash"], wire["created_at"],
                ),
            )
            result = {"write_status": "fresh", **wire}
            self._save_idem(cur, idempotency_key, "register_connector_price_rate", request_hash, result)
            return result

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
        cost_entry_id: str | None = None,
        correction_of_ref: str | None = None,
        idempotency_key: str,
    ) -> dict[str, Any]:
        result = self._record_cost_authority(
            usage_entry_ref,
            price_rate_refs=price_rate_refs,
            amount_micros=amount_micros,
            currency=currency,
            cost_status=cost_status,
            calculation_ref=calculation_ref,
            actor_ref=actor_ref,
            cost_entry_id=cost_entry_id,
            correction_of_ref=correction_of_ref,
            idempotency_key=idempotency_key,
        )
        blocked_reason = result.pop("_blocked_reason", None)
        if blocked_reason is not None:
            raise ConnectorBlocked(blocked_reason)
        return result

    def _record_cost_authority(
        self,
        usage_entry_ref: str,
        *,
        price_rate_refs: Sequence[str],
        amount_micros: int | None,
        currency: str,
        cost_status: str,
        calculation_ref: str,
        actor_ref: str,
        cost_entry_id: str | None = None,
        correction_of_ref: str | None = None,
        idempotency_key: str,
    ) -> dict[str, Any]:
        usage_entry_ref = _text(usage_entry_ref, "usage_entry_ref")
        rate_refs = sorted(_refs(price_rate_refs, "price_rate_refs"))
        amount_micros = _integer(amount_micros, "amount_micros", nullable=True)
        if not isinstance(currency, str) or not _CURRENCY_RE.fullmatch(currency):
            raise ConnectorValidationError("currency must be uppercase ISO-4217")
        cost_status = _text(cost_status, "cost_status")
        if cost_status not in {"actual", "estimated", "unpriced", "waived"}:
            raise ConnectorValidationError("cost_status is invalid")
        if cost_status == "unpriced" and (amount_micros is not None or rate_refs):
            raise ConnectorValidationError("unpriced cost requires null amount and no rate")
        if cost_status == "waived" and (amount_micros != 0 or rate_refs):
            raise ConnectorValidationError("waived cost requires zero amount and no rate")
        if cost_status in {"actual", "estimated"} and (amount_micros is None or not rate_refs):
            raise ConnectorValidationError("priced costs require amount and rate refs")
        calculation_ref = _text(calculation_ref, "calculation_ref")
        actor_ref = _text(actor_ref, "actor_ref")
        cost_entry_id = self._idempotent_id("connector-cost", cost_entry_id, idempotency_key)
        correction_of_ref = None if correction_of_ref is None else _text(correction_of_ref, "correction_of_ref")
        request = {
            "usage_entry_ref": usage_entry_ref, "price_rate_refs": rate_refs,
            "amount_micros": amount_micros, "currency": currency, "cost_status": cost_status,
            "calculation_ref": calculation_ref, "actor_ref": actor_ref,
            "cost_entry_id": cost_entry_id, "correction_of_ref": correction_of_ref,
        }
        request_hash = content_hash(request)
        with self.store._transaction() as cur:
            duplicate = self._idem(cur, idempotency_key, "record_connector_cost", request_hash)
            if duplicate is not None:
                return duplicate
            usage_row = cur.execute(
                "SELECT u.connector_invocation_ref,u.physical_attempt_ref,u.record_json,a.started_at,"
                "a.reservation_ref "
                "FROM connector_usage_entries u JOIN connector_physical_attempts a "
                "ON a.physical_attempt_id=u.physical_attempt_ref WHERE u.usage_entry_id=?",
                (usage_entry_ref,),
            ).fetchone()
            if usage_row is None:
                raise ConnectorNotFound(usage_entry_ref)
            invocation = cur.execute(
                "SELECT connector_profile_ref FROM connector_invocations WHERE connector_invocation_id=?",
                (usage_row["connector_invocation_ref"],),
            ).fetchone()
            usage = json.loads(usage_row["record_json"])
            policy_row = cur.execute(
                "SELECT p.record_json FROM connector_quota_reservations r "
                "JOIN connector_rate_policy_versions p ON p.policy_version_id=r.policy_version_ref "
                "WHERE r.reservation_id=?",
                (usage_row["reservation_ref"],),
            ).fetchone()
            if policy_row is None:
                raise ConnectorConflict("usage has no frozen quota/price policy")
            policy = json.loads(policy_row["record_json"])
            if currency != policy["quota_currency"]:
                raise ConnectorConflict("cost currency does not match frozen quota policy")
            price_book_matches, canonical_rate_refs, canonical_meters = (
                self._price_book_matches_at(
                cur, policy, usage_row["started_at"]
                )
            )
            if not price_book_matches:
                blocked_reason = (
                    "cost cannot use a quota policy with a drifted price book"
                )
                self._open_price_book_drift_incident(
                    cur,
                    connector_invocation_ref=usage_row["connector_invocation_ref"],
                    reservation_ref=usage_row["reservation_ref"],
                    policy_version_ref=policy["id"],
                    physical_attempt_ref=usage_row["physical_attempt_ref"],
                    observed_at=usage_row["started_at"],
                    policy_rate_refs=policy["price_rate_refs"],
                    canonical_rate_refs=canonical_rate_refs,
                    policy_meters=policy["required_price_meters"],
                    canonical_meters=canonical_meters,
                )
                return {"_blocked_reason": blocked_reason}
            expected_rate_refs = sorted(policy["price_rate_refs"])
            if cost_status in {"actual", "estimated"} and rate_refs != expected_rate_refs:
                raise ConnectorConflict("cost must use the exact frozen price book")
            if cost_status in {"waived", "unpriced"} and policy["required_price_meters"]:
                raise ConnectorConflict("required priced meters cannot be waived or unpriced")
            calculated = 0
            priced_meters: set[str] = set()
            for rate_ref in rate_refs:
                rate_row = cur.execute(
                    "SELECT * FROM connector_price_rate_versions WHERE price_rate_version_id=?",
                    (rate_ref,),
                ).fetchone()
                if rate_row is None:
                    raise ConnectorNotFound(rate_ref)
                if rate_row["connector_profile_ref"] != invocation["connector_profile_ref"]:
                    raise ConnectorConflict("price rate belongs to another connector profile")
                if rate_row["currency"] != currency:
                    raise ConnectorConflict("cost cannot combine currencies")
                if rate_row["meter"] in priced_meters:
                    raise ConnectorConflict("cost cannot apply multiple rates to one meter")
                priced_meters.add(rate_row["meter"])
                if rate_row["rounding_mode"] != "ceiling":
                    raise ConnectorConflict("unsupported price rounding mode")
                if usage_row["started_at"] < rate_row["effective_from"] or (
                    rate_row["effective_until"] is not None
                    and usage_row["started_at"] >= rate_row["effective_until"]
                ):
                    raise ConnectorConflict("price rate was not effective for physical attempt")
                quantity = int(usage["metrics"][rate_row["meter"]])
                calculated += (
                    quantity * int(rate_row["unit_price_micros"])
                    + int(rate_row["unit_quantity"]) - 1
                ) // int(rate_row["unit_quantity"])
            if cost_status in {"actual", "estimated"} and amount_micros != calculated:
                raise ConnectorConflict("amount_micros does not match exact price rates")
            latest = cur.execute(
                "SELECT cost_entry_id,revision_number FROM connector_cost_entries "
                "WHERE usage_entry_ref=? ORDER BY revision_number DESC LIMIT 1",
                (usage_entry_ref,),
            ).fetchone()
            revision = 1 if latest is None else int(latest["revision_number"]) + 1
            expected_correction = None if latest is None else latest["cost_entry_id"]
            if correction_of_ref != expected_correction:
                raise ConnectorConflict("cost correction must point to latest revision")
            created_at = self._now()
            wire = self._record(
                {
                    "schema_version": SCHEMA_VERSION, "id": cost_entry_id,
                    "created_at": created_at, "usage_entry_ref": usage_entry_ref,
                    "revision": revision, "correction_of_ref": correction_of_ref,
                    "price_rate_refs": rate_refs, "amount_micros": amount_micros,
                    "currency": currency, "cost_status": cost_status,
                    "calculation_ref": calculation_ref, "actor_ref": actor_ref,
                }
            )
            cur.execute(
                "INSERT INTO connector_cost_entries"
                "(cost_entry_id,usage_entry_ref,revision_number,correction_of_ref,amount_micros,"
                "currency,cost_status,record_json,content_hash,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    cost_entry_id, usage_entry_ref, revision, correction_of_ref, amount_micros,
                    currency, cost_status, canonical_json(wire), wire["content_hash"], created_at,
                ),
            )
            self._mark_measurement_revision_drift(
                cur,
                physical_attempt_ref=usage_row["physical_attempt_ref"],
                latest_usage_ref=usage_entry_ref,
                latest_cost_ref=wire["id"],
            )
            result = {"write_status": "fresh", **wire}
            self._save_idem(cur, idempotency_key, "record_connector_cost", request_hash, result)
            return result

    def record_source_envelope(
        self, spec: Mapping[str, Any], *, idempotency_key: str
    ) -> dict[str, Any]:
        fields = {
            "schema_version", "id", "created_at", "connector_invocation_ref",
            "connector_profile_ref", "physical_attempt_refs", "result_physical_attempt_ref",
            "source", "operation",
            "source_record_refs", "published_at", "updated_at", "as_of", "retrieved_at",
            "cursor", "provider_request_id", "raw_artifact_version_ref", "raw_response_hash",
            "source_schema_hash", "source_content_hash", "completeness", "status",
            "access_policy_ref", "retention_policy_ref", "terms_policy_ref", "error",
        }
        wire = _closed(spec, fields, "SourceEnvelope")
        if wire["schema_version"] != SCHEMA_VERSION:
            raise ConnectorValidationError("unsupported SourceEnvelope schema_version")
        for name in (
            "id", "connector_invocation_ref", "connector_profile_ref",
            "result_physical_attempt_ref", "source", "operation",
            "raw_artifact_version_ref", "access_policy_ref", "retention_policy_ref",
            "terms_policy_ref",
        ):
            wire[name] = _text(wire[name], name)
        wire["created_at"] = _timestamp(wire["created_at"], "created_at")
        for name in ("published_at", "updated_at", "as_of"):
            wire[name] = _timestamp(wire[name], name, nullable=True)
        wire["retrieved_at"] = _timestamp(wire["retrieved_at"], "retrieved_at")
        wire["cursor"] = None if wire["cursor"] is None else _text(wire["cursor"], "cursor")
        wire["provider_request_id"] = (
            None if wire["provider_request_id"] is None else _text(wire["provider_request_id"], "provider_request_id")
        )
        wire["physical_attempt_refs"] = _refs(
            wire["physical_attempt_refs"], "physical_attempt_refs", nonempty=True
        )
        if wire["result_physical_attempt_ref"] not in wire["physical_attempt_refs"]:
            raise ConnectorValidationError(
                "result_physical_attempt_ref must be listed in physical_attempt_refs"
            )
        wire["source_record_refs"] = _refs(wire["source_record_refs"], "source_record_refs")
        for name in ("raw_response_hash", "source_schema_hash", "source_content_hash"):
            wire[name] = _hash(wire[name], name)
        if wire["completeness"] not in _COMPLETENESS:
            raise ConnectorValidationError("SourceEnvelope completeness is invalid")
        if wire["status"] not in {"complete", "partial", "empty", "error"}:
            raise ConnectorValidationError("SourceEnvelope status is invalid")
        if (wire["status"] == "error") != (wire["error"] is not None):
            raise ConnectorValidationError("SourceEnvelope error/status mismatch")
        if wire["error"] is not None:
            wire["error"] = _closed(wire["error"], {"code", "message", "retryable"}, "error")
            wire["error"]["code"] = _text(wire["error"]["code"], "error.code")
            wire["error"]["message"] = _text(wire["error"]["message"], "error.message")
            if type(wire["error"]["retryable"]) is not bool:
                raise ConnectorValidationError("error.retryable must be boolean")
        wire = self._record(wire)
        request_hash = content_hash(wire)
        with self.store._transaction() as cur:
            duplicate = self._idem(cur, idempotency_key, "record_source_envelope", request_hash)
            if duplicate is not None:
                return duplicate
            invocation = cur.execute(
                "SELECT connector_profile_ref,call_spec_ref,execution_ref FROM connector_invocations "
                "WHERE connector_invocation_id=?",
                (wire["connector_invocation_ref"],),
            ).fetchone()
            if invocation is None or invocation["connector_profile_ref"] != wire["connector_profile_ref"]:
                raise ConnectorConflict("SourceEnvelope invocation/profile mismatch")
            profile = self.get_profile(wire["connector_profile_ref"])
            call = self.get_call_spec(invocation["call_spec_ref"])
            if wire["source"] != profile["source_identity"]["source_ref"]:
                raise ConnectorConflict("SourceEnvelope source does not match frozen profile")
            if wire["operation"] != call["operation"]:
                raise ConnectorConflict("SourceEnvelope operation does not match CallSpec")
            if wire["source_schema_hash"] != profile["output_schema_hashes"][wire["operation"]]:
                raise ConnectorConflict("SourceEnvelope schema hash does not match profile")
            for name in ("access_policy_ref", "retention_policy_ref", "terms_policy_ref"):
                if wire[name] != profile[name]:
                    raise ConnectorConflict(f"SourceEnvelope {name} does not match profile")
            result_attempt: sqlite3.Row | None = None
            for attempt_ref in wire["physical_attempt_refs"]:
                attempt = cur.execute(
                    "SELECT connector_invocation_ref,outcome,provider_request_id FROM "
                    "connector_physical_attempts WHERE physical_attempt_id=?",
                    (attempt_ref,),
                ).fetchone()
                if attempt is None or attempt["connector_invocation_ref"] != wire["connector_invocation_ref"]:
                    raise ConnectorConflict("SourceEnvelope attempt does not belong to invocation")
                if attempt_ref == wire["result_physical_attempt_ref"]:
                    result_attempt = attempt
            if result_attempt is None:
                raise ConnectorConflict("result physical attempt was not resolved")
            if wire["provider_request_id"] != result_attempt["provider_request_id"]:
                raise ConnectorConflict("provider_request_id must match the result attempt")
            if wire["status"] in {"complete", "empty"} and result_attempt["outcome"] != "succeeded":
                raise ConnectorConflict("complete/empty result attempt must be succeeded")
            if wire["status"] == "complete" and not wire["source_record_refs"]:
                raise ConnectorConflict("complete SourceEnvelope requires source records")
            if wire["status"] == "empty" and wire["source_record_refs"]:
                raise ConnectorConflict("empty SourceEnvelope cannot contain source records")
            if wire["status"] == "partial" and wire["completeness"] != "partial":
                raise ConnectorConflict("partial SourceEnvelope requires partial completeness")
            if wire["status"] == "error" and (
                result_attempt["outcome"] == "succeeded" or wire["source_record_refs"]
            ):
                raise ConnectorConflict("error result attempt cannot claim successful records")
            if wire["source_content_hash"] != source_envelope_content_hash(wire):
                raise ConnectorConflict("source_content_hash does not bind normalized source content")
            artifact_table = cur.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' "
                "AND name='observability_artifact_version_index'"
            ).fetchone()
            artifact = None
            if artifact_table is not None:
                artifact = cur.execute(
                    "SELECT i.schema_version,i.producer_execution_ref,v.artifact_content_hash "
                    "FROM observability_artifact_version_index i "
                    "LEFT JOIN observability_artifact_versions_v2 v ON v.version_id=i.version_id "
                    "WHERE i.version_id=?",
                    (wire["raw_artifact_version_ref"],),
                ).fetchone()
            if (
                artifact is None
                or artifact["schema_version"] != "0.2"
                or artifact["producer_execution_ref"] != invocation["execution_ref"]
                or artifact["artifact_content_hash"] != wire["raw_response_hash"]
            ):
                raise ConnectorConflict(
                    "raw artifact version/hash/producer must match the connector execution"
                )
            cur.execute(
                "INSERT INTO connector_source_envelopes"
                "(source_envelope_id,connector_invocation_ref,connector_profile_ref,"
                "raw_artifact_version_ref,"
                "raw_response_hash,completeness,status,record_json,content_hash,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    wire["id"], wire["connector_invocation_ref"], wire["connector_profile_ref"],
                    wire["raw_artifact_version_ref"], wire["raw_response_hash"], wire["completeness"],
                    wire["status"], canonical_json(wire), wire["content_hash"], wire["created_at"],
                ),
            )
            result = {"write_status": "fresh", **wire}
            self._save_idem(cur, idempotency_key, "record_source_envelope", request_hash, result)
            return result

    def _mark_measurement_revision_drift(
        self,
        cur: sqlite3.Cursor,
        *,
        physical_attempt_ref: str,
        latest_usage_ref: str,
        latest_cost_ref: str | None,
    ) -> None:
        attempt = cur.execute(
            "SELECT connector_invocation_ref,reservation_ref FROM connector_physical_attempts "
            "WHERE physical_attempt_id=?",
            (physical_attempt_ref,),
        ).fetchone()
        if attempt is None:
            raise ConnectorNotFound(physical_attempt_ref)
        settlement = cur.execute(
            "SELECT settlement_id,state,usage_entry_ref,cost_entry_ref "
            "FROM connector_quota_settlements WHERE reservation_ref=? "
            "ORDER BY revision_number DESC LIMIT 1",
            (attempt["reservation_ref"],),
        ).fetchone()
        if settlement is None or settlement["state"] == "released":
            return
        usage_drift = settlement["usage_entry_ref"] != latest_usage_ref
        cost_drift = (
            latest_cost_ref is not None and settlement["cost_entry_ref"] != latest_cost_ref
        )
        if not usage_drift and not cost_drift:
            return
        self._open_quota_drift_incident(
            cur,
            reservation_ref=attempt["reservation_ref"],
            connector_invocation_ref=attempt["connector_invocation_ref"],
            settlement_ref=settlement["settlement_id"],
            details={
                "drift_kind": "measurement_revision_drift",
                "settled_usage_ref": settlement["usage_entry_ref"],
                "latest_usage_ref": latest_usage_ref,
                "settled_cost_ref": settlement["cost_entry_ref"],
                "latest_cost_ref": latest_cost_ref,
            },
        )

    def _open_quota_drift_incident(
        self,
        cur: sqlite3.Cursor,
        *,
        reservation_ref: str,
        connector_invocation_ref: str,
        settlement_ref: str,
        details: Mapping[str, Any],
    ) -> None:
        invocation = cur.execute(
            "SELECT connector_profile_ref FROM connector_invocations "
            "WHERE connector_invocation_id=?",
            (connector_invocation_ref,),
        ).fetchone()
        if invocation is None:
            raise ConnectorNotFound(connector_invocation_ref)
        actor_ref = "system:connector-quota"
        detail_wire = {**dict(details), "settlement_ref": settlement_ref}
        incident_id = self._idempotent_id(
            "connector-incident", None,
            f"quota-drift:{settlement_ref}:{content_hash(detail_wire)}",
        )
        created_at = self._now()
        wire = self._record(
            {
                "schema_version": SCHEMA_VERSION,
                "id": incident_id,
                "created_at": created_at,
                "connector_profile_ref": invocation["connector_profile_ref"],
                "incident_type": "quota_drift",
                "severity": "blocking",
                "details": detail_wire,
                "actor_ref": actor_ref,
                "connector_invocation_ref": connector_invocation_ref,
                "reservation_ref": reservation_ref,
            }
        )
        cur.execute(
            "INSERT INTO connector_incidents"
            "(incident_id,connector_profile_ref,connector_invocation_ref,reservation_ref,"
            "incident_type,severity,record_json,content_hash,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (
                incident_id, invocation["connector_profile_ref"], connector_invocation_ref,
                reservation_ref, "quota_drift", "blocking", canonical_json(wire),
                wire["content_hash"], created_at,
            ),
        )
        self._append_incident_event(cur, incident_id, "opened", actor_ref, None)

    def _open_price_book_drift_incident(
        self,
        cur: sqlite3.Cursor,
        *,
        connector_invocation_ref: str,
        reservation_ref: str | None = None,
        policy_version_ref: str,
        physical_attempt_ref: str | None = None,
        observed_at: str | None = None,
        policy_rate_refs: Sequence[str],
        canonical_rate_refs: Sequence[str],
        policy_meters: Sequence[str],
        canonical_meters: Sequence[str],
    ) -> None:
        invocation = cur.execute(
            "SELECT connector_profile_ref FROM connector_invocations "
            "WHERE connector_invocation_id=?",
            (connector_invocation_ref,),
        ).fetchone()
        if invocation is None:
            raise ConnectorNotFound(connector_invocation_ref)
        details = {
            "drift_kind": "price_book_drift",
            "policy_version_ref": policy_version_ref,
            "physical_attempt_ref": physical_attempt_ref,
            "observed_at": observed_at,
            "policy_rate_refs": sorted(policy_rate_refs),
            "canonical_rate_refs": sorted(canonical_rate_refs),
            "policy_meters": sorted(policy_meters),
            "canonical_meters": sorted(canonical_meters),
        }
        existing = cur.execute(
            "SELECT i.incident_id FROM connector_incidents i "
            "WHERE i.connector_profile_ref=? AND i.incident_type='policy_violation' "
            "AND i.severity='blocking' AND json_extract(i.record_json,'$.details.drift_kind')="
            "'price_book_drift' AND (SELECT e.state FROM connector_incident_events e "
            "WHERE e.incident_ref=i.incident_id ORDER BY e.rowid DESC LIMIT 1)='opened' LIMIT 1",
            (invocation["connector_profile_ref"],),
        ).fetchone()
        if existing is not None:
            return
        actor_ref = "system:connector-pricing"
        incident_id = self._idempotent_id(
            "connector-incident",
            None,
            f"price-book-drift:{connector_invocation_ref}:{content_hash(details)}",
        )
        created_at = self._now()
        wire = self._record(
            {
                "schema_version": SCHEMA_VERSION,
                "id": incident_id,
                "created_at": created_at,
                "connector_profile_ref": invocation["connector_profile_ref"],
                "incident_type": "policy_violation",
                "severity": "blocking",
                "details": details,
                "actor_ref": actor_ref,
                "connector_invocation_ref": connector_invocation_ref,
                "reservation_ref": reservation_ref,
            }
        )
        cur.execute(
            "INSERT INTO connector_incidents"
            "(incident_id,connector_profile_ref,connector_invocation_ref,reservation_ref,"
            "incident_type,severity,record_json,content_hash,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (
                incident_id,
                invocation["connector_profile_ref"],
                connector_invocation_ref,
                reservation_ref,
                "policy_violation",
                "blocking",
                canonical_json(wire),
                wire["content_hash"],
                created_at,
            ),
        )
        self._append_incident_event(cur, incident_id, "opened", actor_ref, None)

    def open_incident(
        self,
        connector_profile_ref: str,
        incident_type: str,
        severity: str,
        details: Mapping[str, Any],
        *,
        actor_ref: str,
        connector_invocation_ref: str | None = None,
        reservation_ref: str | None = None,
        incident_id: str | None = None,
        idempotency_key: str,
    ) -> dict[str, Any]:
        connector_profile_ref = _text(connector_profile_ref, "connector_profile_ref")
        incident_type = _text(incident_type, "incident_type")
        if incident_type not in _INCIDENT_TYPES:
            raise ConnectorValidationError("incident_type is invalid")
        severity = _text(severity, "severity")
        if severity not in {"warning", "blocking"}:
            raise ConnectorValidationError("severity is invalid")
        if not isinstance(details, Mapping):
            raise ConnectorValidationError("details must be an object")
        details = json.loads(canonical_json(details))
        actor_ref = _text(actor_ref, "actor_ref")
        connector_invocation_ref = None if connector_invocation_ref is None else _text(connector_invocation_ref, "connector_invocation_ref")
        reservation_ref = None if reservation_ref is None else _text(reservation_ref, "reservation_ref")
        incident_id = self._idempotent_id(
            "connector-incident", incident_id, idempotency_key
        )
        request = {
            "connector_profile_ref": connector_profile_ref, "incident_type": incident_type,
            "severity": severity, "details": details, "actor_ref": actor_ref,
            "connector_invocation_ref": connector_invocation_ref,
            "reservation_ref": reservation_ref, "incident_id": incident_id,
        }
        request_hash = content_hash(request)
        with self.store._transaction() as cur:
            duplicate = self._idem(cur, idempotency_key, "open_connector_incident", request_hash)
            if duplicate is not None:
                return duplicate
            self._row_record(
                cur.execute(
                    "SELECT record_json FROM connector_profile_versions WHERE profile_version_id=?",
                    (connector_profile_ref,),
                ).fetchone(),
                connector_profile_ref,
            )
            if connector_invocation_ref is not None:
                invocation = cur.execute(
                    "SELECT connector_profile_ref FROM connector_invocations "
                    "WHERE connector_invocation_id=?",
                    (connector_invocation_ref,),
                ).fetchone()
                if invocation is None or invocation["connector_profile_ref"] != connector_profile_ref:
                    raise ConnectorConflict("incident invocation belongs to another profile")
            if reservation_ref is not None:
                reservation = cur.execute(
                    "SELECT connector_invocation_ref FROM connector_quota_reservations "
                    "WHERE reservation_id=?",
                    (reservation_ref,),
                ).fetchone()
                if reservation is None or (
                    connector_invocation_ref is not None
                    and reservation["connector_invocation_ref"] != connector_invocation_ref
                ):
                    raise ConnectorConflict("incident reservation does not match invocation")
            created_at = self._now()
            wire = self._record(
                {
                    "schema_version": SCHEMA_VERSION, "id": incident_id,
                    "created_at": created_at, **{k: v for k, v in request.items() if k != "incident_id"},
                }
            )
            cur.execute(
                "INSERT INTO connector_incidents"
                "(incident_id,connector_profile_ref,connector_invocation_ref,reservation_ref,"
                "incident_type,severity,record_json,content_hash,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    incident_id, connector_profile_ref, connector_invocation_ref, reservation_ref,
                    incident_type, severity, canonical_json(wire), wire["content_hash"], created_at,
                ),
            )
            event = self._append_incident_event(cur, incident_id, "opened", actor_ref, None)
            result = {"write_status": "fresh", **wire, "event": event}
            self._save_idem(cur, idempotency_key, "open_connector_incident", request_hash, result)
            return result

    def _append_incident_event(
        self,
        cur: sqlite3.Cursor,
        incident_ref: str,
        state: str,
        actor_ref: str,
        prior_event_ref: str | None,
    ) -> dict[str, Any]:
        created_at = self._now()
        wire = self._record(
            {
                "schema_version": SCHEMA_VERSION, "id": self._id("connector-incident-event"),
                "created_at": created_at, "incident_ref": incident_ref, "state": state,
                "prior_event_ref": prior_event_ref, "actor_ref": actor_ref,
            }
        )
        cur.execute(
            "INSERT INTO connector_incident_events"
            "(event_id,incident_ref,state,prior_event_ref,record_json,content_hash,created_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (
                wire["id"], incident_ref, state, prior_event_ref, canonical_json(wire),
                wire["content_hash"], created_at,
            ),
        )
        return wire

    def resolve_incident(
        self, incident_ref: str, *, actor_ref: str, idempotency_key: str
    ) -> dict[str, Any]:
        incident_ref = _text(incident_ref, "incident_ref")
        actor_ref = _text(actor_ref, "actor_ref")
        request_hash = content_hash({"incident_ref": incident_ref, "actor_ref": actor_ref})
        with self.store._transaction() as cur:
            duplicate = self._idem(cur, idempotency_key, "resolve_connector_incident", request_hash)
            if duplicate is not None:
                return duplicate
            latest = cur.execute(
                "SELECT event_id,state FROM connector_incident_events WHERE incident_ref=? "
                "ORDER BY rowid DESC LIMIT 1",
                (incident_ref,),
            ).fetchone()
            if latest is None:
                raise ConnectorNotFound(incident_ref)
            if latest["state"] != "opened":
                raise ConnectorConflict("incident is not open")
            event = self._append_incident_event(
                cur, incident_ref, "resolved", actor_ref, latest["event_id"]
            )
            result = {"write_status": "fresh", "incident_ref": incident_ref, "event": event}
            self._save_idem(cur, idempotency_key, "resolve_connector_incident", request_hash, result)
            return result

    def record_source_health(
        self,
        connector_profile_ref: str,
        state: str,
        *,
        actor_ref: str,
        connector_invocation_ref: str | None = None,
        event_id: str | None = None,
        idempotency_key: str,
    ) -> dict[str, Any]:
        connector_profile_ref = _text(connector_profile_ref, "connector_profile_ref")
        state = _text(state, "state")
        if state not in _HEALTH_STATES:
            raise ConnectorValidationError("source health state is invalid")
        actor_ref = _text(actor_ref, "actor_ref")
        connector_invocation_ref = None if connector_invocation_ref is None else _text(connector_invocation_ref, "connector_invocation_ref")
        event_id = self._idempotent_id(
            "connector-health-event", event_id, idempotency_key
        )
        request = {
            "connector_profile_ref": connector_profile_ref, "state": state,
            "actor_ref": actor_ref, "connector_invocation_ref": connector_invocation_ref,
            "event_id": event_id,
        }
        request_hash = content_hash(request)
        with self.store._transaction() as cur:
            duplicate = self._idem(cur, idempotency_key, "record_connector_health", request_hash)
            if duplicate is not None:
                return duplicate
            self._row_record(
                cur.execute(
                    "SELECT record_json FROM connector_profile_versions WHERE profile_version_id=?",
                    (connector_profile_ref,),
                ).fetchone(),
                connector_profile_ref,
            )
            if connector_invocation_ref is not None:
                invocation = cur.execute(
                    "SELECT connector_profile_ref FROM connector_invocations "
                    "WHERE connector_invocation_id=?",
                    (connector_invocation_ref,),
                ).fetchone()
                if invocation is None or invocation["connector_profile_ref"] != connector_profile_ref:
                    raise ConnectorConflict("health invocation belongs to another profile")
            prior = cur.execute(
                "SELECT event_id,state FROM connector_source_health_events WHERE connector_profile_ref=? "
                "ORDER BY rowid DESC LIMIT 1",
                (connector_profile_ref,),
            ).fetchone()
            transitions = {
                None: {"healthy", "degraded", "open_circuit"},
                "healthy": {"degraded", "open_circuit"},
                "degraded": {"healthy", "open_circuit"},
                "open_circuit": {"recovered"},
                "recovered": {"healthy", "degraded", "open_circuit"},
            }
            prior_state = None if prior is None else prior["state"]
            if state not in transitions[prior_state]:
                raise ConnectorConflict("invalid source health transition")
            created_at = self._now()
            wire = self._record(
                {
                    "schema_version": SCHEMA_VERSION, "id": event_id, "created_at": created_at,
                    "connector_profile_ref": connector_profile_ref,
                    "connector_invocation_ref": connector_invocation_ref, "state": state,
                    "prior_event_ref": None if prior is None else prior["event_id"],
                    "actor_ref": actor_ref,
                }
            )
            cur.execute(
                "INSERT INTO connector_source_health_events"
                "(event_id,connector_profile_ref,connector_invocation_ref,state,prior_event_ref,"
                "record_json,content_hash,created_at) VALUES(?,?,?,?,?,?,?,?)",
                (
                    event_id, connector_profile_ref, connector_invocation_ref, state,
                    wire["prior_event_ref"], canonical_json(wire), wire["content_hash"], created_at,
                ),
            )
            result = {"write_status": "fresh", **wire}
            self._save_idem(cur, idempotency_key, "record_connector_health", request_hash, result)
            return result

    def get_profile(self, profile_version_id: str) -> dict[str, Any]:
        return self._row_record(
            self.connection.execute(
                "SELECT record_json FROM connector_profile_versions WHERE profile_version_id=?",
                (_text(profile_version_id, "profile_version_id"),),
            ).fetchone(),
            profile_version_id,
        )

    def get_call_spec(self, call_spec_id: str) -> dict[str, Any]:
        return self._row_record(
            self.connection.execute(
                "SELECT record_json FROM connector_call_specs WHERE call_spec_id=?",
                (_text(call_spec_id, "call_spec_id"),),
            ).fetchone(),
            call_spec_id,
        )

    def get_invocation(self, connector_invocation_id: str) -> dict[str, Any]:
        return self._row_record(
            self.connection.execute(
                "SELECT record_json FROM connector_invocations WHERE connector_invocation_id=?",
                (_text(connector_invocation_id, "connector_invocation_id"),),
            ).fetchone(),
            connector_invocation_id,
        )


ConnectorAuthority = ConnectorStore


__all__ = [
    "ConnectorAuthority", "ConnectorBlocked", "ConnectorConflict", "ConnectorError",
    "ConnectorNotFound", "ConnectorQuotaExceeded", "ConnectorStore",
    "ConnectorValidationError", "source_envelope_content_hash",
    "validate_connector_proposal_manifest",
]
