"""Fail-closed OpenClaw skill/MCP metadata import.

The snapshot boundary is deliberately narrower than an OpenClaw workspace or
runtime export.  Skill instructions are represented only by opaque refs and
hashes.  MCP tools may carry JSON Schema documents, but never credentials,
server configuration, prompts, executable code, or tool output.
"""

from __future__ import annotations

import copy
import json
import re
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from typing import Any, Callable

from .capability_catalog import (
    CapabilityCatalog,
    CapabilityPermissions,
    CatalogValidationError,
    SCHEMA_VERSION as CATALOG_SCHEMA_VERSION,
    canonical_hash,
    canonical_json,
)


SNAPSHOT_SCHEMA_VERSION = "0.2"
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_REF_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+._-]*:[^\s]+$")
_CANONICAL_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
_SKILL_SOURCES = frozenset(
    {
        "openclaw-workspace", "openclaw-managed", "openclaw-extra",
        "openclaw-plugin", "openclaw-bundled",
    }
)
_SENSITIVE_SCHEMA_KEYS = frozenset(
    {
        "access_token", "api_key", "apikey", "authorization", "client_secret",
        "cookie", "cookies", "credential", "credentials", "headers", "password",
        "refresh_token", "secret",
    }
)


class MetadataImportError(Exception):
    pass


class MetadataValidationError(MetadataImportError, ValueError):
    pass


class MetadataConflict(MetadataImportError):
    pass


def _closed(value: Any, fields: set[str], name: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise MetadataValidationError(f"{name} must be an object")
    missing = fields - set(value)
    unknown = set(value) - fields
    if missing or unknown:
        raise MetadataValidationError(
            f"{name} has invalid closed shape; missing={sorted(missing)}, "
            f"unknown={sorted(unknown)}"
        )
    try:
        return json.loads(canonical_json(value))
    except (TypeError, ValueError) as exc:
        raise MetadataValidationError(f"{name} must be finite JSON") from exc


def _text(value: Any, name: str, *, nullable: bool = False) -> str | None:
    if value is None and nullable:
        return None
    if not isinstance(value, str) or not value.strip():
        raise MetadataValidationError(f"{name} must be a non-empty string")
    return value.strip()


def _hash(value: Any, name: str) -> str:
    value = _text(value, name)
    if not isinstance(value, str) or not _HASH_RE.fullmatch(value):
        raise MetadataValidationError(f"{name} must be lowercase SHA-256 hex")
    return value


def _ref(value: Any, name: str) -> str:
    value = _text(value, name)
    if not isinstance(value, str) or not _REF_RE.fullmatch(value):
        raise MetadataValidationError(f"{name} must be an opaque namespaced ref")
    if value.startswith(("file:", "path:", "http:", "https:")):
        raise MetadataValidationError(f"{name} must not reveal a path or transport URL")
    return value


def _timestamp(value: Any, name: str) -> str:
    value = _text(value, name)
    assert isinstance(value, str)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise MetadataValidationError(f"{name} must be RFC3339") from exc
    if parsed.tzinfo is None:
        raise MetadataValidationError(f"{name} must include timezone")
    return parsed.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _boolean(value: Any, name: str) -> bool:
    if type(value) is not bool:
        raise MetadataValidationError(f"{name} must be boolean")
    return value


def _canonical_name(value: Any, name: str) -> str:
    value = _text(value, name)
    assert isinstance(value, str)
    if not _CANONICAL_NAME_RE.fullmatch(value):
        raise MetadataValidationError(f"{name} must be a canonical lowercase name")
    if len(value) > 128:
        raise MetadataValidationError(f"{name} is too long")
    return value


def _unique_names(value: Any, name: str) -> list[str]:
    if not isinstance(value, list):
        raise MetadataValidationError(f"{name} must be an array")
    result = [_canonical_name(item, f"{name}[]") for item in value]
    if len(result) != len(set(result)):
        raise MetadataValidationError(f"{name} must contain unique values")
    return result


def _verify_content_hash(wire: dict[str, Any], name: str) -> dict[str, Any]:
    declared = _hash(wire.pop("metadata_hash"), f"{name}.metadata_hash")
    if canonical_hash(wire) != declared:
        raise MetadataConflict(f"{name}.metadata_hash mismatch")
    wire["metadata_hash"] = declared
    return wire


def _schema_has_credential_shape(value: Any) -> bool:
    if isinstance(value, Mapping):
        for key, child in value.items():
            normalized = re.sub(r"[^a-z0-9]+", "_", str(key).lower()).strip("_")
            if normalized in _SENSITIVE_SCHEMA_KEYS:
                return True
            if key == "properties" and isinstance(child, Mapping):
                if any(
                    re.sub(r"[^a-z0-9]+", "_", str(prop).lower()).strip("_")
                    in _SENSITIVE_SCHEMA_KEYS
                    for prop in child
                ):
                    return True
            if _schema_has_credential_shape(child):
                return True
    elif isinstance(value, list):
        return any(_schema_has_credential_shape(item) for item in value)
    return False


def _validate_schema(value: Any, declared_hash: Any, name: str) -> tuple[dict[str, Any], str]:
    if not isinstance(value, Mapping):
        raise MetadataValidationError(f"{name} must be a JSON Schema object")
    schema = json.loads(canonical_json(value))
    if len(canonical_json(schema).encode("utf-8")) > 262_144:
        raise MetadataValidationError(f"{name} exceeds the 256 KiB metadata limit")
    if schema.get("type") != "object":
        raise MetadataValidationError(f"{name} root type must be object")
    if _schema_has_credential_shape(schema):
        raise MetadataValidationError(
            f"{name} contains credential-shaped input; MCP auth belongs to host authority"
        )
    schema_hash = _hash(declared_hash, f"{name}_hash")
    if canonical_hash(schema) != schema_hash:
        raise MetadataConflict(f"{name} hash mismatch")
    return schema, schema_hash


def _availability_state(*, eligible: bool, disabled: bool, model_visible: bool) -> str:
    return "ready" if eligible and not disabled and model_visible else "unavailable"


class OpenClawMetadataImporter:
    """Validate snapshots, persist metadata, and build governed proposals."""

    def __init__(
        self,
        catalog: CapabilityCatalog,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.catalog = catalog
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def _now(self) -> str:
        value = self.clock()
        if not isinstance(value, datetime) or value.tzinfo is None:
            raise MetadataValidationError("metadata importer clock must be timezone-aware")
        return value.astimezone(timezone.utc).isoformat(timespec="microseconds")

    @staticmethod
    def validate_snapshot(snapshot: Mapping[str, Any]) -> dict[str, Any]:
        fields = {
            "schema_version", "id", "created_at", "producer", "scope", "skills",
            "mcp_tools", "content_hash",
        }
        wire = _closed(snapshot, fields, "OpenClawCapabilitySnapshot")
        if wire["schema_version"] != SNAPSHOT_SCHEMA_VERSION:
            raise MetadataValidationError("unsupported metadata snapshot schema_version")
        wire["id"] = _ref(wire["id"], "id")
        wire["created_at"] = _timestamp(wire["created_at"], "created_at")
        producer = _closed(
            wire["producer"],
            {
                "openclaw_version", "source_instance_ref", "exporter_version",
                "catalog_generation", "prior_snapshot_ref", "prior_snapshot_hash",
            },
            "producer",
        )
        producer["openclaw_version"] = _text(
            producer["openclaw_version"], "producer.openclaw_version"
        )
        producer["source_instance_ref"] = _ref(
            producer["source_instance_ref"], "producer.source_instance_ref"
        )
        producer["exporter_version"] = _text(
            producer["exporter_version"], "producer.exporter_version"
        )
        if len(producer["exporter_version"]) > 256:
            raise MetadataValidationError("producer.exporter_version is too long")
        if (
            type(producer["catalog_generation"]) is not int
            or producer["catalog_generation"] < 1
        ):
            raise MetadataValidationError(
                "producer.catalog_generation must be a positive integer"
            )
        prior_ref = producer["prior_snapshot_ref"]
        prior_hash = producer["prior_snapshot_hash"]
        if (prior_ref is None) != (prior_hash is None):
            raise MetadataValidationError(
                "producer prior snapshot ref/hash must be paired"
            )
        if prior_ref is not None:
            producer["prior_snapshot_ref"] = _ref(
                prior_ref, "producer.prior_snapshot_ref"
            )
            producer["prior_snapshot_hash"] = _hash(
                prior_hash, "producer.prior_snapshot_hash"
            )
        if producer["catalog_generation"] == 1 and prior_ref is not None:
            raise MetadataValidationError("first generation cannot declare a prior snapshot")
        if producer["catalog_generation"] > 1 and prior_ref is None:
            raise MetadataValidationError("later generations require a prior snapshot")
        wire["producer"] = producer
        scope = _closed(
            wire["scope"], {"skills_complete", "mcp_servers_complete"}, "scope"
        )
        scope["skills_complete"] = _boolean(scope["skills_complete"], "skills_complete")
        scope["mcp_servers_complete"] = _unique_names(
            scope["mcp_servers_complete"], "mcp_servers_complete"
        )
        wire["scope"] = scope

        if not isinstance(wire["skills"], list) or not isinstance(wire["mcp_tools"], list):
            raise MetadataValidationError("skills and mcp_tools must be arrays")
        if len(wire["skills"]) > 10_000 or len(wire["mcp_tools"]) > 10_000:
            raise MetadataValidationError("metadata snapshot inventory is too large")
        skill_fields = {
            "name", "label", "description", "source", "source_version", "eligible",
            "disabled", "model_visible", "user_invocable", "command_visible",
            "instruction_ref", "instruction_hash", "metadata_hash",
        }
        skills: list[dict[str, Any]] = []
        seen_skill_names: set[str] = set()
        for index, raw in enumerate(wire["skills"]):
            item = _closed(raw, skill_fields, f"skills[{index}]")
            item["name"] = _canonical_name(item["name"], f"skills[{index}].name")
            if item["name"] in seen_skill_names:
                raise MetadataValidationError("skill names must be unique")
            seen_skill_names.add(item["name"])
            item["label"] = _text(item["label"], f"skills[{index}].label")
            item["description"] = _text(
                item["description"], f"skills[{index}].description"
            )
            if len(item["label"]) > 160 or len(item["description"]) > 2_000:
                raise MetadataValidationError("skill label/description exceeds compact limits")
            if item["source"] not in _SKILL_SOURCES:
                raise MetadataValidationError("skill source is not supported")
            item["source_version"] = _text(
                item["source_version"], f"skills[{index}].source_version"
            )
            if len(item["source_version"]) > 256:
                raise MetadataValidationError("skill source_version is too long")
            for name in (
                "eligible", "disabled", "model_visible", "user_invocable", "command_visible"
            ):
                item[name] = _boolean(item[name], f"skills[{index}].{name}")
            item["instruction_ref"] = _ref(
                item["instruction_ref"], f"skills[{index}].instruction_ref"
            )
            item["instruction_hash"] = _hash(
                item["instruction_hash"], f"skills[{index}].instruction_hash"
            )
            skills.append(_verify_content_hash(item, f"skills[{index}]"))
        wire["skills"] = skills

        tool_fields = {
            "server_name", "safe_server_name", "tool_name", "title", "description",
            "source_version", "execution_mode", "input_schema_ref", "input_schema",
            "input_schema_hash", "output_schema_ref", "output_schema",
            "output_schema_hash", "metadata_hash",
        }
        tools: list[dict[str, Any]] = []
        seen_tools: set[tuple[str, str]] = set()
        seen_capability_ids: set[str] = set()
        for index, raw in enumerate(wire["mcp_tools"]):
            item = _closed(raw, tool_fields, f"mcp_tools[{index}]")
            item["server_name"] = _text(
                item["server_name"], f"mcp_tools[{index}].server_name"
            )
            item["safe_server_name"] = _canonical_name(
                item["safe_server_name"], f"mcp_tools[{index}].safe_server_name"
            )
            item["tool_name"] = _canonical_name(
                item["tool_name"], f"mcp_tools[{index}].tool_name"
            )
            identity = (item["safe_server_name"], item["tool_name"])
            if identity in seen_tools:
                raise MetadataValidationError("MCP tool identities must be unique")
            seen_tools.add(identity)
            item["title"] = _text(
                item["title"], f"mcp_tools[{index}].title", nullable=True
            )
            item["description"] = _text(
                item["description"], f"mcp_tools[{index}].description", nullable=True
            )
            if (
                item["title"] is not None and len(item["title"]) > 160
            ) or (
                item["description"] is not None and len(item["description"]) > 2_000
            ):
                raise MetadataValidationError("MCP title/description exceeds compact limits")
            item["source_version"] = _text(
                item["source_version"], f"mcp_tools[{index}].source_version"
            )
            if len(item["source_version"]) > 256:
                raise MetadataValidationError("MCP source_version is too long")
            if item["execution_mode"] not in {"sequential", "parallel"}:
                raise MetadataValidationError("MCP execution_mode is invalid")
            item["input_schema_ref"] = _ref(
                item["input_schema_ref"], f"mcp_tools[{index}].input_schema_ref"
            )
            item["output_schema_ref"] = _ref(
                item["output_schema_ref"], f"mcp_tools[{index}].output_schema_ref"
            )
            item["input_schema"], item["input_schema_hash"] = _validate_schema(
                item["input_schema"], item["input_schema_hash"],
                f"mcp_tools[{index}].input_schema",
            )
            item["output_schema"], item["output_schema_hash"] = _validate_schema(
                item["output_schema"], item["output_schema_hash"],
                f"mcp_tools[{index}].output_schema",
            )
            capability_id = (
                f"capability:mcp:{item['safe_server_name']}:{item['tool_name']}"
            )
            if capability_id in seen_capability_ids:
                raise MetadataValidationError("normalized MCP capability IDs must be unique")
            seen_capability_ids.add(capability_id)
            tools.append(_verify_content_hash(item, f"mcp_tools[{index}]"))
        wire["mcp_tools"] = tools

        if len(canonical_json(wire).encode("utf-8")) > 16 * 1024 * 1024:
            raise MetadataValidationError("metadata snapshot exceeds 16 MiB")

        declared = _hash(wire.pop("content_hash"), "content_hash")
        if canonical_hash(wire) != declared:
            raise MetadataConflict("metadata snapshot content_hash mismatch")
        wire["content_hash"] = declared
        return wire

    @staticmethod
    def _skill_metadata(item: Mapping[str, Any], created_at: str) -> dict[str, Any]:
        capability_id = f"capability:skill:openclaw:{item['name']}"
        contract = {
            "mode": "instruction_load",
            "input_schema_ref": None,
            "output_schema_ref": None,
            "instruction_ref": item["instruction_ref"],
            "adapter_ref": "adapter:openclaw:skill-loader:0.1",
        }
        return {
            "schema_version": CATALOG_SCHEMA_VERSION,
            "capability_id": capability_id,
            "created_at": created_at,
            "kind": "instruction",
            "name": item["name"],
            "label": item["label"],
            "summary": item["description"],
            "aliases": [item["name"]],
            "tags": ["openclaw", "skill", item["source"]],
            "intent_examples": [],
            "availability_state": _availability_state(
                eligible=item["eligible"], disabled=item["disabled"],
                model_visible=item["model_visible"],
            ),
            "source": {
                "type": "skill",
                "namespace": "openclaw",
                "source_ref": item["instruction_ref"],
                "source_version": item["source_version"],
            },
            "contract": contract,
            "source_hash": item["instruction_hash"],
            "schema_hash": canonical_hash(
                {
                    "contract": contract,
                    "instruction_hash": item["instruction_hash"],
                    "upstream_metadata_hash": item["metadata_hash"],
                }
            ),
            "upstream_metadata_hash": item["metadata_hash"],
        }

    @staticmethod
    def _mcp_metadata(item: Mapping[str, Any], created_at: str) -> dict[str, Any]:
        capability_id = (
            f"capability:mcp:{item['safe_server_name']}:{item['tool_name']}"
        )
        return {
            "schema_version": CATALOG_SCHEMA_VERSION,
            "capability_id": capability_id,
            "created_at": created_at,
            "kind": "tool",
            "name": f"{item['safe_server_name']}.{item['tool_name']}",
            "label": item["title"] or item["tool_name"],
            "summary": item["description"] or f"MCP tool {item['tool_name']}",
            "aliases": [item["tool_name"]],
            "tags": ["openclaw", "mcp", item["safe_server_name"]],
            "intent_examples": [],
            "availability_state": "ready",
            "source": {
                "type": "mcp",
                "namespace": item["safe_server_name"],
                "source_ref": (
                    f"openclaw-mcp:{item['safe_server_name']}/{item['tool_name']}"
                ),
                "source_version": item["source_version"],
            },
            "contract": {
                "mode": "typed_call",
                "input_schema_ref": item["input_schema_ref"],
                "output_schema_ref": item["output_schema_ref"],
                "instruction_ref": None,
                "adapter_ref": "adapter:openclaw:mcp-call:0.1",
            },
            "source_hash": item["metadata_hash"],
            "schema_hash": canonical_hash(
                {
                    "input_schema_ref": item["input_schema_ref"],
                    "input_schema_hash": item["input_schema_hash"],
                    "output_schema_ref": item["output_schema_ref"],
                    "output_schema_hash": item["output_schema_hash"],
                }
            ),
            "execution_mode": item["execution_mode"],
            "upstream_metadata_hash": item["metadata_hash"],
        }

    def import_snapshot(self, snapshot: Mapping[str, Any]) -> dict[str, Any]:
        wire = self.validate_snapshot(snapshot)
        created_at = wire["created_at"]
        entries: list[dict[str, Any]] = []
        schemas: list[dict[str, Any]] = []
        for skill in wire["skills"]:
            metadata = self._skill_metadata(skill, created_at)
            metadata_hash = canonical_hash(metadata)
            entries.append(
                {
                    "metadata_ref": f"external-metadata:{metadata_hash}",
                    "capability_id": metadata["capability_id"],
                    "source_type": "skill",
                    "source_scope": "skills",
                    "source_key": skill["name"],
                    "source_version": skill["source_version"],
                    "source_hash": metadata["source_hash"],
                    "schema_hash": metadata["schema_hash"],
                    "metadata_hash": metadata_hash,
                    "metadata": metadata,
                    "created_at": created_at,
                }
            )
        for tool in wire["mcp_tools"]:
            metadata = self._mcp_metadata(tool, created_at)
            metadata_hash = canonical_hash(metadata)
            entries.append(
                {
                    "metadata_ref": f"external-metadata:{metadata_hash}",
                    "capability_id": metadata["capability_id"],
                    "source_type": "mcp",
                    "source_scope": f"mcp:{tool['safe_server_name']}",
                    "source_key": tool["tool_name"],
                    "source_version": tool["source_version"],
                    "source_hash": metadata["source_hash"],
                    "schema_hash": metadata["schema_hash"],
                    "metadata_hash": metadata_hash,
                    "metadata": metadata,
                    "created_at": created_at,
                }
            )
            for prefix in ("input", "output"):
                schemas.append(
                    {
                        "schema_ref": tool[f"{prefix}_schema_ref"],
                        "schema_hash": tool[f"{prefix}_schema_hash"],
                        "schema": tool[f"{prefix}_schema"],
                        "created_at": created_at,
                    }
                )
        envelope_hash = canonical_hash(wire)
        return self.catalog.apply_external_metadata_snapshot(
            snapshot_ref=wire["id"],
            snapshot_hash=envelope_hash,
            producer_version=wire["producer"]["openclaw_version"],
            source_instance_ref=wire["producer"]["source_instance_ref"],
            exporter_version=wire["producer"]["exporter_version"],
            catalog_generation=wire["producer"]["catalog_generation"],
            prior_snapshot_ref=wire["producer"]["prior_snapshot_ref"],
            prior_snapshot_hash=wire["producer"]["prior_snapshot_hash"],
            snapshot_json=canonical_json(wire),
            snapshot_created_at=created_at,
            entries=entries,
            schemas=schemas,
            skills_complete=wire["scope"]["skills_complete"],
            mcp_servers_complete=wire["scope"]["mcp_servers_complete"],
        )

    def build_descriptor_spec(
        self,
        capability_id: str,
        *,
        version: int,
        permissions: Mapping[str, Any],
        policy_ref: str,
        visibility_scopes: Sequence[str],
        state: str | None = None,
        valid_until: str | None = None,
        created_at: str | None = None,
    ) -> dict[str, Any]:
        metadata = self.catalog.get_external_metadata(capability_id)
        requested_state = state or metadata["availability_state"]
        if requested_state not in {"ready", "auth_required", "unavailable", "quarantined"}:
            raise MetadataValidationError("descriptor state is invalid")
        if metadata["availability_state"] != "ready" and requested_state in {
            "ready", "auth_required"
        }:
            raise MetadataValidationError("unavailable upstream metadata cannot be made ready")
        parsed_permissions = CapabilityPermissions.from_dict(permissions)
        scopes = list(visibility_scopes)
        if not scopes or not all(isinstance(item, str) and item for item in scopes):
            raise MetadataValidationError("visibility_scopes must contain non-empty strings")
        if len(scopes) != len(set(scopes)):
            raise MetadataValidationError("visibility_scopes must be unique")
        if type(version) is not int or version < 1:
            raise MetadataValidationError("descriptor version must be positive")
        if valid_until is not None:
            valid_until = _timestamp(valid_until, "valid_until")
        proposal = {
            "schema_version": CATALOG_SCHEMA_VERSION,
            "id": metadata["capability_id"],
            "version": version,
            "created_at": created_at or self._now(),
            "kind": metadata["kind"],
            "name": metadata["name"],
            "label": metadata["label"],
            "summary": metadata["summary"],
            "aliases": copy.deepcopy(metadata["aliases"]),
            "tags": copy.deepcopy(metadata["tags"]),
            "intent_examples": copy.deepcopy(metadata["intent_examples"]),
            "source": copy.deepcopy(metadata["source"]),
            "contract": copy.deepcopy(metadata["contract"]),
            "permissions": parsed_permissions.to_dict(),
            "eligibility": {
                "state": requested_state,
                "visibility_scopes": scopes,
                "policy_ref": policy_ref,
                "valid_until": valid_until,
            },
            "source_hash": metadata["source_hash"],
            "schema_hash": metadata["schema_hash"],
        }
        try:
            canonical_json(proposal)
        except (TypeError, ValueError) as exc:
            raise CatalogValidationError("descriptor proposal is not finite JSON") from exc
        return proposal


__all__ = [
    "MetadataConflict",
    "MetadataImportError",
    "MetadataValidationError",
    "OpenClawMetadataImporter",
    "SNAPSHOT_SCHEMA_VERSION",
]
