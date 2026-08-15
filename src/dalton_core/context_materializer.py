"""Authority-bound, ephemeral ContextPack materialization.

The existing ContextPack is a deterministic ref-only selection projection.  It
does not make caller supplied text authoritative.  This module is the narrow
consumer boundary: it resolves only exact ClaimVersion and ArtifactVersion
refs, verifies the Core rows and raw object again, and returns a short-lived
render together with a hash-bearing manifest.  The manifest never contains
document text, storage locators, database paths, or credentials.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from .agenda import read_exact_mandate_version, read_exact_perception_snapshot
from .contracts import ClaimVersion
from .document_index import extract_builtin_document_text
from .observability import ObservabilityNotFound, ObservabilityStore
from .raw_spool import RawSpool
from .research_context import (
    count_dalton_search_tokens,
    validate_agenda_context_binding,
    validate_claim_index,
    validate_compiled_connector_plan,
    validate_context_pack,
)
from .research_review import validate_claim_version_v0_2
from .store import DaltonStore, canonical_json, content_hash


SCHEMA_VERSION = "0.1"
RENDERER_REF = "context-materializer:quoted-json-lines:0.1"
RENDERER_HASH = content_hash({"renderer_ref": RENDERER_REF})
AGENDA_RENDERER_REF = "context-materializer:agenda-quoted-json-lines:0.1"
AGENDA_RENDERER_HASH = content_hash({"renderer_ref": AGENDA_RENDERER_REF})
AGENDA_NO_CLAIM_INDEX_REF = "claim-index:none:agenda-context:0.1"
AGENDA_NO_CLAIM_INDEX_HASH = content_hash(
    {"claim_index_ref": AGENDA_NO_CLAIM_INDEX_REF}
)
TOKENIZER_REF = "tokenizer:dalton-search-token:0.1"
TOKENIZER_HASH = content_hash({"tokenizer_ref": TOKENIZER_REF})
CONTEXT_BUILDER_REF = "context-pack-builder:deterministic-ref-only:0.1"
CONTEXT_BUILDER_HASH = content_hash({"builder_ref": CONTEXT_BUILDER_REF})
SELECTION_POLICY_REF = "context-selector:priority-kind-ref:0.1"
SELECTION_POLICY_HASH = content_hash(
    {"selection_policy_ref": SELECTION_POLICY_REF}
)
TRUNCATION_REF = "truncation:whole-input-drop:0.1"
TRUNCATION_HASH = content_hash({"truncation_ref": TRUNCATION_REF})
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_ACCESS_CLASSES = frozenset({"public", "internal", "restricted"})
_SENSITIVE_KEYS = {
    "authorization", "cookie", "cookies", "password", "passwd", "secret", "token",
    "accesstoken", "apikey", "authtoken", "bearertoken", "clientsecret",
    "credentialvalue", "databasepath", "dbpath", "privatekey", "refreshtoken",
    "sessiontoken", "storagelocator", "connectionstring",
}


def _reject_sensitive_keys(value: Any, name: str) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            normalized = re.sub(r"[^a-z0-9]", "", str(key).lower())
            if normalized in _SENSITIVE_KEYS or normalized.endswith((
                "apikey", "authtoken", "bearertoken", "clientsecret", "privatekey",
                "refreshtoken", "sessiontoken", "connectionstring",
            )):
                raise ContextMaterializerError(f"{name} contains forbidden sensitive field")
            _reject_sensitive_keys(item, f"{name}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _reject_sensitive_keys(item, f"{name}[{index}]")


class ContextMaterializerError(ValueError):
    """Base error for the fail-closed materialization boundary."""


class ContextMaterializerConflict(ContextMaterializerError):
    """An immutable ref, hash, accounting value, or budget drifted."""


class ContextMaterializerUnsupported(ContextMaterializerError):
    """The ContextPack input kind or extractor is not supported."""


def _closed(value: Any, fields: set[str], name: str) -> dict[str, Any]:
    if type(value) is not dict:
        raise ContextMaterializerError(f"{name} must be an object")
    actual = set(value)
    if actual != fields:
        raise ContextMaterializerError(
            f"{name} shape mismatch: missing={sorted(fields - actual)}, "
            f"extra={sorted(actual - fields)}"
        )
    return dict(value)


def _text(value: Any, name: str) -> str:
    if type(value) is not str or not value:
        raise ContextMaterializerError(f"{name} must be a non-empty string")
    return value


def _hash(value: Any, name: str) -> str:
    value = _text(value, name)
    if _HASH_RE.fullmatch(value) is None:
        raise ContextMaterializerError(f"{name} must be lowercase SHA-256")
    return value


def _integer(value: Any, name: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or type(value) is not int or value < minimum:
        raise ContextMaterializerError(f"{name} must be an integer >= {minimum}")
    return value


def _timestamp(value: Any, name: str) -> str:
    value = _text(value, name)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ContextMaterializerError(f"{name} must be RFC3339") from exc
    if parsed.tzinfo is None:
        raise ContextMaterializerError(f"{name} must include timezone")
    return parsed.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _authority_hash(wire: Mapping[str, Any]) -> str:
    body = dict(wire)
    supplied = body.pop("content_hash", None)
    expected = content_hash(body)
    if supplied != expected:
        raise ContextMaterializerConflict("authority record content_hash mismatch")
    return expected


def _canonical_claim(wire: Mapping[str, Any]) -> dict[str, Any]:
    """Validate one formal ClaimVersion without trusting its SQL row."""

    schema = wire.get("schema_version")
    if schema == "0.1":
        try:
            return ClaimVersion.from_dict(wire).to_dict()
        except Exception as exc:
            raise ContextMaterializerError("ClaimVersion 0.1 is invalid") from exc
    if schema == "0.2":
        try:
            return validate_claim_version_v0_2(wire)
        except Exception as exc:
            raise ContextMaterializerError("ClaimVersion 0.2 is invalid") from exc
    raise ContextMaterializerUnsupported("unsupported ClaimVersion schema_version")


@dataclass(frozen=True, slots=True)
class _ResolvedInput:
    kind: str
    ref: str
    authority_hash: str
    body_value: Any
    body_text: str
    body_hash: str
    body_tokens: int
    body_bytes: int


@dataclass(frozen=True, slots=True)
class ContextMaterialization:
    """Ephemeral model input plus the durable-safe audit manifest."""

    rendered_text: str
    manifest: dict[str, Any]

    def __post_init__(self) -> None:
        """Keep the ephemeral body paired with its closed manifest.

        The dataclass is public, so callers must not be able to construct a
        body/manifest pair whose hash or envelope accounting disagrees.  The
        manifest is copied after validation so later replacement of the
        caller's top-level mapping cannot alter the pair silently.
        """

        if type(self.rendered_text) is not str:
            raise ContextMaterializerError("rendered_text must be a string")
        if type(self.manifest) is not dict:
            raise ContextMaterializerError("manifest must be a closed object")
        manifest = validate_context_materialization(self.manifest)
        if _sha256_text(self.rendered_text) != manifest["rendered_content_hash"]:
            raise ContextMaterializerConflict("rendered_text content_hash mismatch")
        rendered_tokens = count_dalton_search_tokens(self.rendered_text)
        rendered_bytes = len(self.rendered_text.encode("utf-8"))
        if rendered_tokens != manifest["totals"]["rendered_tokens"]:
            raise ContextMaterializerConflict("rendered_text token accounting mismatch")
        if rendered_bytes != manifest["totals"]["rendered_bytes"]:
            raise ContextMaterializerConflict("rendered_text byte accounting mismatch")
        object.__setattr__(self, "manifest", manifest)

    def to_dict(self) -> dict[str, Any]:
        """Return only the closed manifest; the render remains ephemeral."""

        return dict(self.manifest)


_ENTRY_FIELDS = {
    "kind", "ref", "hash", "selected", "selection_reason", "authority_ref",
    "authority_hash", "body_hash", "body_tokens", "body_bytes",
    "pack_original_tokens", "pack_original_bytes", "render_position",
    "rendered_tokens", "rendered_bytes", "failure_code", "content_hash",
}
_TOTAL_FIELDS = {
    "selected_count", "omitted_count", "failure_count", "body_tokens",
    "body_bytes", "overhead_tokens", "overhead_bytes", "rendered_tokens",
    "rendered_bytes",
}
_BUDGET_FIELDS = {"max_tokens", "max_bytes"}
_MANIFEST_FIELDS = {
    "schema_version", "id", "created_at", "context_pack_ref", "context_pack_hash",
    "renderer_ref", "renderer_hash", "tokenizer_ref", "tokenizer_hash", "budget",
    "inputs", "totals", "rendered_content_hash", "content_hash",
}


_SUPPORTED_KINDS = frozenset({"claim", "artifact", "mandate", "perception"})


def _validate_entry(value: Mapping[str, Any], name: str) -> dict[str, Any]:
    wire = _closed(value, _ENTRY_FIELDS, name)
    if wire["kind"] not in _SUPPORTED_KINDS:
        raise ContextMaterializerUnsupported(f"{name}.kind is unsupported")
    for field in ("ref", "selection_reason"):
        wire[field] = _text(wire[field], f"{name}.{field}")
    if wire["selection_reason"] not in {
        "included", "budget_tokens", "budget_bytes", "duplicate",
    }:
        raise ContextMaterializerError(f"{name}.selection_reason is invalid")
    _hash(wire["hash"], f"{name}.hash")
    _text(wire["authority_ref"], f"{name}.authority_ref")
    _hash(wire["authority_hash"], f"{name}.authority_hash")
    if wire["ref"] != wire["authority_ref"] or wire["hash"] != wire["authority_hash"]:
        raise ContextMaterializerConflict(f"{name} authority binding mismatch")
    _hash(wire["body_hash"], f"{name}.body_hash")
    if type(wire["selected"]) is not bool:
        raise ContextMaterializerError(f"{name}.selected must be boolean")
    for field in (
        "body_tokens", "body_bytes", "pack_original_tokens", "pack_original_bytes",
        "rendered_tokens", "rendered_bytes",
    ):
        wire[field] = _integer(wire[field], f"{name}.{field}")
    if (
        wire["body_tokens"] != wire["pack_original_tokens"]
        or wire["body_bytes"] != wire["pack_original_bytes"]
    ):
        raise ContextMaterializerConflict(f"{name} pack/body accounting mismatch")
    if wire["render_position"] is not None:
        wire["render_position"] = _integer(
            wire["render_position"], f"{name}.render_position", minimum=1
        )
    if wire["failure_code"] is not None:
        _text(wire["failure_code"], f"{name}.failure_code")
    if wire["selected"]:
        if wire["selection_reason"] != "included" or wire["render_position"] is None:
            raise ContextMaterializerConflict(f"{name} selected accounting is inconsistent")
    else:
        if wire["render_position"] is not None or wire["rendered_tokens"] or wire["rendered_bytes"]:
            raise ContextMaterializerConflict(f"{name} omitted accounting is inconsistent")
    supplied = _hash(wire["content_hash"], f"{name}.content_hash")
    if supplied != content_hash({key: value for key, value in wire.items() if key != "content_hash"}):
        raise ContextMaterializerConflict(f"{name}.content_hash mismatch")
    return wire


def validate_context_materialization(value: Mapping[str, Any]) -> dict[str, Any]:
    """Validate the closed, body-free manifest emitted by the materializer."""

    wire = _closed(value, _MANIFEST_FIELDS, "ContextMaterialization")
    if wire["schema_version"] != SCHEMA_VERSION:
        raise ContextMaterializerUnsupported("unsupported ContextMaterialization schema_version")
    for field in (
        "id", "context_pack_ref", "renderer_ref", "tokenizer_ref",
    ):
        wire[field] = _text(wire[field], field)
    for field in (
        "context_pack_hash", "renderer_hash", "tokenizer_hash", "rendered_content_hash",
        "content_hash",
    ):
        wire[field] = _hash(wire[field], field)
    wire["created_at"] = _timestamp(wire["created_at"], "created_at")
    renderer_bindings = {
        (RENDERER_REF, RENDERER_HASH),
        (AGENDA_RENDERER_REF, AGENDA_RENDERER_HASH),
    }
    if (wire["renderer_ref"], wire["renderer_hash"]) not in renderer_bindings:
        raise ContextMaterializerConflict("renderer binding drifted")
    if wire["tokenizer_ref"] != TOKENIZER_REF or wire["tokenizer_hash"] != TOKENIZER_HASH:
        raise ContextMaterializerConflict("tokenizer binding drifted")
    budget = _closed(wire["budget"], _BUDGET_FIELDS, "budget")
    for field in budget:
        budget[field] = _integer(budget[field], f"budget.{field}", minimum=1)
    wire["budget"] = budget
    if type(wire["inputs"]) is not list:
        raise ContextMaterializerError("inputs must be an array")
    inputs = [_validate_entry(item, f"inputs[{index}]") for index, item in enumerate(wire["inputs"])]
    has_agenda_input = any(
        item["kind"] in {"mandate", "perception"} for item in inputs
    )
    expected_renderer = (
        (AGENDA_RENDERER_REF, AGENDA_RENDERER_HASH)
        if has_agenda_input
        else (RENDERER_REF, RENDERER_HASH)
    )
    if (wire["renderer_ref"], wire["renderer_hash"]) != expected_renderer:
        raise ContextMaterializerConflict(
            "renderer binding does not match the materialized input kinds"
        )
    positions = [item["render_position"] for item in inputs if item["render_position"] is not None]
    if positions != list(range(1, len(positions) + 1)):
        raise ContextMaterializerConflict("render positions are not contiguous")
    totals = _closed(wire["totals"], _TOTAL_FIELDS, "totals")
    for field in totals:
        totals[field] = _integer(totals[field], f"totals.{field}")
    expected = {
        "selected_count": sum(item["selected"] for item in inputs),
        "omitted_count": sum(not item["selected"] for item in inputs),
        "failure_count": sum(item["failure_code"] is not None for item in inputs),
        "body_tokens": sum(item["body_tokens"] for item in inputs if item["selected"]),
        "body_bytes": sum(item["body_bytes"] for item in inputs if item["selected"]),
    }
    if any(totals[field] != value for field, value in expected.items()):
        raise ContextMaterializerConflict("materialization totals mismatch")
    if totals["rendered_tokens"] < sum(item["rendered_tokens"] for item in inputs):
        raise ContextMaterializerConflict("rendered token accounting is below input blocks")
    if totals["rendered_bytes"] < sum(item["rendered_bytes"] for item in inputs):
        raise ContextMaterializerConflict("rendered byte accounting is below input blocks")
    if totals["overhead_tokens"] != totals["rendered_tokens"] - totals["body_tokens"]:
        raise ContextMaterializerConflict("rendered token overhead mismatch")
    if totals["overhead_bytes"] != totals["rendered_bytes"] - totals["body_bytes"]:
        raise ContextMaterializerConflict("rendered byte overhead mismatch")
    if totals["rendered_tokens"] > budget["max_tokens"] or totals["rendered_bytes"] > budget["max_bytes"]:
        raise ContextMaterializerConflict("materialized render exceeds budget")
    if wire["id"] != "context-materialization:" + content_hash({
        key: value for key, value in wire.items() if key not in {"id", "content_hash", "created_at"}
    }):
        raise ContextMaterializerConflict("materialization id binding mismatch")
    expected_hash = content_hash({key: value for key, value in wire.items() if key != "content_hash"})
    if wire["content_hash"] != expected_hash:
        raise ContextMaterializerConflict("materialization content_hash mismatch")
    return wire


class ContextMaterializer:
    """Resolve a ContextPack against exact Core authorities and render it."""

    def __init__(
        self,
        core: DaltonStore,
        observability: ObservabilityStore,
        raw_spool: RawSpool | None,
        visible_access_classes: Sequence[str] = ("public",),
    ) -> None:
        if type(core) is not DaltonStore:
            raise TypeError("core must be the exact DaltonStore")
        if type(observability) is not ObservabilityStore or observability.store is not core:
            raise TypeError("observability must share the exact DaltonStore")
        # A materializer without a spool is legal and strictly weaker: it can
        # resolve Core-resident records but no raw artifact object.  Callers
        # that never quote an artifact (Agenda) must not have to hand a raw
        # object store to a boundary that would only widen their blast radius.
        if raw_spool is not None and type(raw_spool) is not RawSpool:
            raise TypeError("raw_spool must be the exact RawSpool or None")
        classes = tuple(visible_access_classes)
        if not classes or len(classes) != len(set(classes)) or any(item not in _ACCESS_CLASSES for item in classes):
            raise ContextMaterializerError("visible_access_classes is invalid")
        self.core = core
        self.observability = observability
        self.raw_spool = raw_spool
        self.visible_access_classes = classes

    def _read_claim(
        self,
        ref: str,
        expected_hash: str,
        claim_index_entry: Mapping[str, Any],
    ) -> _ResolvedInput:
        row = self.core.connection.execute(
            "SELECT * FROM claim_versions WHERE claim_version_id=?", (ref,)
        ).fetchone()
        if row is None:
            raise ContextMaterializerConflict("ClaimVersion authority row is missing")
        try:
            wire = json.loads(row["claim_json"])
        except (TypeError, ValueError) as exc:
            raise ContextMaterializerError("ClaimVersion claim_json is invalid") from exc
        if type(wire) is not dict or wire.get("id") != ref:
            raise ContextMaterializerConflict("ClaimVersion identity binding failed")
        if (
            row["claim_ref"] != wire.get("claim_ref")
            or row["version_number"] != wire.get("version")
            or row["prior_version_id"] != wire.get("prior_version_ref")
            or row["created_at"] != wire.get("created_at")
            or row["content_hash"] != wire.get("content_hash")
            or row["claim_json"] != canonical_json(wire)
        ):
            raise ContextMaterializerConflict("ClaimVersion SQL columns drifted")
        authority_hash = _authority_hash(wire)
        if authority_hash != expected_hash:
            raise ContextMaterializerConflict("ClaimVersion ref/hash binding failed")
        canonical = _canonical_claim(wire)
        if canonical != wire:
            raise ContextMaterializerConflict("ClaimVersion is not canonical")
        _reject_sensitive_keys(canonical, "ClaimVersion")
        # Claim status is a derived projection, not a ClaimVersion field.  It
        # still matters to the model: a contested or retracted claim must not
        # look identical to a proposed claim.  Freeze the exact ClaimIndex
        # entry into the body while keeping ClaimVersion as the only fact
        # authority.  The pack-level ClaimIndex ref/hash binds this projection.
        body_value = {
            "claim_version": canonical,
            "claim_index_entry": dict(claim_index_entry),
        }
        body_text = canonical_json(body_value)
        return _ResolvedInput(
            "claim", ref, authority_hash, body_value, body_text,
            _sha256_text(body_text),
            count_dalton_search_tokens(body_text), len(body_text.encode("utf-8")),
        )

    def _read_mandate(self, ref: str, expected_hash: str) -> _ResolvedInput:
        """Resolve a MandateVersion from Core, never from a caller body."""

        try:
            wire = read_exact_mandate_version(self.core.connection, ref)
        except Exception as exc:
            raise ContextMaterializerConflict(
                "MandateVersion authority record is unavailable or drifted"
            ) from exc
        if wire["id"] != ref:
            raise ContextMaterializerConflict("MandateVersion identity binding failed")
        if wire["content_hash"] != expected_hash:
            raise ContextMaterializerConflict("MandateVersion ref/hash binding failed")
        if _authority_hash(wire) != expected_hash:
            raise ContextMaterializerConflict("MandateVersion content_hash binding failed")
        _reject_sensitive_keys(wire, "MandateVersion")
        body_text = canonical_json(wire)
        return _ResolvedInput(
            "mandate", ref, expected_hash, wire, body_text, _sha256_text(body_text),
            count_dalton_search_tokens(body_text), len(body_text.encode("utf-8")),
        )

    def _read_perception(self, ref: str, expected_hash: str) -> _ResolvedInput:
        """Resolve a PerceptionSnapshot from Core's append-only authority."""

        try:
            wire = read_exact_perception_snapshot(self.core.connection, ref)
        except Exception as exc:
            raise ContextMaterializerConflict(
                "PerceptionSnapshot authority record is unavailable or drifted"
            ) from exc
        if wire["snapshot_id"] != ref:
            raise ContextMaterializerConflict("PerceptionSnapshot identity binding failed")
        if wire["content_hash"] != expected_hash:
            raise ContextMaterializerConflict("PerceptionSnapshot ref/hash binding failed")
        if _authority_hash(wire) != expected_hash:
            raise ContextMaterializerConflict("PerceptionSnapshot content_hash binding failed")
        _reject_sensitive_keys(wire, "PerceptionSnapshot")
        body_text = canonical_json(wire)
        return _ResolvedInput(
            "perception", ref, expected_hash, wire, body_text, _sha256_text(body_text),
            count_dalton_search_tokens(body_text), len(body_text.encode("utf-8")),
        )

    def _read_artifact(self, ref: str, expected_hash: str) -> _ResolvedInput:
        if self.raw_spool is None:
            raise ContextMaterializerUnsupported(
                "this materializer has no raw spool and cannot quote an artifact"
            )
        try:
            api_wire = self.observability.get_artifact_version(ref)
        except ObservabilityNotFound as exc:
            raise ContextMaterializerConflict("ArtifactVersion authority row is missing") from exc
        if type(api_wire) is not dict or api_wire.get("id") != ref:
            raise ContextMaterializerConflict("ArtifactVersion identity binding failed")
        authority_hash = _authority_hash(api_wire)
        if authority_hash != expected_hash:
            raise ContextMaterializerConflict("ArtifactVersion ref/hash binding failed")
        connection = self.observability.connection
        index = connection.execute(
            "SELECT * FROM observability_artifact_version_index WHERE version_id=?", (ref,)
        ).fetchone()
        if index is None or index["record_hash"] != authority_hash:
            raise ContextMaterializerConflict("ArtifactVersion index hash binding failed")
        schema = index["schema_version"]
        table = "observability_artifact_versions" if schema == "0.1" else "observability_artifact_versions_v2" if schema == "0.2" else None
        if table is None:
            raise ContextMaterializerUnsupported("unsupported ArtifactVersion schema_version")
        row = connection.execute(f"SELECT * FROM {table} WHERE version_id=?", (ref,)).fetchone()
        if row is None:
            raise ContextMaterializerConflict("ArtifactVersion SQL row is missing")
        try:
            sql_wire = json.loads(row["record_json"])
        except (TypeError, ValueError) as exc:
            raise ContextMaterializerError("ArtifactVersion record_json is invalid") from exc
        if canonical_json(sql_wire) != canonical_json(api_wire):
            raise ContextMaterializerConflict("ArtifactVersion API and SQL records disagree")
        if row["content_hash"] != authority_hash or row["record_json"] != canonical_json(sql_wire):
            raise ContextMaterializerConflict("ArtifactVersion record hash binding failed")
        producer_field = "producer_invocation_ref" if schema == "0.1" else "producer_execution_ref"
        for column, field in (
            ("artifact_ref", "artifact_ref"), ("version_number", "version"),
            ("artifact_content_hash", "artifact_content_hash"), ("storage_locator", "storage_locator"),
            ("work_order_ref", "work_order_ref"), ("result_envelope_ref", "result_envelope_ref"),
            ("result_envelope_hash", "result_envelope_hash"), (producer_field, producer_field),
        ):
            if row[column] != sql_wire.get(field):
                raise ContextMaterializerConflict(f"ArtifactVersion column {column} drifted")
        for column, field in (
            ("artifact_ref", "artifact_ref"),
            ("version_number", "version"),
            ("schema_version", "schema"),
            ("prior_version_ref", "prior_version_ref"),
            ("producer_execution_ref", producer_field),
            ("record_hash", "content_hash"),
            ("created_at", "created_at"),
        ):
            expected = schema if field == "schema" else sql_wire[field]
            if index[column] != expected:
                raise ContextMaterializerConflict(f"ArtifactVersion index column {column} drifted")
        if sql_wire.get("access_class") not in self.visible_access_classes:
            raise ContextMaterializerConflict("ArtifactVersion access class is not authorized")
        raw_hash = sql_wire.get("artifact_content_hash")
        size = sql_wire.get("size_bytes")
        if type(raw_hash) is not str or _HASH_RE.fullmatch(raw_hash) is None or isinstance(size, bool) or type(size) is not int or size < 0:
            raise ContextMaterializerError("ArtifactVersion raw binding fields are invalid")
        expected_locator = f"spool:objects/{raw_hash[:2]}/{raw_hash}"
        if sql_wire.get("storage_locator") != expected_locator:
            raise ContextMaterializerConflict(
                "ArtifactVersion storage locator is not bound to its content hash"
            )
        try:
            raw = self.raw_spool.read_object(raw_hash)
        except Exception as exc:
            raise ContextMaterializerConflict("raw artifact object is unavailable") from exc
        if len(raw) != size or hashlib.sha256(raw).hexdigest() != raw_hash:
            raise ContextMaterializerConflict("raw artifact hash/size binding failed")
        media_type = str(sql_wire.get("media_type", "")).split(";", 1)[0].lower()
        if media_type == "application/json" or media_type.endswith("+json"):
            mode = "json"
        elif media_type in {"text/plain", "text/markdown", "text/csv"}:
            mode = "utf8"
        else:
            raise ContextMaterializerUnsupported(
                f"unsupported ArtifactVersion media_type: {media_type}"
            )
        body_text = extract_builtin_document_text(raw, mode)
        if mode == "json":
            try:
                body_value = json.loads(body_text)
            except (TypeError, ValueError) as exc:
                raise ContextMaterializerError("canonical JSON extractor returned invalid JSON") from exc
            _reject_sensitive_keys(body_value, "Artifact JSON")
        else:
            body_value = body_text
        body_hash = _sha256_text(body_text)
        return _ResolvedInput(
            "artifact", ref, authority_hash, body_value, body_text, body_hash,
            count_dalton_search_tokens(body_text), len(body_text.encode("utf-8")),
        )

    def _resolve(
        self,
        kind: str,
        ref: str,
        expected_hash: str,
        *,
        claim_entries: Mapping[str, Mapping[str, Any]],
    ) -> _ResolvedInput:
        if kind == "claim":
            entry = claim_entries.get(ref)
            if entry is None or entry.get("claim_version_hash") != expected_hash:
                raise ContextMaterializerConflict(
                    "ContextPack claim is absent from ClaimIndex"
                )
            return self._read_claim(ref, expected_hash, entry)
        if kind == "artifact":
            return self._read_artifact(ref, expected_hash)
        if kind == "mandate":
            return self._read_mandate(ref, expected_hash)
        if kind == "perception":
            return self._read_perception(ref, expected_hash)
        raise ContextMaterializerUnsupported(f"ContextPack input kind is unsupported: {kind}")

    @staticmethod
    def _validate_agenda_inputs(
        pack: Mapping[str, Any], binding: Mapping[str, Any]
    ) -> None:
        """Both Agenda facts are required and neither may be budget-dropped.

        A prompt built from a mandate without perception, or perception
        without its mandate, is a different task than the one the cycle
        authorized.  Dropping either under budget pressure would silently
        change what the model was asked, so it fails the whole render.
        """

        required = {
            "mandate": (
                binding["mandate_version_ref"], binding["mandate_version_hash"],
            ),
            "perception": (
                binding["perception_snapshot_ref"],
                binding["perception_snapshot_hash"],
            ),
        }
        seen: set[str] = set()
        for index, item in enumerate(pack["inputs"]):
            expected = required.get(item["kind"])
            if expected is None:
                raise ContextMaterializerConflict(
                    f"AgendaContextBinding does not admit inputs[{index}].kind"
                )
            if (item["ref"], item["hash"]) != expected:
                raise ContextMaterializerConflict(
                    f"inputs[{index}] is not the AgendaContextBinding {item['kind']}"
                )
            if item["kind"] in seen:
                raise ContextMaterializerConflict(
                    f"AgendaContextBinding admits one {item['kind']} input"
                )
            if not item["selected"]:
                raise ContextMaterializerConflict(
                    f"required Agenda {item['kind']} input was dropped by budget"
                )
            seen.add(item["kind"])
        if seen != set(required):
            raise ContextMaterializerConflict(
                "Agenda context requires both a mandate and a perception input"
            )

    @staticmethod
    def _plan_binding(value: Mapping[str, Any]) -> tuple[str, dict[str, Any]]:
        """Accept exactly one member of the closed plan-binding union.

        The union is discriminated before validation so an Agenda binding can
        never be silently retried as a connector plan (or the reverse), and an
        unknown record shape fails closed instead of degrading.
        """

        if type(value) is not dict:
            raise ContextMaterializerError("plan binding must be an object")
        if "steps" in value:
            return "compiled_plan", validate_compiled_connector_plan(value)
        if "binder_ref" in value:
            return "agenda_context_binding", validate_agenda_context_binding(value)
        raise ContextMaterializerUnsupported("plan binding kind is unsupported")

    def _validate_bindings(
        self,
        pack: Mapping[str, Any],
        *,
        compiled_plan: Mapping[str, Any],
        claim_index: Mapping[str, Any] | None,
    ) -> tuple[dict[str, dict[str, Any]], str, dict[str, Any]]:
        binding_kind, plan = self._plan_binding(compiled_plan)
        if (
            plan["id"] != pack["compiled_plan_ref"]
            or plan["content_hash"] != pack["compiled_plan_hash"]
            or plan["task_ref"] != pack["task_ref"]
            or plan["task_hash"] != pack["task_hash"]
        ):
            raise ContextMaterializerConflict("ContextPack plan/task binding drifted")
        if binding_kind == "agenda_context_binding":
            self._validate_agenda_inputs(pack, plan)
            if claim_index is not None:
                raise ContextMaterializerConflict(
                    "Agenda context does not admit a ClaimIndex body"
                )
            if (
                pack["claim_index_ref"] != AGENDA_NO_CLAIM_INDEX_REF
                or pack["claim_index_hash"] != AGENDA_NO_CLAIM_INDEX_HASH
            ):
                raise ContextMaterializerConflict(
                    "Agenda ContextPack no-ClaimIndex binding drifted"
                )
            return {}, binding_kind, plan
        else:
            for item in pack["inputs"]:
                if item["kind"] in {"mandate", "perception"}:
                    raise ContextMaterializerUnsupported(
                        "mandate/perception inputs require an AgendaContextBinding"
                    )
        if claim_index is None:
            raise ContextMaterializerConflict(
                "connector ContextPack requires an exact ClaimIndex"
            )
        index = validate_claim_index(claim_index)
        if (
            index["id"] != pack["claim_index_ref"]
            or index["content_hash"] != pack["claim_index_hash"]
        ):
            raise ContextMaterializerConflict("ContextPack ClaimIndex binding drifted")
        # Do not rebuild an old ClaimIndex from today's larger append-only
        # Ledger: that would make a historical ContextPack unreplayable after
        # an unrelated claim or adjudication is appended.  ClaimIndex's own
        # builder derives its snapshot from Core.  This consumer verifies the
        # exact frozen index wire and then re-verifies every selected
        # ClaimVersion directly against Core before rendering it.
        entries = {entry["claim_version_ref"]: entry for entry in index["entries"]}
        for item in pack["inputs"]:
            if item["kind"] == "claim":
                entry = entries.get(item["ref"])
                if entry is None or entry["claim_version_hash"] != item["hash"]:
                    raise ContextMaterializerConflict("ContextPack claim is absent from ClaimIndex")
        return entries, binding_kind, plan

    @staticmethod
    def _render(
        pack: Mapping[str, Any],
        resolved: Sequence[_ResolvedInput],
        selected_indices: Sequence[int],
        *,
        binding_kind: str,
        binding: Mapping[str, Any],
    ) -> tuple[str, list[dict[str, int]], dict[str, int], str, str]:
        if binding_kind == "agenda_context_binding":
            # Agenda replay must not move when unrelated claims change the
            # empty compatibility ClaimIndex carried by ContextPack 0.1.  Its
            # model-visible envelope therefore binds to the exact cycle
            # context, while the durable manifest still records the concrete
            # ContextPack used for this materialization.
            renderer_ref = AGENDA_RENDERER_REF
            renderer_hash = AGENDA_RENDERER_HASH
            context_binding = {
                "agenda_context_binding_ref": binding["id"],
                "agenda_context_binding_hash": binding["content_hash"],
            }
        else:
            renderer_ref = RENDERER_REF
            renderer_hash = RENDERER_HASH
            context_binding = {
                "context_pack_ref": pack["id"],
                "context_pack_hash": pack["content_hash"],
            }
        header = {
            "_dalton_context": "materialization",
            "schema_version": SCHEMA_VERSION,
            **context_binding,
            "renderer_ref": renderer_ref,
            "renderer_hash": renderer_hash,
            "tokenizer_ref": TOKENIZER_REF,
            "tokenizer_hash": TOKENIZER_HASH,
            "quoted_data_only": True,
        }
        lines = [canonical_json(header)]
        block_stats: list[dict[str, int]] = []
        for position, index in enumerate(selected_indices, start=1):
            item = resolved[index]
            block = {
                "_dalton_quoted_input": {
                    "schema_version": SCHEMA_VERSION,
                    "position": position,
                    "kind": item.kind,
                    "ref": item.ref,
                    "authority_ref": item.ref,
                    "authority_hash": item.authority_hash,
                    "body_hash": item.body_hash,
                    "body_tokens": item.body_tokens,
                    "body_bytes": item.body_bytes,
                    "quoted_data": item.body_value,
                }
            }
            line = canonical_json(block)
            lines.append(line)
            block_stats.append({
                "tokens": count_dalton_search_tokens(line),
                "bytes": len(line.encode("utf-8")) + 1,
            })
        footer = {
            "_dalton_context_end": {
                "schema_version": SCHEMA_VERSION,
                **context_binding,
                "selected_count": len(selected_indices),
                "quoted_data_only": True,
            }
        }
        lines.append(canonical_json(footer))
        rendered = "\n".join(lines) + "\n"
        total = {
            "tokens": count_dalton_search_tokens(rendered),
            "bytes": len(rendered.encode("utf-8")),
        }
        body = {
            "tokens": sum(resolved[index].body_tokens for index in selected_indices),
            "bytes": sum(resolved[index].body_bytes for index in selected_indices),
        }
        overhead = {
            "tokens": total["tokens"] - body["tokens"],
            "bytes": total["bytes"] - body["bytes"],
        }
        if overhead["tokens"] < 0 or overhead["bytes"] < 0:
            raise ContextMaterializerConflict("renderer accounting underflow")
        return (
            rendered,
            block_stats,
            {"tokens": overhead["tokens"], "bytes": overhead["bytes"], **total},
            renderer_ref,
            renderer_hash,
        )

    def materialize(
        self,
        context_pack: Mapping[str, Any],
        *,
        max_rendered_tokens: int,
        max_rendered_bytes: int,
        compiled_plan: Mapping[str, Any],
        claim_index: Mapping[str, Any] | None,
        created_at: str | None = None,
    ) -> ContextMaterialization:
        pack = validate_context_pack(context_pack)
        if (
            pack["builder_ref"] != CONTEXT_BUILDER_REF
            or pack["builder_hash"] != CONTEXT_BUILDER_HASH
            or pack["selection_policy_ref"] != SELECTION_POLICY_REF
            or pack["selection_policy_hash"] != SELECTION_POLICY_HASH
            or pack["tokenizer_ref"] != TOKENIZER_REF
            or pack["tokenizer_hash"] != TOKENIZER_HASH
            or pack["truncation_ref"] != TRUNCATION_REF
            or pack["truncation_hash"] != TRUNCATION_HASH
        ):
            raise ContextMaterializerConflict(
                "ContextPack build/selection/tokenizer/truncation policy is unsupported"
            )
        max_tokens = _integer(max_rendered_tokens, "max_rendered_tokens", minimum=1)
        max_bytes = _integer(max_rendered_bytes, "max_rendered_bytes", minimum=1)
        claim_entries, binding_kind, binding = self._validate_bindings(
            pack, compiled_plan=compiled_plan, claim_index=claim_index
        )
        resolved: list[_ResolvedInput] = []
        for index, item in enumerate(pack["inputs"]):
            result = self._resolve(
                item["kind"], item["ref"], item["hash"],
                claim_entries=claim_entries,
            )
            if result.body_tokens != item["original_tokens"] or result.body_bytes != item["original_bytes"]:
                raise ContextMaterializerConflict(
                    f"ContextPack accounting drifted for inputs[{index}]"
                )
            resolved.append(result)
        selected_indices = [index for index, item in enumerate(pack["inputs"]) if item["selected"]]
        rendered, block_stats, render_totals, renderer_ref, renderer_hash = self._render(
            pack,
            resolved,
            selected_indices,
            binding_kind=binding_kind,
            binding=binding,
        )
        if render_totals["tokens"] > max_tokens or render_totals["bytes"] > max_bytes:
            raise ContextMaterializerConflict("rendered ContextPack exceeds envelope-inclusive budget")
        entries: list[dict[str, Any]] = []
        selected_position = 0
        for index, (item, result) in enumerate(zip(pack["inputs"], resolved)):
            selected = bool(item["selected"])
            if selected:
                selected_position += 1
                rendered_tokens = block_stats[selected_position - 1]["tokens"]
                rendered_bytes = block_stats[selected_position - 1]["bytes"]
                position: int | None = selected_position
            else:
                rendered_tokens = rendered_bytes = 0
                position = None
            entry = {
                "kind": item["kind"], "ref": item["ref"], "hash": item["hash"],
                "selected": selected, "selection_reason": item["selection_reason"],
                "authority_ref": result.ref, "authority_hash": result.authority_hash,
                "body_hash": result.body_hash, "body_tokens": result.body_tokens,
                "body_bytes": result.body_bytes,
                "pack_original_tokens": item["original_tokens"],
                "pack_original_bytes": item["original_bytes"],
                "render_position": position, "rendered_tokens": rendered_tokens,
                "rendered_bytes": rendered_bytes, "failure_code": None,
            }
            entry["content_hash"] = content_hash(entry)
            entries.append(entry)
        materialization_created_at = pack["created_at"] if created_at is None else _timestamp(created_at, "created_at")
        base = {
            "schema_version": SCHEMA_VERSION,
            "id": "pending",
            "created_at": materialization_created_at,
            "context_pack_ref": pack["id"],
            "context_pack_hash": pack["content_hash"],
            "renderer_ref": renderer_ref, "renderer_hash": renderer_hash,
            "tokenizer_ref": TOKENIZER_REF, "tokenizer_hash": TOKENIZER_HASH,
            "budget": {"max_tokens": max_tokens, "max_bytes": max_bytes},
            "inputs": entries,
            "totals": {
                "selected_count": len(selected_indices),
                "omitted_count": len(pack["inputs"]) - len(selected_indices),
                "failure_count": 0,
                "body_tokens": sum(resolved[index].body_tokens for index in selected_indices),
                "body_bytes": sum(resolved[index].body_bytes for index in selected_indices),
                "overhead_tokens": render_totals["tokens"] - sum(resolved[index].body_tokens for index in selected_indices),
                "overhead_bytes": render_totals["bytes"] - sum(resolved[index].body_bytes for index in selected_indices),
                "rendered_tokens": render_totals["tokens"], "rendered_bytes": render_totals["bytes"],
            },
            "rendered_content_hash": _sha256_text(rendered),
        }
        base["id"] = "context-materialization:" + content_hash({
            key: value for key, value in base.items() if key not in {"id", "content_hash", "created_at"}
        })
        base["content_hash"] = content_hash(base)
        manifest = validate_context_materialization(base)
        return ContextMaterialization(rendered_text=rendered, manifest=manifest)

    def build_authority_context_pack(
        self,
        input_specs: Sequence[Mapping[str, Any]],
        *,
        task_ref: str,
        task_hash: str,
        compiled_plan_ref: str,
        compiled_plan_hash: str,
        claim_index_ref: str,
        claim_index_hash: str,
        claim_index: Mapping[str, Any],
        created_at: str,
        max_tokens: int,
        max_bytes: int,
    ) -> dict[str, Any]:
        """Build the legacy 0.1 pack shape from exact authority bodies.

        The caller supplies only refs, hashes, and priority.  This adapter is
        intentionally separate from ``build_context_pack`` so old caller-text
        fixtures remain usable for ref-only coordinator tests but cannot pass
        this materializer's accounting check.
        """

        index_wire = validate_claim_index(claim_index)
        if (
            index_wire["id"] != claim_index_ref
            or index_wire["content_hash"] != claim_index_hash
        ):
            raise ContextMaterializerConflict(
                "authority-bound builder ClaimIndex binding drifted"
            )
        claim_entries = {
            entry["claim_version_ref"]: entry for entry in index_wire["entries"]
        }
        specs: list[dict[str, Any]] = []
        for index, raw in enumerate(input_specs):
            spec = _closed(raw, {"kind", "ref", "hash", "priority"}, f"authority_input[{index}]")
            kind = _text(spec["kind"], f"authority_input[{index}].kind")
            ref = _text(spec["ref"], f"authority_input[{index}].ref")
            digest = _hash(spec["hash"], f"authority_input[{index}].hash")
            priority = _integer(spec["priority"], f"authority_input[{index}].priority")
            result = self._resolve(
                kind, ref, digest, claim_entries=claim_entries
            )
            specs.append({"kind": kind, "ref": ref, "hash": digest, "priority": priority, "content": result.body_text})
        from .research_context import build_context_pack

        return build_context_pack(
            specs,
            task_ref=task_ref, task_hash=task_hash,
            compiled_plan_ref=compiled_plan_ref, compiled_plan_hash=compiled_plan_hash,
            claim_index_ref=claim_index_ref, claim_index_hash=claim_index_hash,
            created_at=created_at, max_tokens=max_tokens, max_bytes=max_bytes,
        )

    def build_agenda_authority_context_pack(
        self,
        input_specs: Sequence[Mapping[str, Any]],
        *,
        agenda_binding: Mapping[str, Any],
        created_at: str,
        max_tokens: int,
        max_bytes: int,
    ) -> dict[str, Any]:
        """Build one Agenda pack without inventing or scanning a ClaimIndex.

        ContextPack 0.1 has mandatory ClaimIndex ref/hash fields.  Agenda
        quotes no claim, so those fields carry a frozen, explicit no-index
        sentinel.  The exact AgendaContextBinding remains the task/plan
        authority, and materialization accepts the sentinel only for that
        binding kind.
        """

        binding = validate_agenda_context_binding(agenda_binding)
        specs: list[dict[str, Any]] = []
        for index, raw in enumerate(input_specs):
            spec = _closed(
                raw,
                {"kind", "ref", "hash", "priority"},
                f"agenda_authority_input[{index}]",
            )
            kind = _text(spec["kind"], f"agenda_authority_input[{index}].kind")
            if kind not in {"mandate", "perception"}:
                raise ContextMaterializerUnsupported(
                    "Agenda ContextPack only accepts mandate/perception inputs"
                )
            ref = _text(spec["ref"], f"agenda_authority_input[{index}].ref")
            digest = _hash(
                spec["hash"], f"agenda_authority_input[{index}].hash"
            )
            priority = _integer(
                spec["priority"], f"agenda_authority_input[{index}].priority"
            )
            result = self._resolve(kind, ref, digest, claim_entries={})
            specs.append(
                {
                    "kind": kind,
                    "ref": ref,
                    "hash": digest,
                    "priority": priority,
                    "content": result.body_text,
                }
            )
        from .research_context import build_context_pack

        return build_context_pack(
            specs,
            task_ref=binding["task_ref"],
            task_hash=binding["task_hash"],
            compiled_plan_ref=binding["id"],
            compiled_plan_hash=binding["content_hash"],
            claim_index_ref=AGENDA_NO_CLAIM_INDEX_REF,
            claim_index_hash=AGENDA_NO_CLAIM_INDEX_HASH,
            created_at=created_at,
            max_tokens=max_tokens,
            max_bytes=max_bytes,
        )


__all__ = [
    "AGENDA_NO_CLAIM_INDEX_HASH", "AGENDA_NO_CLAIM_INDEX_REF",
    "AGENDA_RENDERER_HASH", "AGENDA_RENDERER_REF",
    "ContextMaterialization", "ContextMaterializer", "ContextMaterializerConflict",
    "ContextMaterializerError", "ContextMaterializerUnsupported",
    "CONTEXT_BUILDER_HASH", "CONTEXT_BUILDER_REF", "RENDERER_HASH",
    "RENDERER_REF", "SCHEMA_VERSION", "SELECTION_POLICY_HASH",
    "SELECTION_POLICY_REF", "TOKENIZER_HASH", "TOKENIZER_REF",
    "TRUNCATION_HASH", "TRUNCATION_REF", "validate_context_materialization",
]
