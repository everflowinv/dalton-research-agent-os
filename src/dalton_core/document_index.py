"""Rebuildable SQLite FTS5 projection over immutable ArtifactVersion facts.

``DocumentIndex`` is deliberately a projection boundary.  It reads exact
ArtifactVersion rows from an exact ``ObservabilityStore`` and, when present,
exact Connector ``SourceEnvelope`` rows.  It never writes to either authority
and never treats caller-provided metadata or extracted text as a source of
truth.  Text is derived only by a built-in deterministic extractor after the
raw spool is checked against the artifact's hash and size.

The projection does not return document bytes.  Search results carry immutable
artifact/text refs and hashes for a later, separately authorized materializer.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from collections.abc import Iterable, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .connector import ConnectorStore, source_envelope_content_hash
from .observability import ObservabilityNotFound, ObservabilityStore
from .raw_spool import RawSpool
from .store import DaltonStore, canonical_json, content_hash


SCHEMA_VERSION = "0.1"
BUILDER_REF = "document-index-builder:sqlite-fts5:0.1"
_SCHEMA_PATH = Path(__file__).with_name("document_index_schema.sql")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_REF_RE = re.compile(r"^[^\x00\s]+$")
_INPUT_FIELDS = frozenset(
    {
        "schema_version",
        "id",
        "created_at",
        "artifact_version_ref",
        "artifact_version_hash",
        "extraction_mode",
        "content_hash",
    }
)
_SOURCE_FIELDS = frozenset(
    {
        "schema_version",
        "id",
        "created_at",
        "connector_invocation_ref",
        "connector_profile_ref",
        "physical_attempt_refs",
        "result_physical_attempt_ref",
        "source",
        "operation",
        "source_record_refs",
        "published_at",
        "updated_at",
        "as_of",
        "retrieved_at",
        "cursor",
        "provider_request_id",
        "raw_artifact_version_ref",
        "raw_response_hash",
        "source_schema_hash",
        "source_content_hash",
        "completeness",
        "status",
        "access_policy_ref",
        "retention_policy_ref",
        "terms_policy_ref",
        "error",
        "content_hash",
    }
)
_ACCESS_CLASSES = frozenset({"public", "internal", "restricted"})
_MAX_EXTRACTED_TEXT_BYTES = 16 * 1024 * 1024
_MAX_QUERY_BYTES = 4096


class DocumentIndexError(Exception):
    """Base error for the disposable document projection."""


class DocumentIndexValidationError(DocumentIndexError):
    pass


class DocumentIndexConflict(DocumentIndexError):
    pass


class DocumentIndexNotFound(DocumentIndexError):
    pass


def _nonempty(value: Any, name: str) -> str:
    if type(value) is not str or not value or not _REF_RE.fullmatch(value):
        raise DocumentIndexValidationError(f"{name} must be a non-empty ref-safe string")
    return value


def _sha256(value: Any, name: str) -> str:
    if type(value) is not str or _SHA256_RE.fullmatch(value) is None:
        raise DocumentIndexValidationError(f"{name} must be lowercase SHA-256 hex")
    return value


def _timestamp(value: Any, name: str) -> str:
    if type(value) is not str:
        raise DocumentIndexValidationError(f"{name} must be RFC3339")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise DocumentIndexValidationError(f"{name} must be RFC3339") from exc
    if parsed.tzinfo is None:
        raise DocumentIndexValidationError(f"{name} must include timezone")
    return parsed.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _optional_timestamp(value: Any, name: str) -> str | None:
    return None if value is None else _timestamp(value, name)


def _date(value: Any, name: str) -> str:
    if type(value) is not str or re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value) is None:
        raise DocumentIndexValidationError(f"{name} must be YYYY-MM-DD")
    try:
        parsed = datetime.strptime(value, "%Y-%m-%d")
    except ValueError as exc:
        raise DocumentIndexValidationError(f"{name} must be YYYY-MM-DD") from exc
    return parsed.date().isoformat()


def _closed(value: Any, fields: frozenset[str], name: str) -> dict[str, Any]:
    if type(value) is not dict:
        raise DocumentIndexValidationError(f"{name} must be an object")
    actual = set(value)
    if actual != set(fields):
        missing = sorted(set(fields) - actual)
        extra = sorted(actual - set(fields))
        raise DocumentIndexValidationError(
            f"{name} shape drift (missing={missing}, extra={extra})"
        )
    return dict(value)


def _hash_without_content_hash(value: Mapping[str, Any], name: str) -> str:
    payload = dict(value)
    supplied = payload.pop("content_hash", None)
    expected = content_hash(payload)
    if supplied != expected:
        raise DocumentIndexValidationError(f"{name} content_hash mismatch")
    return expected


def _text_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _raw_hash(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _json_list(value: Any, name: str, *, allow_empty: bool = True) -> list[str]:
    if type(value) is not list:
        raise DocumentIndexValidationError(f"{name} must be an array")
    result = list(value)
    if not allow_empty and not result:
        raise DocumentIndexValidationError(f"{name} must not be empty")
    if any(type(item) is not str or not item for item in result):
        raise DocumentIndexValidationError(f"{name} must contain non-empty strings")
    if len(result) != len(set(result)):
        raise DocumentIndexValidationError(f"{name} must not contain duplicates")
    return result


_COMPANY_PARSER_REF = "company-ref-parser:connector-parameters:0.1"


def _company_refs_from_call(
    profile: Mapping[str, Any], call_spec: Mapping[str, Any]
) -> list[str]:
    """Derive company refs only from frozen, source-specific CallSpec params.

    This intentionally handles the two authority-supported forms only.  A
    ticker/name is not a stable company identity, so unknown parameters yield
    no company facet instead of an invented mapping.
    """

    params = call_spec.get("parameters")
    if type(params) is not dict:
        return []
    source_type = profile.get("source_identity", {}).get("source_type")
    source_identity = profile.get("source_identity", {})
    source_ref = source_identity.get("source_ref")
    operation = call_spec.get("operation")
    if (
        source_type == "official_filing"
        and source_ref == "source:sec-edgar"
        and operation == "list_filings"
    ):
        issuer = params.get("issuer")
        if type(issuer) is str and re.fullmatch(r"[0-9]{1,10}", issuer):
            return [f"company:sec-cik:{issuer.zfill(10)}"]
    return []


def extract_builtin_document_text(raw: bytes, mode: str) -> str:
    """Run the shared deterministic UTF-8/JSON document extractor."""
    if mode == "utf8":
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise DocumentIndexValidationError("raw artifact is not valid UTF-8") from exc
    elif mode == "json":
        try:
            value = json.loads(raw.decode("utf-8"))
            text = canonical_json(value)
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
            raise DocumentIndexValidationError("raw artifact is not canonical JSON") from exc
    else:
        raise DocumentIndexValidationError("unsupported built-in extractor")
    if "\x00" in text:
        raise DocumentIndexValidationError("extracted text contains NUL")
    if len(text.encode("utf-8")) > _MAX_EXTRACTED_TEXT_BYTES:
        raise DocumentIndexValidationError("extracted text exceeds projection limit")
    return text


def _document_date(source: Mapping[str, Any] | None, artifact: Mapping[str, Any]) -> str:
    # This order is part of the projection contract.  It never uses the
    # retrieval time when a source-published/as-of date is available.
    candidates = (
        None if source is None else source.get("published_at"),
        None if source is None else source.get("as_of"),
        None if source is None else source.get("updated_at"),
        artifact.get("created_at"),
    )
    for candidate in candidates:
        if candidate is not None:
            return _timestamp(candidate, "document date")[:10]
    raise DocumentIndexValidationError("artifact has no usable document date")


def _source_metadata(
    source: Mapping[str, Any] | None, profile: Mapping[str, Any] | None = None
) -> str:
    if source is None:
        return ""
    # Keep only source metadata useful for recall.  Provider request IDs and
    # policy refs are authority fields but are not search text.
    projection = {
        "source": source["source"],
        "source_type": None if profile is None else profile["source_identity"]["source_type"],
        "operation": source["operation"],
        "source_record_refs": source["source_record_refs"],
        "published_at": source["published_at"],
        "updated_at": source["updated_at"],
        "as_of": source["as_of"],
        "completeness": source["completeness"],
        "status": source["status"],
    }
    return canonical_json(projection)


@dataclass(frozen=True, slots=True)
class DocumentIndexInput:
    """Closed extraction directive supplied at the projection boundary.

    The input contains no caller-provided body or metadata.  The builder reads
    the exact raw object and runs one of the built-in deterministic extractors.
    ``to_dict`` is the interchange shape used by the JSON contract.
    """

    artifact_version_ref: str
    artifact_version_hash: str
    extraction_mode: str
    created_at: str

    def to_dict(self) -> dict[str, Any]:
        artifact_ref = _nonempty(self.artifact_version_ref, "artifact_version_ref")
        artifact_hash = _sha256(self.artifact_version_hash, "artifact_version_hash")
        if self.extraction_mode not in {"utf8", "json"}:
            raise DocumentIndexValidationError("extraction_mode must be utf8 or json")
        created_at = _timestamp(self.created_at, "created_at")
        identifier = f"document-input:{artifact_ref}:{artifact_hash}:{self.extraction_mode}"
        base = {
            "schema_version": SCHEMA_VERSION,
            "id": identifier,
            "created_at": created_at,
            "artifact_version_ref": artifact_ref,
            "artifact_version_hash": artifact_hash,
            "extraction_mode": self.extraction_mode,
        }
        return {**base, "content_hash": content_hash(base)}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "DocumentIndexInput":
        wire = _closed(value, _INPUT_FIELDS, "DocumentIndexInput")
        if wire["schema_version"] != SCHEMA_VERSION:
            raise DocumentIndexValidationError("unsupported DocumentIndexInput schema_version")
        candidate = cls(
            artifact_version_ref=wire["artifact_version_ref"],
            artifact_version_hash=wire["artifact_version_hash"],
            extraction_mode=wire["extraction_mode"],
            created_at=wire["created_at"],
        )
        normalized = candidate.to_dict()
        if wire != normalized:
            raise DocumentIndexValidationError("DocumentIndexInput is not canonical")
        _hash_without_content_hash(wire, "DocumentIndexInput")
        return candidate


def make_document_index_input(
    artifact_version_ref: str,
    artifact_version_hash: str,
    extraction_mode: str,
    *,
    created_at: str,
) -> dict[str, Any]:
    """Build the canonical projection-input wire without changing authority."""

    return DocumentIndexInput(
        artifact_version_ref=artifact_version_ref,
        artifact_version_hash=artifact_version_hash,
        extraction_mode=extraction_mode,
        created_at=created_at,
    ).to_dict()


def _validate_source_envelope(value: Mapping[str, Any]) -> dict[str, Any]:
    wire = _closed(value, _SOURCE_FIELDS, "SourceEnvelope")
    if wire["schema_version"] != "0.1":
        raise DocumentIndexValidationError("unsupported SourceEnvelope schema_version")
    _nonempty(wire["id"], "source envelope id")
    _timestamp(wire["created_at"], "source envelope created_at")
    for name in (
        "connector_invocation_ref",
        "connector_profile_ref",
        "result_physical_attempt_ref",
        "source",
        "operation",
        "raw_artifact_version_ref",
        "access_policy_ref",
        "retention_policy_ref",
        "terms_policy_ref",
    ):
        _nonempty(wire[name], f"source envelope {name}")
    _json_list(wire["physical_attempt_refs"], "source envelope physical_attempt_refs", allow_empty=False)
    refs = _json_list(wire["source_record_refs"], "source envelope source_record_refs")
    if wire["result_physical_attempt_ref"] not in wire["physical_attempt_refs"]:
        raise DocumentIndexValidationError("source envelope result attempt is not in attempt refs")
    for name in ("published_at", "updated_at", "as_of"):
        wire[name] = _optional_timestamp(wire[name], f"source envelope {name}")
    wire["retrieved_at"] = _timestamp(wire["retrieved_at"], "source envelope retrieved_at")
    for name in ("raw_response_hash", "source_schema_hash", "source_content_hash", "content_hash"):
        _sha256(wire[name], f"source envelope {name}")
    if wire["completeness"] not in {"enumerated", "ranked", "partial", "unknown"}:
        raise DocumentIndexValidationError("source envelope completeness is invalid")
    if wire["status"] not in {"complete", "partial", "empty", "error"}:
        raise DocumentIndexValidationError("source envelope status is invalid")
    if wire["error"] is not None and type(wire["error"]) is not dict:
        raise DocumentIndexValidationError("source envelope error must be an object or null")
    if wire["source_content_hash"] != source_envelope_content_hash(wire):
        raise DocumentIndexValidationError("source envelope source_content_hash mismatch")
    _hash_without_content_hash(wire, "SourceEnvelope")
    # Return a normalized copy; source_record_refs order is authority order and
    # is retained in the FTS metadata hash, while company facets are sorted.
    wire["source_record_refs"] = refs
    return wire


class DocumentIndex:
    """A disposable FTS5 projection over a trusted artifact read boundary."""

    def __init__(
        self,
        path: str | Path = ":memory:",
        *,
        observability: Any,
        raw_spool: Any,
        connector_store: Any | None = None,
        visible_access_classes: Sequence[str] = ("public",),
    ) -> None:
        if type(observability) is not ObservabilityStore:
            raise TypeError("DocumentIndex requires the exact ObservabilityStore artifact authority")
        store = observability.store
        if type(store) is not DaltonStore:
            raise TypeError("DocumentIndex requires ObservabilityStore on the exact DaltonStore")
        if type(raw_spool) is not RawSpool:
            raise TypeError("DocumentIndex requires the exact read-only RawSpool")
        if connector_store is not None and (
            type(connector_store) is not ConnectorStore
            or connector_store.store is not store
        ):
            raise TypeError("connector_store must be the ConnectorStore sharing this DaltonStore")
        classes = tuple(visible_access_classes)
        if not classes or any(item not in _ACCESS_CLASSES for item in classes):
            raise DocumentIndexValidationError("visible_access_classes is invalid")
        if len(classes) != len(set(classes)):
            raise DocumentIndexValidationError("visible_access_classes contains duplicates")
        self.path = str(path)
        self.store = store
        self.observability = observability
        self.raw_spool = raw_spool
        self.connector_store = connector_store
        self.visible_access_classes = classes
        if self.path != ":memory:" and self.path.startswith("file:"):
            raise DocumentIndexValidationError("DocumentIndex path must be a local filesystem path")
        self.connection = sqlite3.connect(self.path, isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA busy_timeout = 5000")
        self.connection.executescript(_SCHEMA_PATH.read_text(encoding="utf-8"))
        if self.path != ":memory:":
            try:
                os.chmod(self.path, 0o600)
            except OSError as exc:
                self.connection.close()
                raise DocumentIndexError("DocumentIndex file must be owner-readable only") from exc

    @property
    def conn(self) -> sqlite3.Connection:
        return self.connection

    def close(self) -> None:
        self.connection.close()

    @contextmanager
    def _projection_transaction(self):
        """Make replacement/clear atomic even though the connection is autocommit."""

        self.connection.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self.connection.rollback()
            raise
        else:
            self.connection.commit()

    @staticmethod
    def _read_authority_record(
        connection: sqlite3.Connection,
        table: str,
        id_column: str,
        identifier: str,
        name: str,
        column_bindings: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        row = connection.execute(
            f"SELECT * FROM {table} WHERE {id_column}=?",
            (identifier,),
        ).fetchone()
        if row is None:
            raise DocumentIndexConflict(f"{name} authority row is missing")
        try:
            wire = json.loads(row["record_json"])
        except (TypeError, ValueError) as exc:
            raise DocumentIndexValidationError(f"{name} record_json is invalid") from exc
        if type(wire) is not dict or wire.get("id") != identifier:
            raise DocumentIndexConflict(f"{name} identity binding failed")
        if wire.get("content_hash") != row["content_hash"]:
            raise DocumentIndexConflict(f"{name} stored hash binding failed")
        _hash_without_content_hash(wire, name)
        for column, field in (column_bindings or {}).items():
            if row[column] != wire.get(field):
                raise DocumentIndexConflict(f"{name} column {column} binding failed")
        return dict(wire)

    @staticmethod
    def _read_execution_record(
        connection: sqlite3.Connection, identifier: str
    ) -> tuple[dict[str, Any], str]:
        row = connection.execute(
            "SELECT * FROM execution_invocations WHERE execution_id=?",
            (identifier,),
        ).fetchone()
        if row is None:
            raise DocumentIndexConflict("producer ExecutionInvocation authority row is missing")
        try:
            wire = json.loads(row["execution_json"])
        except (TypeError, ValueError) as exc:
            raise DocumentIndexValidationError("ExecutionInvocation record_json is invalid") from exc
        if type(wire) is not dict or wire.get("id") != identifier:
            raise DocumentIndexConflict("ExecutionInvocation identity binding failed")
        expected = content_hash(wire)
        if row["content_hash"] != expected or row["execution_id"] != wire["id"]:
            raise DocumentIndexConflict("ExecutionInvocation stored hash binding failed")
        for column, field in (
            ("kind", "kind"),
            ("work_order_ref", "work_order_ref"),
            ("profile_ref", "profile_ref"),
            ("capability", "capability"),
            ("runtime_ref", "runtime_ref"),
            ("actor_ref", "actor_ref"),
            ("parent_ref", "parent_ref"),
            ("environment_hash", "environment_hash"),
        ):
            if row[column] != wire.get(field):
                raise DocumentIndexConflict(f"ExecutionInvocation column {column} binding failed")
        return dict(wire), row["content_hash"]

    def _read_artifact_authority(self, identifier: str) -> dict[str, Any]:
        """Read an ArtifactVersion through both its wire row and cross-version index."""

        try:
            api_wire = self.observability.get_artifact_version(identifier)
        except ObservabilityNotFound as exc:
            raise DocumentIndexNotFound("ArtifactVersion authority row not found") from exc
        except (TypeError, ValueError, KeyError) as exc:
            raise DocumentIndexConflict("ArtifactVersion authority record is malformed") from exc
        connection = self.observability.connection
        index = connection.execute(
            "SELECT * FROM observability_artifact_version_index WHERE version_id=?",
            (identifier,),
        ).fetchone()
        if index is None:
            raise DocumentIndexConflict("ArtifactVersion cross-version index row is missing")
        schema_version = index["schema_version"]
        if schema_version not in {"0.1", "0.2"}:
            raise DocumentIndexConflict("ArtifactVersion schema index is invalid")
        table = (
            "observability_artifact_versions"
            if schema_version == "0.1"
            else "observability_artifact_versions_v2"
        )
        row = connection.execute(
            f"SELECT * FROM {table} WHERE version_id=?", (identifier,)
        ).fetchone()
        if row is None:
            raise DocumentIndexConflict("ArtifactVersion authority row is missing")
        try:
            wire = json.loads(row["record_json"])
        except (TypeError, ValueError) as exc:
            raise DocumentIndexConflict("ArtifactVersion record_json is invalid") from exc
        if type(wire) is not dict or wire.get("id") != identifier:
            raise DocumentIndexConflict("ArtifactVersion identity binding failed")
        if canonical_json(wire) != canonical_json(api_wire):
            raise DocumentIndexConflict("ArtifactVersion API and SQL records disagree")
        if row["content_hash"] != wire.get("content_hash"):
            raise DocumentIndexConflict("ArtifactVersion stored hash binding failed")
        _hash_without_content_hash(wire, "ArtifactVersion")
        producer_field = (
            "producer_invocation_ref" if schema_version == "0.1" else "producer_execution_ref"
        )
        for column, field in (
            ("artifact_ref", "artifact_ref"),
            ("version_number", "version"),
            ("prior_version_id" if schema_version == "0.1" else "prior_version_ref", "prior_version_ref"),
            ("artifact_content_hash", "artifact_content_hash"),
            ("work_order_ref", "work_order_ref"),
            ("result_envelope_ref", "result_envelope_ref"),
            ("result_envelope_hash", "result_envelope_hash"),
            ("storage_locator", "storage_locator"),
            ("created_at", "created_at"),
            (producer_field, producer_field),
        ):
            if row[column] != wire.get(field):
                raise DocumentIndexConflict(f"ArtifactVersion column {column} binding failed")
        index_producer = wire[producer_field]
        for column, expected in (
            ("artifact_ref", wire["artifact_ref"]),
            ("version_number", wire["version"]),
            ("schema_version", schema_version),
            ("prior_version_ref", wire["prior_version_ref"]),
            ("producer_execution_ref", index_producer),
            ("record_hash", wire["content_hash"]),
            ("created_at", wire["created_at"]),
        ):
            if index[column] != expected:
                raise DocumentIndexConflict(f"ArtifactVersion index column {column} binding failed")
        return dict(wire)

    def _read_source_for_artifact(
        self, artifact: Mapping[str, Any]
    ) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]] | None:
        if self.connector_store is None:
            table = self.store.connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='connector_source_envelopes'"
            ).fetchone()
            if table is not None:
                raise DocumentIndexConflict("ConnectorStore authority reader is required for SourceEnvelope joins")
            return None
        connection = self.connector_store.connection
        table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='connector_source_envelopes'"
        ).fetchone()
        if table is None:
            return None
        rows = connection.execute(
            "SELECT * "
            "FROM connector_source_envelopes WHERE raw_artifact_version_ref=? "
            "ORDER BY source_envelope_id",
            (artifact["id"],),
        ).fetchall()
        if not rows:
            return None
        if len(rows) != 1:
            raise DocumentIndexConflict(
                "one ArtifactVersion must bind exactly one SourceEnvelope for DocumentIndex"
            )
        row = rows[0]
        try:
            wire = json.loads(row["record_json"])
        except (TypeError, ValueError) as exc:
            raise DocumentIndexValidationError("SourceEnvelope record_json is invalid") from exc
        source = _validate_source_envelope(wire)
        if source["id"] != row["source_envelope_id"]:
            raise DocumentIndexConflict("SourceEnvelope identity drifted")
        for column, field in (
            ("connector_invocation_ref", "connector_invocation_ref"),
            ("connector_profile_ref", "connector_profile_ref"),
            ("raw_artifact_version_ref", "raw_artifact_version_ref"),
            ("raw_response_hash", "raw_response_hash"),
            ("completeness", "completeness"),
            ("status", "status"),
        ):
            if row[column] != source[field]:
                raise DocumentIndexConflict(f"SourceEnvelope column {column} binding failed")
        invocation = self._read_authority_record(
            connection,
            "connector_invocations",
            "connector_invocation_id",
            source["connector_invocation_ref"],
            "ConnectorInvocation",
            {
                "work_order_ref": "work_order_ref",
                "work_order_hash": "work_order_hash",
                "connector_profile_ref": "connector_profile_ref",
                "connector_profile_hash": "connector_profile_hash",
                "call_spec_ref": "call_spec_ref",
                "call_spec_hash": "call_spec_hash",
                "capability_lease_ref": "capability_lease_ref",
                "capability_lease_hash": "capability_lease_hash",
                "descriptor_revision_ref": "descriptor_revision_ref",
                "catalog_epoch": "catalog_epoch",
                "logical_invocation_key": "logical_invocation_key",
                "created_at": "created_at",
            },
        )
        invocation_row = connection.execute(
            "SELECT execution_ref,execution_hash FROM connector_invocations WHERE connector_invocation_id=?",
            (source["connector_invocation_ref"],),
        ).fetchone()
        if invocation_row is None:
            raise DocumentIndexConflict("ConnectorInvocation table row is missing")
        execution_ref = invocation_row["execution_ref"]
        execution_hash_from_link = invocation_row["execution_hash"]
        profile = self._read_authority_record(
            connection,
            "connector_profile_versions",
            "profile_version_id",
            source["connector_profile_ref"],
            "ConnectorProfileVersion",
            {
                "connector_ref": "connector_ref",
                "version_number": "version",
                "prior_version_ref": "prior_version_ref",
                "capability_id": "capability_id",
                "descriptor_revision_ref": "descriptor_revision_ref",
                "descriptor_hash": "descriptor_hash",
                "source_hash": "source_hash",
                "schema_hash": "schema_hash",
                "catalog_epoch": "catalog_epoch",
                "adapter_ref": "adapter_ref",
                "adapter_hash": "adapter_hash",
                "created_at": "created_at",
            },
        )
        source_identity = profile.get("source_identity")
        if type(source_identity) is not dict:
            raise DocumentIndexValidationError("ConnectorProfile source_identity is invalid")
        for field in ("source_ref", "source_type", "source_version"):
            _nonempty(source_identity.get(field), f"ConnectorProfile source_identity.{field}")
        call_spec_ref = invocation.get("call_spec_ref")
        if type(call_spec_ref) is not str:
            raise DocumentIndexConflict("ConnectorInvocation has no exact CallSpec ref")
        call_spec = self._read_authority_record(
            connection,
            "connector_call_specs",
            "call_spec_id",
            call_spec_ref,
            "ConnectorCallSpec",
            {
                "work_order_ref": "work_order_ref",
                "work_order_hash": "work_order_hash",
                "connector_profile_ref": "connector_profile_ref",
                "operation": "operation",
                "query_hash": "query_hash",
                "created_at": "created_at",
            },
        )
        if type(execution_ref) is not str:
            raise DocumentIndexConflict("ConnectorInvocation has no exact execution ref")
        execution, execution_hash = self._read_execution_record(connection, execution_ref)
        if (
            source["content_hash"] != row["content_hash"]
            or source["raw_artifact_version_ref"] != artifact["id"]
            or source["raw_response_hash"] != artifact["artifact_content_hash"]
            or row["raw_response_hash"] != artifact["artifact_content_hash"]
            or invocation.get("connector_profile_ref") != source["connector_profile_ref"]
            or invocation.get("connector_profile_hash") != profile["content_hash"]
            or execution_hash_from_link != execution_hash
            or artifact.get("producer_execution_ref") != execution_ref
            or invocation.get("call_spec_ref") != call_spec["id"]
            or invocation.get("call_spec_hash") != call_spec["content_hash"]
            or call_spec.get("connector_profile_ref") != profile["id"]
            or call_spec.get("operation") != source["operation"]
            or profile.get("source_identity", {}).get("source_ref") != source["source"]
        ):
            raise DocumentIndexConflict("SourceEnvelope is not bound to exact connector authority chain")
        if execution.get("kind") != "connector":
            raise DocumentIndexConflict("SourceEnvelope must bind a connector execution")
        source_hash = profile.get("source_hash")
        source_identity = profile.get("source_identity")
        if type(source_identity) is not dict or content_hash(source_identity) != source_hash:
            raise DocumentIndexConflict("ConnectorProfile source identity hash drifted")
        return source, invocation, profile, call_spec

    def _normalize_input(self, value: DocumentIndexInput | Mapping[str, Any]) -> dict[str, Any]:
        if type(value) is DocumentIndexInput:
            wire = value.to_dict()
        elif type(value) is dict:
            wire = DocumentIndexInput.from_dict(value).to_dict()
        else:
            raise DocumentIndexValidationError("DocumentIndex input must be a closed mapping")
        return wire

    def _project_one(self, value: DocumentIndexInput | Mapping[str, Any]) -> dict[str, Any]:
        item = self._normalize_input(value)
        artifact = self._read_artifact_authority(item["artifact_version_ref"])
        if artifact.get("id") != item["artifact_version_ref"]:
            raise DocumentIndexConflict("ArtifactVersion id does not match requested ref")
        if artifact.get("content_hash") != item["artifact_version_hash"]:
            raise DocumentIndexConflict("ArtifactVersion content hash binding failed")
        if artifact.get("schema_version") not in {"0.1", "0.2"}:
            raise DocumentIndexValidationError("unsupported ArtifactVersion schema_version")
        artifact_base = dict(artifact)
        artifact_hash = artifact_base.pop("content_hash", None)
        if artifact_hash != content_hash(artifact_base):
            raise DocumentIndexConflict("ArtifactVersion authority hash drifted")
        for name in (
            "artifact_ref",
            "title",
            "kind",
            "media_type",
            "artifact_content_hash",
            "storage_locator",
            "access_class",
            "preview_status",
        ):
            if type(artifact.get(name)) is not str or artifact.get(name) == "":
                raise DocumentIndexValidationError(f"ArtifactVersion {name} is invalid")
        _sha256(artifact["artifact_content_hash"], "artifact_content_hash")
        if artifact["access_class"] not in _ACCESS_CLASSES:
            raise DocumentIndexValidationError("ArtifactVersion access_class is invalid")
        if type(artifact.get("size_bytes")) is not int:
            raise DocumentIndexValidationError("ArtifactVersion size_bytes is invalid")

        # Re-read raw bytes by content address and verify both fields from the
        # authority.  The projection never stores those raw bytes.
        try:
            raw = self.raw_spool.read_object(artifact["artifact_content_hash"])
        except Exception as exc:
            raise DocumentIndexConflict("raw artifact object is unavailable") from exc
        if type(raw) not in (bytes, bytearray, memoryview):
            raise DocumentIndexValidationError("raw spool returned non-bytes")
        raw_bytes = bytes(raw)
        if len(raw_bytes) != artifact["size_bytes"]:
            raise DocumentIndexConflict("raw artifact size does not match ArtifactVersion")
        if _raw_hash(raw_bytes) != artifact["artifact_content_hash"]:
            raise DocumentIndexConflict("raw artifact hash does not match ArtifactVersion")

        extracted_text = extract_builtin_document_text(
            raw_bytes, item["extraction_mode"]
        )
        text_hash = _text_hash(extracted_text)
        extractor_ref = f"document-extractor:builtin-{item['extraction_mode']}:0.1"
        text_ref = f"document-text:{artifact['id']}:{artifact['content_hash']}:{extractor_ref}:{text_hash}"
        source_join = self._read_source_for_artifact(artifact)
        source = None if source_join is None else source_join[0]
        invocation = None if source_join is None else source_join[1]
        profile = None if source_join is None else source_join[2]
        call_spec = None if source_join is None else source_join[3]
        source_refs = [] if source is None else list(source["source_record_refs"])
        company_refs = [] if profile is None or call_spec is None else _company_refs_from_call(profile, call_spec)
        source_type = None if profile is None else profile["source_identity"]["source_type"]
        source_operation = None if source is None else source["operation"]
        document_date = _document_date(source, artifact)
        source_metadata = _source_metadata(source, profile)
        document = {
            "schema_version": SCHEMA_VERSION,
            "artifact_version_ref": artifact["id"],
            "artifact_version_hash": artifact["content_hash"],
            "artifact_ref": artifact["artifact_ref"],
            "artifact_version": artifact["version"],
            "artifact_content_hash": artifact["artifact_content_hash"],
            "title": artifact["title"],
            "kind": artifact["kind"],
            "media_type": artifact["media_type"],
            "access_class": artifact["access_class"],
            "source_envelope_ref": None if source is None else source["id"],
            "source_envelope_hash": None if source is None else source["content_hash"],
            "source_type": source_type,
            "source_operation": source_operation,
            "source_record_refs": source_refs,
            "company_refs": company_refs,
            "company_parser_ref": _COMPANY_PARSER_REF if company_refs else None,
            "published_at": None if source is None else source["published_at"],
            "updated_at": None if source is None else source["updated_at"],
            "as_of": None if source is None else source["as_of"],
            "retrieved_at": None if source is None else source["retrieved_at"],
            "document_date": document_date,
            "source_metadata": source_metadata,
            "extracted_text_ref": text_ref,
            "extracted_text_hash": text_hash,
            "extracted_text_size_bytes": len(extracted_text.encode("utf-8")),
            "input_ref": item["id"],
            "input_hash": item["content_hash"],
            # extracted_text is sent to FTS only; it is intentionally excluded
            # from the persisted record and snapshot contract.
            "_extracted_text": extracted_text,
        }
        return document

    @staticmethod
    def _authority_snapshot(documents: Sequence[Mapping[str, Any]]) -> tuple[str, str]:
        body = {
            "schema_version": SCHEMA_VERSION,
            "artifacts": [
                {
                    "artifact_version_ref": item["artifact_version_ref"],
                    "artifact_version_hash": item["artifact_version_hash"],
                    "source_envelope_ref": item["source_envelope_ref"],
                    "source_envelope_hash": item["source_envelope_hash"],
                }
                for item in documents
            ],
        }
        digest = content_hash(body)
        return f"document-authority-snapshot:{digest}", digest

    @staticmethod
    def _snapshot(documents: Sequence[Mapping[str, Any]], *, created_at: str) -> dict[str, Any]:
        created = _timestamp(created_at, "snapshot created_at")
        authority_ref, authority_hash = DocumentIndex._authority_snapshot(documents)
        clean_documents = []
        for item in documents:
            clean = {key: value for key, value in item.items() if not key.startswith("_")}
            clean_documents.append(clean)
        body = {
            "schema_version": SCHEMA_VERSION,
            "id": "pending",
            "created_at": created,
            "authority_snapshot_ref": authority_ref,
            "authority_snapshot_hash": authority_hash,
            "builder_ref": BUILDER_REF,
            "documents": clean_documents,
        }
        digest = content_hash(body | {"id": "pending"})
        body["id"] = f"document-index-snapshot:{digest}"
        # The id is part of the immutable object, so the final content hash is
        # computed after the deterministic id is known.
        digest = content_hash(body)
        body["content_hash"] = digest
        return body

    @staticmethod
    def _validate_document_wire(value: Mapping[str, Any]) -> dict[str, Any]:
        # Snapshot document records are generated by _project_one.  Keep this
        # check narrow and closed so DB tampering cannot become a search result.
        expected = {
            "schema_version", "artifact_version_ref", "artifact_version_hash", "artifact_ref",
            "artifact_version", "artifact_content_hash", "title", "kind", "media_type",
            "access_class", "source_envelope_ref", "source_envelope_hash", "source_type",
            "source_operation", "source_record_refs", "company_refs", "published_at",
            "company_parser_ref", "updated_at", "as_of", "retrieved_at", "document_date", "source_metadata",
            "extracted_text_ref", "extracted_text_hash", "extracted_text_size_bytes",
            "input_ref", "input_hash",
        }
        wire = _closed(value, frozenset(expected), "DocumentIndexDocument")
        if wire["schema_version"] != SCHEMA_VERSION:
            raise DocumentIndexValidationError("unsupported DocumentIndexDocument schema_version")
        _nonempty(wire["artifact_version_ref"], "artifact_version_ref")
        _sha256(wire["artifact_version_hash"], "artifact_version_hash")
        _nonempty(wire["artifact_ref"], "artifact_ref")
        if type(wire["artifact_version"]) is not int or wire["artifact_version"] < 1:
            raise DocumentIndexValidationError("artifact_version is invalid")
        _sha256(wire["artifact_content_hash"], "artifact_content_hash")
        for name in ("title", "kind", "media_type", "access_class", "source_metadata", "extracted_text_ref", "input_ref"):
            if type(wire[name]) is not str or (name != "source_metadata" and not wire[name]):
                raise DocumentIndexValidationError(f"{name} is invalid")
        if wire["access_class"] not in _ACCESS_CLASSES:
            raise DocumentIndexValidationError("access_class is invalid")
        for name in ("source_record_refs", "company_refs"):
            _json_list(wire[name], name)
        if wire["company_refs"] != sorted(set(wire["company_refs"])):
            raise DocumentIndexValidationError("company_refs are not canonical")
        if wire["company_refs"] and wire["company_parser_ref"] != _COMPANY_PARSER_REF:
            raise DocumentIndexValidationError("company facet parser binding is missing")
        if not wire["company_refs"] and wire["company_parser_ref"] is not None:
            raise DocumentIndexValidationError("empty company facet cannot claim a parser")
        for name in ("published_at", "updated_at", "as_of", "retrieved_at"):
            if wire[name] is not None:
                _timestamp(wire[name], name)
        _date(wire["document_date"], "document_date")
        _sha256(wire["extracted_text_hash"], "extracted_text_hash")
        if type(wire["extracted_text_size_bytes"]) is not int or wire["extracted_text_size_bytes"] < 0:
            raise DocumentIndexValidationError("extracted_text_size_bytes is invalid")
        _sha256(wire["input_hash"], "input_hash")
        if wire["source_envelope_ref"] is None:
            if wire["source_envelope_hash"] is not None or wire["source_type"] is not None:
                raise DocumentIndexValidationError("source metadata is partially bound")
        else:
            _nonempty(wire["source_envelope_ref"], "source_envelope_ref")
            _sha256(wire["source_envelope_hash"], "source_envelope_hash")
            _nonempty(wire["source_type"], "source_type")
            _nonempty(wire["source_operation"], "source_operation")
        return wire

    def rebuild(
        self,
        inputs: Iterable[DocumentIndexInput | Mapping[str, Any]],
        *,
        created_at: str,
    ) -> dict[str, Any]:
        """Validate authority/text inputs, then atomically replace the projection."""

        projected: list[dict[str, Any]] = []
        seen: set[str] = set()
        for value in inputs:
            document = self._project_one(value)
            ref = document["artifact_version_ref"]
            if ref in seen:
                raise DocumentIndexConflict("duplicate ArtifactVersion input")
            seen.add(ref)
            projected.append(document)
        projected.sort(key=lambda item: (item["artifact_ref"], item["artifact_version"], item["artifact_version_ref"]))
        snapshot = self._snapshot(projected, created_at=created_at)
        for document in projected:
            self._validate_document_wire({key: value for key, value in document.items() if not key.startswith("_")})
        with self._projection_transaction():
            self.connection.execute("INSERT INTO document_index_fts(document_index_fts) VALUES('delete-all')")
            self.connection.execute("DELETE FROM document_index_companies")
            self.connection.execute("DELETE FROM document_index_documents")
            self.connection.execute("DELETE FROM document_index_snapshot")
            for rowid, document in enumerate(projected, start=1):
                clean = {key: value for key, value in document.items() if not key.startswith("_")}
                clean_json = canonical_json(clean)
                row_hash = content_hash(clean)
                self.connection.execute(
                    "INSERT INTO document_index_documents"
                    "(rowid,artifact_version_ref,artifact_version_hash,artifact_ref,artifact_version,"
                    "artifact_content_hash,title,kind,media_type,access_class,source_envelope_ref,source_envelope_hash,"
                    "source_type,source_operation,source_record_refs_json,company_refs_json,company_parser_ref,published_at,updated_at,"
                    "as_of,retrieved_at,document_date,source_metadata,extracted_text,extracted_text_ref,extracted_text_hash,"
                    "extracted_text_size_bytes,input_ref,input_hash,record_json,content_hash) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        rowid,
                        clean["artifact_version_ref"], clean["artifact_version_hash"], clean["artifact_ref"],
                        clean["artifact_version"], clean["artifact_content_hash"], clean["title"], clean["kind"],
                        clean["media_type"], clean["access_class"], clean["source_envelope_ref"], clean["source_envelope_hash"],
                        clean["source_type"], clean["source_operation"], canonical_json(clean["source_record_refs"]),
                        canonical_json(clean["company_refs"]), clean["company_parser_ref"], clean["published_at"], clean["updated_at"], clean["as_of"],
                        clean["retrieved_at"], clean["document_date"], clean["source_metadata"], document["_extracted_text"], clean["extracted_text_ref"],
                        clean["extracted_text_hash"], clean["extracted_text_size_bytes"], clean["input_ref"], clean["input_hash"],
                        clean_json, row_hash,
                    ),
                )
                self.connection.execute(
                    "INSERT INTO document_index_fts(rowid,title,source_metadata,extracted_text) VALUES(?,?,?,?)",
                    (rowid, clean["title"], clean["source_metadata"], document["_extracted_text"]),
                )
                for company_ref in clean["company_refs"]:
                    self.connection.execute(
                        "INSERT INTO document_index_companies(document_rowid,company_ref) VALUES(?,?)",
                        (rowid, company_ref),
                    )
            # Validate the newly assembled external-content index before the
            # snapshot becomes visible.  Any mismatch rolls the complete
            # projection replacement back in this transaction.
            self._assert_projection_integrity()
            snapshot_json = canonical_json(snapshot)
            self.connection.execute(
                "INSERT INTO document_index_snapshot(singleton,snapshot_id,record_json,content_hash,created_at) "
                "VALUES(1,?,?,?,?)",
                (snapshot["id"], snapshot_json, snapshot["content_hash"], snapshot["created_at"]),
            )
        return snapshot

    def clear(self) -> None:
        """Delete only projection rows; authority tables and raw spool remain untouched."""

        with self._projection_transaction():
            self.connection.execute("INSERT INTO document_index_fts(document_index_fts) VALUES('delete-all')")
            self.connection.execute("DELETE FROM document_index_companies")
            self.connection.execute("DELETE FROM document_index_documents")
            self.connection.execute("DELETE FROM document_index_snapshot")

    def snapshot(self) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT record_json,content_hash FROM document_index_snapshot WHERE singleton=1"
        ).fetchone()
        if row is None:
            return None
        try:
            wire = json.loads(row["record_json"])
        except (TypeError, ValueError) as exc:
            raise DocumentIndexValidationError("stored DocumentIndex snapshot is invalid") from exc
        if wire.get("content_hash") != row["content_hash"] or wire.get("content_hash") != content_hash({key: value for key, value in wire.items() if key != "content_hash"}):
            raise DocumentIndexConflict("stored DocumentIndex snapshot hash drifted")
        return wire

    @staticmethod
    def _safe_match_query(value: Any) -> str:
        if type(value) is not str or not value.strip():
            raise DocumentIndexValidationError("query must be a non-empty string")
        if len(value.encode("utf-8")) > _MAX_QUERY_BYTES or "\x00" in value:
            raise DocumentIndexValidationError("query exceeds FTS boundary")
        if any(ord(char) < 32 and char not in "\t\n\r" for char in value):
            raise DocumentIndexValidationError("query contains control characters")
        # FTS5 MATCH receives one quoted phrase per whitespace-delimited term.
        # This deliberately disables caller-supplied MATCH operators/columns.
        parts = value.split()
        return " AND ".join('"' + part.replace('"', '""') + '"' for part in parts)

    @staticmethod
    def _filter_text(value: Any, name: str) -> str:
        return _nonempty(value, name)

    def _assert_projection_integrity(self) -> None:
        """Refuse search when FTS/main-table/facet rows disagree."""

        try:
            # External-content FTS tables mirror the content table when joined;
            # COUNT/JOIN therefore cannot detect a stale inverted index.  The
            # rank=1 integrity command compares the actual FTS index checksum.
            self.connection.execute(
                "INSERT INTO document_index_fts(document_index_fts,rank) VALUES('integrity-check',1)"
            )
        except sqlite3.DatabaseError as exc:
            raise DocumentIndexConflict("FTS/main-table index checksum drifted") from exc
        rows = self.connection.execute(
            "SELECT rowid,record_json,content_hash,artifact_version_ref,artifact_version_hash,"
            "artifact_ref,artifact_version,artifact_content_hash,title,kind,media_type,access_class,"
            "source_envelope_ref,source_envelope_hash,source_type,source_operation,"
            "source_record_refs_json,company_refs_json,company_parser_ref,published_at,updated_at,"
            "as_of,retrieved_at,document_date,source_metadata,extracted_text,extracted_text_ref,"
            "extracted_text_hash,extracted_text_size_bytes,input_ref,input_hash "
            "FROM document_index_documents ORDER BY rowid"
        ).fetchall()
        for row in rows:
            try:
                value = json.loads(row["record_json"])
                self._validate_document_wire(value)
            except (TypeError, ValueError) as exc:
                raise DocumentIndexConflict("stored DocumentIndex document is invalid") from exc
            if row["content_hash"] != content_hash(value):
                raise DocumentIndexConflict("stored document row hash drifted")
            for column, field in (
                ("artifact_version_ref", "artifact_version_ref"),
                ("artifact_version_hash", "artifact_version_hash"),
                ("artifact_ref", "artifact_ref"),
                ("artifact_version", "artifact_version"),
                ("artifact_content_hash", "artifact_content_hash"),
                ("title", "title"),
                ("kind", "kind"),
                ("media_type", "media_type"),
                ("access_class", "access_class"),
                ("source_envelope_ref", "source_envelope_ref"),
                ("source_envelope_hash", "source_envelope_hash"),
                ("source_type", "source_type"),
                ("source_operation", "source_operation"),
                ("company_parser_ref", "company_parser_ref"),
                ("published_at", "published_at"),
                ("updated_at", "updated_at"),
                ("as_of", "as_of"),
                ("retrieved_at", "retrieved_at"),
                ("document_date", "document_date"),
                ("source_metadata", "source_metadata"),
                ("extracted_text_ref", "extracted_text_ref"),
                ("extracted_text_hash", "extracted_text_hash"),
                ("extracted_text_size_bytes", "extracted_text_size_bytes"),
                ("input_ref", "input_ref"),
                ("input_hash", "input_hash"),
            ):
                if row[column] != value[field]:
                    raise DocumentIndexConflict(
                        f"stored projection column {column} disagrees with document record"
                    )
            if row["source_record_refs_json"] != canonical_json(value["source_record_refs"]):
                raise DocumentIndexConflict("stored source refs disagree with document record")
            if row["company_refs_json"] != canonical_json(value["company_refs"]):
                raise DocumentIndexConflict("stored company refs disagree with document record")
            if type(row["extracted_text"]) is not str:
                raise DocumentIndexConflict("stored extracted text is not text")
            if _text_hash(row["extracted_text"]) != value["extracted_text_hash"]:
                raise DocumentIndexConflict("stored extracted text hash drifted")
            if len(row["extracted_text"].encode("utf-8")) != value["extracted_text_size_bytes"]:
                raise DocumentIndexConflict("stored extracted text size drifted")
            companies = [
                child["company_ref"]
                for child in self.connection.execute(
                    "SELECT company_ref FROM document_index_companies WHERE document_rowid=? ORDER BY company_ref",
                    (row["rowid"],),
                ).fetchall()
            ]
            if companies != value["company_refs"]:
                raise DocumentIndexConflict("stored company facet disagrees with document record")

    def search(
        self,
        query: str,
        *,
        company_ref: str | None = None,
        source_type: str | None = None,
        content_type: str | None = None,
        media_type: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        access_classes: Sequence[str] | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        match = self._safe_match_query(query)
        classes = self.visible_access_classes if access_classes is None else tuple(access_classes)
        if not classes or len(classes) != len(set(classes)) or any(item not in _ACCESS_CLASSES for item in classes):
            raise DocumentIndexValidationError("access_classes is invalid")
        if any(item not in self.visible_access_classes for item in classes):
            raise DocumentIndexValidationError("requested access class is not authorized for this projection")
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise DocumentIndexValidationError("limit must be between 1 and 1000")
        if type(offset) is not int or offset < 0:
            raise DocumentIndexValidationError("offset must be non-negative")
        company_ref = None if company_ref is None else self._filter_text(company_ref, "company_ref")
        source_type = None if source_type is None else self._filter_text(source_type, "source_type")
        content_type = None if content_type is None else self._filter_text(content_type, "content_type")
        media_type = None if media_type is None else self._filter_text(media_type, "media_type")
        date_from = None if date_from is None else _date(date_from, "date_from")
        date_to = None if date_to is None else _date(date_to, "date_to")
        if date_from is not None and date_to is not None and date_from > date_to:
            raise DocumentIndexValidationError("date_from must not be after date_to")
        placeholders = ",".join("?" for _ in classes)
        clauses = [f"d.access_class IN ({placeholders})"]
        params: list[Any] = list(classes)
        if company_ref is not None:
            clauses.append(
                "EXISTS (SELECT 1 FROM document_index_companies c "
                "WHERE c.document_rowid=d.rowid AND c.company_ref=?)"
            )
            params.append(company_ref)
        if source_type is not None:
            clauses.append("d.source_type=?")
            params.append(source_type)
        if content_type is not None:
            clauses.append("d.kind=?")
            params.append(content_type)
        if media_type is not None:
            clauses.append("d.media_type=?")
            params.append(media_type)
        if date_from is not None:
            clauses.append("d.document_date>=?")
            params.append(date_from)
        if date_to is not None:
            clauses.append("d.document_date<=?")
            params.append(date_to)
        sql = (
            "SELECT d.rowid,d.record_json,d.content_hash,d.access_class,d.source_type,d.kind,d.media_type,d.document_date "
            "FROM document_index_fts f "
            "JOIN document_index_documents d ON d.rowid=f.rowid "
            "WHERE f.document_index_fts MATCH ? AND " + " AND ".join(clauses) +
            " ORDER BY d.document_date,d.artifact_version_ref LIMIT ? OFFSET ?"
        )
        params = [match, *params, limit, offset]
        try:
            self._assert_projection_integrity()
            rows = self.connection.execute(sql, params).fetchall()
        except sqlite3.OperationalError as exc:
            raise DocumentIndexValidationError("FTS query rejected") from exc
        results = []
        for row in rows:
            try:
                value = json.loads(row["record_json"])
                self._validate_document_wire(value)
                if row["content_hash"] != content_hash(value):
                    raise DocumentIndexConflict("stored document row hash drifted")
                if any(
                    (
                        row_name == "access_class" and row[row_name] != value["access_class"]
                    )
                    or (row_name == "source_type" and row[row_name] != value["source_type"])
                    or (row_name == "kind" and row[row_name] != value["kind"])
                    or (row_name == "media_type" and row[row_name] != value["media_type"])
                    or (row_name == "document_date" and row[row_name] != value["document_date"])
                for row_name in ("access_class", "source_type", "kind", "media_type", "document_date")
                ):
                    raise DocumentIndexConflict("stored filter column disagrees with document record")
                if value["access_class"] not in classes:
                    raise DocumentIndexConflict("stored result exceeds authorized access classes")
            except (TypeError, ValueError) as exc:
                raise DocumentIndexConflict("stored DocumentIndex document is invalid") from exc
            results.append(value)
        return results

    def count(self) -> int:
        row = self.connection.execute("SELECT COUNT(*) AS count FROM document_index_documents").fetchone()
        return int(row["count"])


__all__ = [
    "BUILDER_REF",
    "DocumentIndex",
    "DocumentIndexConflict",
    "DocumentIndexError",
    "DocumentIndexInput",
    "DocumentIndexNotFound",
    "DocumentIndexValidationError",
    "extract_builtin_document_text",
    "make_document_index_input",
]
