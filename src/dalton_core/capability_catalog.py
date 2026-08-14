"""Versioned, prompt-small capability discovery and lease preparation.

This is a control-plane catalog.  It stores descriptor references, never
credential values, schema bodies, skill instructions, or executable code.
Preparing a capability only issues an immutable short lease; it never invokes
the capability.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .contracts import WorkOrder


SCHEMA_VERSION = "0.1"
_SCHEMA_PATH = Path(__file__).with_name("capability_catalog_schema.sql")
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_CAPABILITY_RE = re.compile(
    r"^capability:(dalton|skill|tool|mcp|plugin):"
    r"[a-z0-9][a-z0-9._-]*:[a-z0-9][a-z0-9._-]*$"
)
_SLOT_RE = re.compile(r"^credential[-_]slot:[A-Za-z0-9][A-Za-z0-9._:/-]*$")
_REF_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+._-]*:[^\s]+$")
_WORD_RE = re.compile(r"[a-z0-9]+")
_KINDS = frozenset({"instruction", "tool", "connector", "transform", "runtime"})
_SOURCE_TYPES = frozenset({"dalton", "skill", "tool", "mcp", "plugin"})
_MODES = frozenset({"instruction_load", "typed_call", "process"})
_STATES = frozenset({"ready", "auth_required", "unavailable", "quarantined"})


class CatalogError(Exception):
    """Base class for catalog failures."""


class CatalogValidationError(CatalogError, ValueError):
    pass


class CatalogConflict(CatalogError):
    pass


class CapabilityNotFound(CatalogError):
    pass


class CapabilityNotEligible(CatalogError):
    pass


class AuthorizationRequired(CapabilityNotEligible):
    pass


class StaleCatalog(CatalogError):
    pass


class PermissionDenied(CatalogError):
    pass


class ApprovalRequired(PermissionDenied):
    """The capability is not bound to a current human approval authority."""


class GovernancePolicyRejected(PermissionDenied):
    """The current governance policy does not authorize the requested lease."""


class LeaseNotUsable(PermissionDenied):
    """A historical lease failed its mandatory use-time checks."""


class ExternalSourceRegistrationRequired(PermissionDenied):
    """An external metadata source lacks an active operator registration."""


class ExternalSnapshotRejected(StaleCatalog):
    """A well-formed external snapshot failed the monotonic source chain."""

    def __init__(self, outcome: str, message: str) -> None:
        super().__init__(message)
        self.outcome = outcome


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def canonical_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _descriptor_matches_external(
    descriptor: Mapping[str, Any], metadata: Mapping[str, Any]
) -> bool:
    exact_fields = (
        "kind", "name", "label", "summary", "aliases", "tags",
        "intent_examples", "source", "contract", "source_hash", "schema_hash",
    )
    return all(descriptor.get(field) == metadata.get(field) for field in exact_fields)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _timestamp(value: datetime) -> str:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise CatalogValidationError("catalog clock must return a timezone-aware datetime")
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _parse_time(value: str, name: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise CatalogValidationError(f"{name} must be an RFC3339 timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CatalogValidationError(f"{name} must be an RFC3339 timestamp") from exc
    if parsed.tzinfo is None:
        raise CatalogValidationError(f"{name} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _closed(data: Any, allowed: set[str], required: set[str], name: str) -> Mapping[str, Any]:
    if not isinstance(data, Mapping):
        raise CatalogValidationError(f"{name} must be an object")
    unknown = set(data) - allowed
    missing = required - set(data)
    if unknown:
        raise CatalogValidationError(f"{name} unknown field(s): {sorted(unknown)}")
    if missing:
        raise CatalogValidationError(f"{name} missing field(s): {sorted(missing)}")
    return data


def _string(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise CatalogValidationError(f"{name} must be a non-empty string")
    return value


def _hash(value: Any, name: str) -> str:
    value = _string(value, name)
    if not _HASH_RE.fullmatch(value):
        raise CatalogValidationError(f"{name} must be 64 lowercase SHA-256 hex characters")
    return value


def _ref(value: Any, name: str, *, nullable: bool = False) -> str | None:
    if value is None and nullable:
        return None
    value = _string(value, name)
    if not _REF_RE.fullmatch(value):
        raise CatalogValidationError(f"{name} must be an opaque namespaced reference")
    return value


def _unique_strings(value: Any, name: str, *, nonempty: bool = False) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)) or not all(isinstance(x, str) and x for x in value):
        raise CatalogValidationError(f"{name} must be an array of non-empty strings")
    result = tuple(value)
    if nonempty and not result:
        raise CatalogValidationError(f"{name} must not be empty")
    if len(set(result)) != len(result):
        raise CatalogValidationError(f"{name} must contain unique values")
    return result


@dataclass(frozen=True, slots=True)
class CapabilitySource:
    type: str
    namespace: str
    source_ref: str
    source_version: str

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "CapabilitySource":
        obj = _closed(data, {"type", "namespace", "source_ref", "source_version"},
                      {"type", "namespace", "source_ref", "source_version"}, "source")
        source_type = _string(obj["type"], "source.type")
        if source_type not in _SOURCE_TYPES:
            raise CatalogValidationError("source.type is not supported")
        namespace = _string(obj["namespace"], "source.namespace")
        if not re.fullmatch(r"[a-z0-9][a-z0-9._-]*", namespace):
            raise CatalogValidationError("source.namespace is not canonical")
        return cls(source_type, namespace, _ref(obj["source_ref"], "source.source_ref"),
                   _string(obj["source_version"], "source.source_version"))  # type: ignore[arg-type]

    def to_dict(self) -> dict[str, Any]:
        return {"type": self.type, "namespace": self.namespace,
                "source_ref": self.source_ref, "source_version": self.source_version}


@dataclass(frozen=True, slots=True)
class CapabilityContract:
    mode: str
    input_schema_ref: str | None
    output_schema_ref: str | None
    instruction_ref: str | None
    adapter_ref: str

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "CapabilityContract":
        keys = {"mode", "input_schema_ref", "output_schema_ref", "instruction_ref", "adapter_ref"}
        obj = _closed(data, keys, keys, "contract")
        mode = _string(obj["mode"], "contract.mode")
        if mode not in _MODES:
            raise CatalogValidationError("contract.mode is not supported")
        contract = cls(mode, _ref(obj["input_schema_ref"], "input_schema_ref", nullable=True),
                       _ref(obj["output_schema_ref"], "output_schema_ref", nullable=True),
                       _ref(obj["instruction_ref"], "instruction_ref", nullable=True),
                       _ref(obj["adapter_ref"], "adapter_ref"))  # type: ignore[arg-type]
        if mode == "instruction_load" and contract.instruction_ref is None:
            raise CatalogValidationError("instruction_load requires instruction_ref")
        if mode != "instruction_load" and (contract.input_schema_ref is None or contract.output_schema_ref is None):
            raise CatalogValidationError("typed/process capabilities require input and output schema refs")
        return contract

    def to_dict(self) -> dict[str, Any]:
        return {"mode": self.mode, "input_schema_ref": self.input_schema_ref,
                "output_schema_ref": self.output_schema_ref, "instruction_ref": self.instruction_ref,
                "adapter_ref": self.adapter_ref}


@dataclass(frozen=True, slots=True)
class CapabilityPermissions:
    risk_class: str
    network: bool
    filesystem_read: tuple[str, ...]
    filesystem_write: tuple[str, ...]
    credential_slot_refs: tuple[str, ...]
    core_db: bool
    side_effects: tuple[str, ...]

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "CapabilityPermissions":
        keys = {"risk_class", "network", "filesystem_read", "filesystem_write",
                "credential_slot_refs", "core_db", "side_effects"}
        obj = _closed(data, keys, keys, "permissions")
        risk = _string(obj["risk_class"], "permissions.risk_class")
        if risk not in {"none", "low", "medium", "high"}:
            raise CatalogValidationError("permissions.risk_class is invalid")
        if type(obj["network"]) is not bool or type(obj["core_db"]) is not bool:
            raise CatalogValidationError("permissions network/core_db must be boolean")
        slots = _unique_strings(obj["credential_slot_refs"], "credential_slot_refs")
        if any(not _SLOT_RE.fullmatch(slot) for slot in slots):
            raise CatalogValidationError("credential_slot_refs must contain logical slot references only")
        return cls(risk, obj["network"],
                   _unique_strings(obj["filesystem_read"], "filesystem_read"),
                   _unique_strings(obj["filesystem_write"], "filesystem_write"), slots,
                   obj["core_db"], _unique_strings(obj["side_effects"], "side_effects"))

    def to_dict(self) -> dict[str, Any]:
        return {"risk_class": self.risk_class, "network": self.network,
                "filesystem_read": list(self.filesystem_read),
                "filesystem_write": list(self.filesystem_write),
                "credential_slot_refs": list(self.credential_slot_refs),
                "core_db": self.core_db, "side_effects": list(self.side_effects)}


_RISK_LEVEL = {"none": 0, "low": 1, "medium": 2, "high": 3}


def _permissions_subset(
    requested: CapabilityPermissions, allowed: CapabilityPermissions
) -> bool:
    """Return whether every requested grant is explicitly allowed.

    Paths and credential slots are opaque exact values.  This intentionally
    has no wildcard expansion: a policy which wants to allow ``/`` must say so
    explicitly, and a narrower grant cannot be inferred from a broader-looking
    string by this control-plane component.
    """

    return (
        _RISK_LEVEL[requested.risk_class] <= _RISK_LEVEL[allowed.risk_class]
        and (not requested.network or allowed.network)
        and (not requested.core_db or allowed.core_db)
        and set(requested.filesystem_read).issubset(allowed.filesystem_read)
        and set(requested.filesystem_write).issubset(allowed.filesystem_write)
        and set(requested.credential_slot_refs).issubset(
            allowed.credential_slot_refs
        )
        and set(requested.side_effects).issubset(allowed.side_effects)
    )


@dataclass(frozen=True, slots=True)
class CapabilityApproval:
    schema_version: str
    approval_ref: str
    capability_id: str
    registry_revision_ref: str
    artifact_ref: str
    artifact_hash: str
    schema_hash: str
    fixture_manifest_hash: str
    attestation_ref: str
    attestation_hash: str
    decision_ref: str
    decision: str
    approved_by: str
    approved_permissions: CapabilityPermissions
    active: bool
    effective_from: str
    effective_until: str | None
    receipt_hash: str

    @classmethod
    def from_dict(
        cls, data: Mapping[str, Any], *, verify_hash: bool = True
    ) -> "CapabilityApproval":
        keys = {
            "schema_version", "approval_ref", "capability_id",
            "registry_revision_ref", "artifact_ref", "artifact_hash", "schema_hash",
            "fixture_manifest_hash", "attestation_ref", "attestation_hash",
            "decision_ref", "decision", "approved_by", "approved_permissions", "active",
            "effective_from", "effective_until", "receipt_hash",
        }
        obj = _closed(data, keys, keys, "approval receipt")
        if obj["schema_version"] != SCHEMA_VERSION:
            raise ApprovalRequired("approval receipt schema_version is unsupported")
        if obj["decision"] != "approve" or obj["active"] is not True:
            raise ApprovalRequired("capability revision is not actively approved")
        approved_by = _ref(obj["approved_by"], "approval.approved_by")
        if not str(approved_by).startswith("human:") or not str(approved_by)[6:]:
            raise ApprovalRequired("approval must be attributed to a human actor")
        effective_from = _parse_time(obj["effective_from"], "approval.effective_from")
        effective_until = obj["effective_until"]
        if effective_until is not None:
            until = _parse_time(effective_until, "approval.effective_until")
            if until <= effective_from:
                raise ApprovalRequired("approval interval is invalid")
        approval = cls(
            SCHEMA_VERSION,
            _ref(obj["approval_ref"], "approval.approval_ref"),
            _string(obj["capability_id"], "approval.capability_id"),
            _ref(obj["registry_revision_ref"], "approval.registry_revision_ref"),
            _ref(obj["artifact_ref"], "approval.artifact_ref"),
            _hash(obj["artifact_hash"], "approval.artifact_hash"),
            _hash(obj["schema_hash"], "approval.schema_hash"),
            _hash(obj["fixture_manifest_hash"], "approval.fixture_manifest_hash"),
            _ref(obj["attestation_ref"], "approval.attestation_ref"),
            _hash(obj["attestation_hash"], "approval.attestation_hash"),
            _ref(obj["decision_ref"], "approval.decision_ref"),
            "approve",
            approved_by,
            CapabilityPermissions.from_dict(obj["approved_permissions"]),
            True,
            obj["effective_from"],
            effective_until,
            _hash(obj["receipt_hash"], "approval.receipt_hash"),
        )  # type: ignore[arg-type]
        if verify_hash and canonical_hash(
            approval.to_dict(include_hash=False)
        ) != approval.receipt_hash:
            raise ApprovalRequired("approval receipt hash mismatch")
        return approval

    def to_dict(self, *, include_hash: bool = True) -> dict[str, Any]:
        result = {
            "schema_version": self.schema_version,
            "approval_ref": self.approval_ref,
            "capability_id": self.capability_id,
            "registry_revision_ref": self.registry_revision_ref,
            "artifact_ref": self.artifact_ref,
            "artifact_hash": self.artifact_hash,
            "schema_hash": self.schema_hash,
            "fixture_manifest_hash": self.fixture_manifest_hash,
            "attestation_ref": self.attestation_ref,
            "attestation_hash": self.attestation_hash,
            "decision_ref": self.decision_ref,
            "decision": self.decision,
            "approved_by": self.approved_by,
            "approved_permissions": self.approved_permissions.to_dict(),
            "active": self.active,
            "effective_from": self.effective_from,
            "effective_until": self.effective_until,
        }
        if include_hash:
            result["receipt_hash"] = self.receipt_hash
        return result

    def is_effective(self, at: datetime) -> bool:
        if _parse_time(self.effective_from, "approval.effective_from") > at:
            return False
        return self.effective_until is None or _parse_time(
            self.effective_until, "approval.effective_until"
        ) > at


@dataclass(frozen=True, slots=True)
class ResolvedGovernancePolicy:
    schema_version: str
    policy_ref: str
    effective_from: str
    effective_until: str | None
    allowed_principal_refs: tuple[str, ...]
    allowed_permissions: CapabilityPermissions
    max_lease_seconds: int
    content_hash: str

    @classmethod
    def from_dict(
        cls, data: Mapping[str, Any], *, verify_hash: bool = True
    ) -> "ResolvedGovernancePolicy":
        keys = {
            "schema_version", "policy_ref", "effective_from", "effective_until",
            "allowed_principal_refs", "allowed_permissions", "max_lease_seconds",
            "content_hash",
        }
        obj = _closed(data, keys, keys, "resolved governance policy")
        if obj["schema_version"] != SCHEMA_VERSION:
            raise GovernancePolicyRejected("policy schema_version is unsupported")
        start = _parse_time(obj["effective_from"], "policy.effective_from")
        end = obj["effective_until"]
        if end is not None and _parse_time(end, "policy.effective_until") <= start:
            raise GovernancePolicyRejected("policy interval is invalid")
        principals = _unique_strings(
            obj["allowed_principal_refs"], "policy.allowed_principal_refs", nonempty=True
        )
        for principal in principals:
            _ref(principal, "policy.allowed_principal_refs[]")
        max_lease = obj["max_lease_seconds"]
        if type(max_lease) is not int or max_lease < 1:
            raise GovernancePolicyRejected("policy max_lease_seconds is invalid")
        policy = cls(
            SCHEMA_VERSION,
            _ref(obj["policy_ref"], "policy.policy_ref"),
            obj["effective_from"],
            end,
            principals,
            CapabilityPermissions.from_dict(obj["allowed_permissions"]),
            max_lease,
            _hash(obj["content_hash"], "policy.content_hash"),
        )  # type: ignore[arg-type]
        if verify_hash and canonical_hash(
            policy.to_dict(include_hash=False)
        ) != policy.content_hash:
            raise GovernancePolicyRejected("policy content_hash mismatch")
        return policy

    def to_dict(self, *, include_hash: bool = True) -> dict[str, Any]:
        result = {
            "schema_version": self.schema_version,
            "policy_ref": self.policy_ref,
            "effective_from": self.effective_from,
            "effective_until": self.effective_until,
            "allowed_principal_refs": list(self.allowed_principal_refs),
            "allowed_permissions": self.allowed_permissions.to_dict(),
            "max_lease_seconds": self.max_lease_seconds,
        }
        if include_hash:
            result["content_hash"] = self.content_hash
        return result

    def is_effective(self, at: datetime) -> bool:
        if _parse_time(self.effective_from, "policy.effective_from") > at:
            return False
        return self.effective_until is None or _parse_time(
            self.effective_until, "policy.effective_until"
        ) > at


@dataclass(frozen=True, slots=True)
class CapabilityEligibility:
    state: str
    visibility_scopes: tuple[str, ...]
    policy_ref: str
    valid_until: str | None

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "CapabilityEligibility":
        keys = {"state", "visibility_scopes", "policy_ref", "valid_until"}
        obj = _closed(data, keys, keys, "eligibility")
        state = _string(obj["state"], "eligibility.state")
        if state not in _STATES:
            raise CatalogValidationError("eligibility.state is invalid")
        scopes = _unique_strings(obj["visibility_scopes"], "visibility_scopes", nonempty=True)
        policy_ref = _ref(obj["policy_ref"], "eligibility.policy_ref")
        valid_until = obj["valid_until"]
        if valid_until is not None:
            _parse_time(valid_until, "eligibility.valid_until")
        return cls(state, scopes, policy_ref, valid_until)  # type: ignore[arg-type]

    def to_dict(self) -> dict[str, Any]:
        return {"state": self.state, "visibility_scopes": list(self.visibility_scopes),
                "policy_ref": self.policy_ref, "valid_until": self.valid_until}


@dataclass(frozen=True, slots=True)
class CapabilityDescriptor:
    schema_version: str
    id: str
    revision_ref: str
    version: int
    created_at: str
    catalog_epoch: int
    kind: str
    name: str
    label: str
    summary: str
    aliases: tuple[str, ...]
    tags: tuple[str, ...]
    intent_examples: tuple[str, ...]
    source: CapabilitySource
    contract: CapabilityContract
    permissions: CapabilityPermissions
    eligibility: CapabilityEligibility
    approval: CapabilityApproval
    source_hash: str
    schema_hash: str
    content_hash: str

    @classmethod
    def from_dict(cls, data: Mapping[str, Any], *, verify_hash: bool = True) -> "CapabilityDescriptor":
        keys = {"schema_version", "id", "revision_ref", "version", "created_at", "catalog_epoch",
                "kind", "name", "label", "summary", "aliases", "tags", "intent_examples",
                "source", "contract", "permissions", "eligibility", "approval",
                "source_hash", "schema_hash", "content_hash"}
        obj = _closed(data, keys, keys, "CapabilityDescriptor")
        if obj["schema_version"] != SCHEMA_VERSION:
            raise CatalogValidationError("unsupported descriptor schema_version")
        capability_id = _string(obj["id"], "id")
        match = _CAPABILITY_RE.fullmatch(capability_id)
        if not match:
            raise CatalogValidationError("id must be a canonical namespaced capability ID")
        version = obj["version"]
        epoch = obj["catalog_epoch"]
        if type(version) is not int or version < 1 or type(epoch) is not int or epoch < 1:
            raise CatalogValidationError("version/catalog_epoch must be positive integers")
        revision = f"{capability_id}@v{version}"
        if obj["revision_ref"] != revision:
            raise CatalogValidationError("revision_ref does not match id and version")
        _parse_time(obj["created_at"], "created_at")
        kind = _string(obj["kind"], "kind")
        if kind not in _KINDS:
            raise CatalogValidationError("kind is invalid")
        source = CapabilitySource.from_dict(obj["source"])
        parts = capability_id.split(":")
        if parts[1] != source.type or parts[2] != source.namespace:
            raise CatalogValidationError("id namespace must match source")
        descriptor = cls(SCHEMA_VERSION, capability_id, revision, version, obj["created_at"], epoch,
                         kind, _string(obj["name"], "name"), _string(obj["label"], "label"),
                         _string(obj["summary"], "summary"), _unique_strings(obj["aliases"], "aliases"),
                         _unique_strings(obj["tags"], "tags"),
                         _unique_strings(obj["intent_examples"], "intent_examples"), source,
                         CapabilityContract.from_dict(obj["contract"]),
                         CapabilityPermissions.from_dict(obj["permissions"]),
                         CapabilityEligibility.from_dict(obj["eligibility"]),
                         CapabilityApproval.from_dict(obj["approval"]),
                         _hash(obj["source_hash"], "source_hash"), _hash(obj["schema_hash"], "schema_hash"),
                         _hash(obj["content_hash"], "content_hash"))
        if verify_hash and canonical_hash(descriptor.to_dict(include_hash=False)) != descriptor.content_hash:
            raise CatalogValidationError("descriptor content_hash mismatch")
        return descriptor

    def to_dict(self, *, include_hash: bool = True) -> dict[str, Any]:
        result = {"schema_version": self.schema_version, "id": self.id,
                  "revision_ref": self.revision_ref, "version": self.version,
                  "created_at": self.created_at, "catalog_epoch": self.catalog_epoch,
                  "kind": self.kind, "name": self.name, "label": self.label,
                  "summary": self.summary, "aliases": list(self.aliases), "tags": list(self.tags),
                  "intent_examples": list(self.intent_examples), "source": self.source.to_dict(),
                  "contract": self.contract.to_dict(), "permissions": self.permissions.to_dict(),
                  "eligibility": self.eligibility.to_dict(), "approval": self.approval.to_dict(),
                  "source_hash": self.source_hash,
                  "schema_hash": self.schema_hash}
        if include_hash:
            result["content_hash"] = self.content_hash
        return result


@dataclass(frozen=True, slots=True)
class CapabilityHit:
    id: str
    revision_ref: str
    version: int
    catalog_epoch: int
    name: str
    label: str
    summary: str
    kind: str
    state: str
    score: int
    descriptor_hash: str

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "revision_ref": self.revision_ref, "version": self.version,
                "catalog_epoch": self.catalog_epoch, "name": self.name, "label": self.label,
                "summary": self.summary, "kind": self.kind, "state": self.state,
                "score": self.score, "descriptor_hash": self.descriptor_hash}


@dataclass(frozen=True, slots=True)
class CapabilityLease:
    schema_version: str
    id: str
    created_at: str
    expires_at: str
    work_order_ref: str
    work_order_hash: str
    capability_id: str
    revision_ref: str
    descriptor_hash: str
    source_hash: str
    schema_hash: str
    catalog_epoch: int
    policy_ref: str
    policy_hash: str
    principal_ref: str
    permissions: CapabilityPermissions
    allowed_side_effects: tuple[str, ...]
    credential_slot_refs: tuple[str, ...]
    content_hash: str

    @classmethod
    def from_dict(cls, data: Mapping[str, Any], *, verify_hash: bool = True) -> "CapabilityLease":
        keys = {"schema_version", "id", "created_at", "expires_at", "work_order_ref", "work_order_hash",
                "capability_id", "revision_ref", "descriptor_hash", "source_hash", "schema_hash",
                "catalog_epoch", "policy_ref", "policy_hash", "principal_ref", "permissions",
                "allowed_side_effects", "credential_slot_refs", "content_hash"}
        obj = _closed(data, keys, keys, "CapabilityLease")
        if obj["schema_version"] != SCHEMA_VERSION:
            raise CatalogValidationError("unsupported lease schema_version")
        created = _parse_time(obj["created_at"], "created_at")
        expires = _parse_time(obj["expires_at"], "expires_at")
        if expires <= created:
            raise CatalogValidationError("lease expiry must follow creation")
        epoch = obj["catalog_epoch"]
        if type(epoch) is not int or epoch < 1:
            raise CatalogValidationError("catalog_epoch must be positive")
        permissions = CapabilityPermissions.from_dict(obj["permissions"])
        slots = _unique_strings(obj["credential_slot_refs"], "credential_slot_refs")
        if slots != permissions.credential_slot_refs:
            raise CatalogValidationError("lease credential slots must match permissions")
        lease = cls(SCHEMA_VERSION, _string(obj["id"], "id"), obj["created_at"], obj["expires_at"],
                    _string(obj["work_order_ref"], "work_order_ref"), _hash(obj["work_order_hash"], "work_order_hash"),
                    _string(obj["capability_id"], "capability_id"), _string(obj["revision_ref"], "revision_ref"),
                    _hash(obj["descriptor_hash"], "descriptor_hash"), _hash(obj["source_hash"], "source_hash"),
                    _hash(obj["schema_hash"], "schema_hash"), epoch, _ref(obj["policy_ref"], "policy_ref"),
                    _hash(obj["policy_hash"], "policy_hash"), _ref(obj["principal_ref"], "principal_ref"),
                    permissions, _unique_strings(obj["allowed_side_effects"], "allowed_side_effects"),
                    slots, _hash(obj["content_hash"], "content_hash"))  # type: ignore[arg-type]
        if lease.id != f"lease:{lease.content_hash}":
            raise CatalogValidationError("lease id must be derived from content_hash")
        if verify_hash and canonical_hash(lease.to_dict(include_hash=False, include_id=False)) != lease.content_hash:
            raise CatalogValidationError("lease content_hash mismatch")
        return lease

    def to_dict(self, *, include_hash: bool = True, include_id: bool = True) -> dict[str, Any]:
        result = {"schema_version": self.schema_version, "created_at": self.created_at,
                  "expires_at": self.expires_at, "work_order_ref": self.work_order_ref,
                  "work_order_hash": self.work_order_hash, "capability_id": self.capability_id,
                  "revision_ref": self.revision_ref, "descriptor_hash": self.descriptor_hash,
                  "source_hash": self.source_hash, "schema_hash": self.schema_hash,
                  "catalog_epoch": self.catalog_epoch, "policy_ref": self.policy_ref,
                  "policy_hash": self.policy_hash, "principal_ref": self.principal_ref,
                  "permissions": self.permissions.to_dict(),
                  "allowed_side_effects": list(self.allowed_side_effects),
                  "credential_slot_refs": list(self.credential_slot_refs)}
        if include_id:
            result["id"] = self.id
        if include_hash:
            result["content_hash"] = self.content_hash
        return result


class CapabilityCatalog:
    """Append-only descriptor catalog with deterministic lexical discovery."""

    def __init__(
        self,
        path: str | Path = ":memory:",
        *,
        clock: Callable[[], datetime] | None = None,
        max_lease_seconds: int = 300,
        approval_resolver: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None = None,
        policy_resolver: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None = None,
        source_registration_resolver: (
            Callable[[Mapping[str, Any]], Mapping[str, Any]] | None
        ) = None,
    ) -> None:
        if type(max_lease_seconds) is not int or max_lease_seconds < 1:
            raise CatalogValidationError("max_lease_seconds must be a positive integer")
        self.path = str(path)
        self.clock = clock or _now
        self.max_lease_seconds = max_lease_seconds
        self.approval_resolver = approval_resolver
        self.policy_resolver = policy_resolver
        self.source_registration_resolver = source_registration_resolver
        self._conn = sqlite3.connect(self.path, isolation_level=None)
        if self.path != ":memory:":
            os.chmod(self.path, 0o600)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.execute("PRAGMA busy_timeout = 5000")
        self._conn.executescript(_SCHEMA_PATH.read_text(encoding="utf-8"))

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "CapabilityCatalog":
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()

    @property
    def epoch(self) -> int:
        return int(self._conn.execute("SELECT catalog_epoch FROM capability_catalog_meta WHERE singleton=1").fetchone()[0])

    def _time(self) -> datetime:
        value = self.clock()
        _timestamp(value)
        return value.astimezone(timezone.utc)

    def register_external_source(self, source_instance_ref: str) -> dict[str, Any]:
        """Register one exporter instance through a trusted operator resolver."""

        source_instance_ref = _ref(
            source_instance_ref, "source_instance_ref"
        )  # type: ignore[assignment]
        if self.source_registration_resolver is None:
            raise ExternalSourceRegistrationRequired(
                "trusted external source registration resolver is required"
            )
        now = self._time()
        existing = self._conn.execute(
            "SELECT registration_hash FROM external_capability_source_registrations "
            "WHERE source_instance_ref=?",
            (source_instance_ref,),
        ).fetchone()
        observed_active = self._conn.execute(
            "SELECT a.source_instance_ref,r.registration_ref,a.registration_hash "
            "FROM external_capability_active_source a "
            "JOIN external_capability_source_registrations r "
            "ON r.source_instance_ref=a.source_instance_ref WHERE a.singleton=1"
        ).fetchone()
        if existing is not None:
            if (
                observed_active is None
                or observed_active["source_instance_ref"] != source_instance_ref
            ):
                raise ExternalSourceRegistrationRequired(
                    "superseded source instances cannot be reactivated; "
                    "register a new instance"
                )
            return {
                "write_status": "duplicate",
                "source_instance_ref": source_instance_ref,
                "registration_hash": existing["registration_hash"],
                "invalidated_descriptors": 0,
            }
        prior_source_instance_ref = (
            None if observed_active is None else observed_active["source_instance_ref"]
        )
        prior_registration_hash = (
            None if observed_active is None else observed_active["registration_hash"]
        )
        prior_registration_ref = (
            None if observed_active is None else observed_active["registration_ref"]
        )
        query = {
            "source_instance_ref": source_instance_ref,
            "prior_source_instance_ref": prior_source_instance_ref,
            "prior_registration_ref": prior_registration_ref,
            "prior_registration_hash": prior_registration_hash,
            "at": _timestamp(now),
        }
        try:
            raw = self.source_registration_resolver(query)
        except CatalogError:
            raise
        except Exception as exc:
            raise ExternalSourceRegistrationRequired(
                "external source registration lookup failed"
            ) from exc
        fields = {
            "schema_version", "registration_ref", "source_instance_ref",
            "prior_source_instance_ref", "prior_registration_ref",
            "prior_registration_hash",
            "decision", "registered_by", "active", "effective_from",
            "effective_until", "receipt_hash",
        }
        receipt = dict(_closed(raw, fields, fields, "external source registration"))
        if receipt["schema_version"] != SCHEMA_VERSION:
            raise ExternalSourceRegistrationRequired(
                "external source registration schema_version is unsupported"
            )
        receipt["registration_ref"] = _ref(
            receipt["registration_ref"], "registration_ref"
        )
        receipt["source_instance_ref"] = _ref(
            receipt["source_instance_ref"], "source_instance_ref"
        )
        if receipt["source_instance_ref"] != source_instance_ref:
            raise ExternalSourceRegistrationRequired(
                "registration source instance does not match request"
            )
        receipt["prior_source_instance_ref"] = _ref(
            receipt["prior_source_instance_ref"],
            "prior_source_instance_ref",
            nullable=True,
        )
        receipt["prior_registration_ref"] = _ref(
            receipt["prior_registration_ref"],
            "prior_registration_ref",
            nullable=True,
        )
        if receipt["prior_registration_hash"] is not None:
            receipt["prior_registration_hash"] = _hash(
                receipt["prior_registration_hash"], "prior_registration_hash"
            )
        if (
            receipt["prior_source_instance_ref"] != prior_source_instance_ref
            or receipt["prior_registration_ref"] != prior_registration_ref
            or receipt["prior_registration_hash"] != prior_registration_hash
        ):
            raise ExternalSourceRegistrationRequired(
                "registration does not bind the observed active source head"
            )
        if receipt["decision"] != "approve" or receipt["active"] is not True:
            raise ExternalSourceRegistrationRequired(
                "external source is not actively approved"
            )
        receipt["registered_by"] = _ref(
            receipt["registered_by"], "registered_by"
        )
        if not str(receipt["registered_by"]).startswith("human:"):
            raise ExternalSourceRegistrationRequired(
                "external source registration requires a human operator"
            )
        effective_from = _parse_time(receipt["effective_from"], "effective_from")
        if receipt["effective_until"] is not None:
            raise ExternalSourceRegistrationRequired(
                "source registration wire 0.1 requires effective_until=null; "
                "rotate to a new source instance explicitly"
            )
        if effective_from > now:
            raise ExternalSourceRegistrationRequired(
                "external source registration is not currently effective"
            )
        declared_hash = _hash(receipt.pop("receipt_hash"), "receipt_hash")
        if canonical_hash(receipt) != declared_hash:
            raise ExternalSourceRegistrationRequired(
                "external source registration receipt hash mismatch"
            )
        receipt["receipt_hash"] = declared_hash
        try:
            self._conn.execute("BEGIN IMMEDIATE")
            active = self._conn.execute(
                "SELECT a.source_instance_ref,r.registration_ref,a.registration_hash "
                "FROM external_capability_active_source a "
                "JOIN external_capability_source_registrations r "
                "ON r.source_instance_ref=a.source_instance_ref WHERE a.singleton=1"
            ).fetchone()
            live_prior = (
                None if active is None else active["source_instance_ref"],
                None if active is None else active["registration_ref"],
                None if active is None else active["registration_hash"],
            )
            if live_prior != (
                prior_source_instance_ref,
                prior_registration_ref,
                prior_registration_hash,
            ):
                raise StaleCatalog(
                    "active external source changed during registration"
                )
            existing = self._conn.execute(
                "SELECT registration_hash FROM external_capability_source_registrations "
                "WHERE source_instance_ref=?",
                (source_instance_ref,),
            ).fetchone()
            if existing is not None:
                raise StaleCatalog("source instance was registered concurrently")
            self._conn.execute(
                "INSERT INTO external_capability_source_registrations"
                "(source_instance_ref,registration_ref,registration_hash,"
                "registration_json,registered_at) VALUES(?,?,?,?,?)",
                (
                    source_instance_ref, receipt["registration_ref"], declared_hash,
                    canonical_json(receipt), _timestamp(now),
                ),
            )
            invalidated_descriptors = 0
            if active is None or active["source_instance_ref"] != source_instance_ref:
                descriptor_rows = self._conn.execute(
                    "SELECT c.capability_id,v.descriptor_json "
                    "FROM capability_current c JOIN capability_descriptor_versions v "
                    "ON v.revision_ref=c.revision_ref"
                ).fetchall()
                external_descriptor_ids = []
                for row in descriptor_rows:
                    descriptor = json.loads(row["descriptor_json"])
                    source = descriptor["source"]
                    if (
                        source["type"] == "mcp"
                        or (
                            source["type"] == "skill"
                            and source["namespace"] == "openclaw"
                        )
                    ):
                        external_descriptor_ids.append(row["capability_id"])
                invalidated_descriptors = len(external_descriptor_ids)
                self._conn.executemany(
                    "DELETE FROM capability_current WHERE capability_id=?",
                    [(capability_id,) for capability_id in external_descriptor_ids],
                )
                self._conn.execute("DELETE FROM external_capability_metadata_current")
                if invalidated_descriptors:
                    self._conn.execute(
                        "UPDATE capability_catalog_meta "
                        "SET catalog_epoch=catalog_epoch+1 WHERE singleton=1"
                    )
            self._conn.execute(
                "INSERT INTO external_capability_active_source"
                "(singleton,source_instance_ref,registration_hash,updated_at) "
                "VALUES(1,?,?,?) ON CONFLICT(singleton) DO UPDATE SET "
                "source_instance_ref=excluded.source_instance_ref,"
                "registration_hash=excluded.registration_hash,"
                "updated_at=excluded.updated_at",
                (source_instance_ref, declared_hash, _timestamp(now)),
            )
            self._conn.execute(
                "UPDATE external_capability_import_state "
                "SET import_generation=import_generation+1 WHERE singleton=1"
            )
            self._conn.commit()
        except BaseException:
            self._conn.rollback()
            raise
        return {
            "write_status": "fresh",
            "source_instance_ref": source_instance_ref,
            "registration_hash": declared_hash,
            "invalidated_descriptors": invalidated_descriptors,
        }

    def _resolve_approval(
        self,
        *,
        capability_id: str,
        catalog_version: int,
        source_ref: str,
        source_hash: str,
        schema_hash: str,
        permissions: CapabilityPermissions,
        at: datetime,
    ) -> CapabilityApproval:
        if self.approval_resolver is None:
            raise ApprovalRequired("trusted capability approval resolver is required")
        query = {
            "capability_id": capability_id,
            "catalog_version": catalog_version,
            "source_ref": source_ref,
            "source_hash": source_hash,
            "schema_hash": schema_hash,
            "requested_permissions": permissions.to_dict(),
            "at": _timestamp(at),
        }
        try:
            raw = self.approval_resolver(query)
        except CatalogError:
            raise
        except Exception as exc:
            raise ApprovalRequired("capability approval lookup failed") from exc
        if not isinstance(raw, Mapping):
            raise ApprovalRequired("capability approval lookup returned no receipt")
        approval = CapabilityApproval.from_dict(raw)
        if approval.capability_id != capability_id:
            raise ApprovalRequired("approval belongs to another capability")
        if (
            approval.artifact_ref != source_ref
            or approval.artifact_hash != source_hash
            or approval.schema_hash != schema_hash
            or approval.approved_permissions != permissions
        ):
            raise ApprovalRequired(
                "approval artifact/schema/permissions do not match descriptor"
            )
        if not approval.is_effective(at):
            raise ApprovalRequired("capability approval is not currently effective")
        return approval

    def _resolve_policy(
        self, *, policy_ref: str, principal_ref: str, at: datetime
    ) -> ResolvedGovernancePolicy:
        if self.policy_resolver is None:
            raise GovernancePolicyRejected("trusted governance policy resolver is required")
        query = {
            "policy_ref": policy_ref,
            "principal_ref": principal_ref,
            "at": _timestamp(at),
        }
        try:
            raw = self.policy_resolver(query)
        except CatalogError:
            raise
        except Exception as exc:
            raise GovernancePolicyRejected("governance policy lookup failed") from exc
        if not isinstance(raw, Mapping):
            raise GovernancePolicyRejected("governance policy lookup returned no policy")
        policy = ResolvedGovernancePolicy.from_dict(raw)
        if policy.policy_ref != policy_ref:
            raise GovernancePolicyRejected("resolved policy ref does not match request")
        if not policy.is_effective(at):
            raise GovernancePolicyRejected("governance policy is not currently effective")
        if principal_ref not in policy.allowed_principal_refs:
            raise GovernancePolicyRejected("principal is not allowed by governance policy")
        return policy

    def publish(self, spec: Mapping[str, Any], *, expected_epoch: int | None = None) -> CapabilityDescriptor:
        input_keys = {"schema_version", "id", "version", "created_at", "kind", "name", "label", "summary",
                      "aliases", "tags", "intent_examples", "source", "contract", "permissions", "eligibility",
                      "source_hash", "schema_hash"}
        obj = _closed(spec, input_keys, input_keys, "descriptor proposal")
        if obj["schema_version"] != SCHEMA_VERSION:
            raise CatalogValidationError("unsupported descriptor schema_version")
        capability_id = _string(obj["id"], "id")
        if not _CAPABILITY_RE.fullmatch(capability_id):
            raise CatalogValidationError("id must be a canonical namespaced capability ID")
        version = obj["version"]
        if type(version) is not int or version < 1:
            raise CatalogValidationError("descriptor version must be a positive integer")
        _parse_time(obj["created_at"], "created_at")
        current_epoch = self.epoch
        if expected_epoch is not None and expected_epoch != current_epoch:
            raise StaleCatalog(f"catalog epoch is {current_epoch}, not {expected_epoch}")
        current_import_generation = int(
            self._conn.execute(
                "SELECT import_generation FROM external_capability_import_state WHERE singleton=1"
            ).fetchone()[0]
        )
        next_epoch = current_epoch + 1
        now = self._time()
        source = CapabilitySource.from_dict(obj["source"])
        permissions = CapabilityPermissions.from_dict(obj["permissions"])
        eligibility = CapabilityEligibility.from_dict(obj["eligibility"])
        external_row = self._conn.execute(
            "SELECT v.source_hash,v.schema_hash,v.metadata_json "
            "FROM external_capability_metadata_current c "
            "JOIN external_capability_metadata_versions v ON v.metadata_ref=c.metadata_ref "
            "WHERE c.capability_id=?",
            (capability_id,),
        ).fetchone()
        if external_row is not None:
            external_metadata = json.loads(external_row["metadata_json"])
            if not _descriptor_matches_external(obj, external_metadata):
                raise StaleCatalog(
                    "descriptor source/schema does not match current imported metadata"
                )
            if (
                external_metadata["availability_state"] != "ready"
                and eligibility.state in {"ready", "auth_required"}
            ):
                raise StaleCatalog("unavailable imported capability cannot be published ready")
        elif self._external_scope_is_authoritative(capability_id):
            raise StaleCatalog("capability is absent from current complete external inventory")
        approval = self._resolve_approval(
            capability_id=capability_id,
            catalog_version=version,
            source_ref=source.source_ref,
            source_hash=_hash(obj["source_hash"], "source_hash"),
            schema_hash=_hash(obj["schema_hash"], "schema_hash"),
            permissions=permissions,
            at=now,
        )
        wire = dict(obj)
        wire["approval"] = approval.to_dict()
        wire["revision_ref"] = f"{obj['id']}@v{obj['version']}"
        wire["catalog_epoch"] = next_epoch
        wire["content_hash"] = canonical_hash(wire)
        descriptor = CapabilityDescriptor.from_dict(wire)
        row = self._conn.execute(
            "SELECT MAX(version) FROM capability_descriptor_versions WHERE capability_id=?",
            (descriptor.id,),
        ).fetchone()
        expected_version = 1 if row is None or row[0] is None else int(row[0]) + 1
        if descriptor.version != expected_version:
            raise CatalogConflict(f"descriptor version must be {expected_version}")
        try:
            self._conn.execute("BEGIN IMMEDIATE")
            live_epoch = int(self._conn.execute("SELECT catalog_epoch FROM capability_catalog_meta WHERE singleton=1").fetchone()[0])
            if live_epoch != current_epoch:
                raise StaleCatalog("catalog changed during publish")
            live_import_generation = int(
                self._conn.execute(
                    "SELECT import_generation FROM external_capability_import_state "
                    "WHERE singleton=1"
                ).fetchone()[0]
            )
            if live_import_generation != current_import_generation:
                raise StaleCatalog("external metadata changed during publish")
            self._conn.execute(
                "INSERT INTO capability_descriptor_versions "
                "(revision_ref,capability_id,version,catalog_epoch,descriptor_hash,"
                "approval_ref,approval_hash,registry_revision_ref,descriptor_json,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                (descriptor.revision_ref, descriptor.id, descriptor.version, descriptor.catalog_epoch,
                 descriptor.content_hash, descriptor.approval.approval_ref,
                 descriptor.approval.receipt_hash,
                 descriptor.approval.registry_revision_ref,
                 canonical_json(descriptor.to_dict()), descriptor.created_at),
            )
            self._conn.execute(
                "INSERT INTO capability_current(capability_id,revision_ref,version,catalog_epoch) VALUES(?,?,?,?) "
                "ON CONFLICT(capability_id) DO UPDATE SET revision_ref=excluded.revision_ref,version=excluded.version,catalog_epoch=excluded.catalog_epoch",
                (descriptor.id, descriptor.revision_ref, descriptor.version, next_epoch),
            )
            self._conn.execute("UPDATE capability_catalog_meta SET catalog_epoch=? WHERE singleton=1", (next_epoch,))
            self._conn.commit()
        except BaseException:
            self._conn.rollback()
            raise
        return descriptor

    def _external_scope_is_authoritative(self, capability_id: str) -> bool:
        parts = capability_id.split(":")
        if len(parts) != 4:
            return False
        source_type, namespace = parts[1], parts[2]
        if source_type == "skill" and namespace != "openclaw":
            return False
        if source_type not in {"skill", "mcp"}:
            return False
        rows = self._conn.execute(
            "SELECT snapshot_json FROM external_capability_snapshots ORDER BY imported_at DESC"
        ).fetchall()
        for row in rows:
            snapshot = json.loads(row["snapshot_json"])
            scope = snapshot["scope"]
            if source_type == "skill" and scope["skills_complete"]:
                return True
            if source_type == "mcp" and namespace in scope["mcp_servers_complete"]:
                return True
        return False

    def apply_external_metadata_snapshot(
        self,
        *,
        snapshot_ref: str,
        snapshot_hash: str,
        producer_version: str,
        source_instance_ref: str,
        exporter_version: str,
        catalog_generation: int,
        prior_snapshot_ref: str | None,
        prior_snapshot_hash: str | None,
        snapshot_json: str,
        snapshot_created_at: str,
        entries: Sequence[Mapping[str, Any]],
        schemas: Sequence[Mapping[str, Any]],
        skills_complete: bool,
        mcp_servers_complete: Sequence[str],
    ) -> dict[str, Any]:
        """Atomically import a validated external metadata snapshot.

        The importer stores compact metadata and MCP JSON Schemas, never skill
        instructions, credentials, or executable code.  Drift of a previously
        observed skill/tool bumps the catalog epoch and withdraws its current
        descriptor projection before a replacement can be human-approved.
        """

        snapshot_ref = _ref(snapshot_ref, "snapshot_ref")  # type: ignore[assignment]
        snapshot_hash = _hash(snapshot_hash, "snapshot_hash")
        producer_version = _string(producer_version, "producer_version")
        source_instance_ref = _ref(
            source_instance_ref, "source_instance_ref"
        )  # type: ignore[assignment]
        exporter_version = _string(exporter_version, "exporter_version")
        if len(exporter_version) > 256:
            raise CatalogValidationError("exporter_version is too long")
        if type(catalog_generation) is not int or catalog_generation < 1:
            raise CatalogValidationError("catalog_generation must be a positive integer")
        prior_snapshot_ref = _ref(
            prior_snapshot_ref, "prior_snapshot_ref", nullable=True
        )
        if prior_snapshot_hash is not None:
            prior_snapshot_hash = _hash(prior_snapshot_hash, "prior_snapshot_hash")
        if (prior_snapshot_ref is None) != (prior_snapshot_hash is None):
            raise CatalogValidationError("prior snapshot ref/hash must be paired")
        if catalog_generation == 1 and prior_snapshot_ref is not None:
            raise CatalogValidationError("first generation cannot have a prior snapshot")
        if catalog_generation > 1 and prior_snapshot_ref is None:
            raise CatalogValidationError("later generations require a prior snapshot")
        _parse_time(snapshot_created_at, "snapshot_created_at")
        if type(skills_complete) is not bool:
            raise CatalogValidationError("skills_complete must be boolean")
        complete_servers = _unique_strings(
            mcp_servers_complete, "mcp_servers_complete"
        )
        try:
            decoded_snapshot = json.loads(snapshot_json)
        except (TypeError, ValueError) as exc:
            raise CatalogValidationError("snapshot_json must be valid JSON") from exc
        if canonical_hash(decoded_snapshot) != snapshot_hash:
            raise CatalogConflict("snapshot_hash does not bind snapshot_json")
        try:
            producer = decoded_snapshot["producer"]
            exact_envelope = (
                decoded_snapshot["id"] == snapshot_ref
                and decoded_snapshot["created_at"] == snapshot_created_at
                and producer["openclaw_version"] == producer_version
                and producer["source_instance_ref"] == source_instance_ref
                and producer["exporter_version"] == exporter_version
                and producer["catalog_generation"] == catalog_generation
                and producer["prior_snapshot_ref"] == prior_snapshot_ref
                and producer["prior_snapshot_hash"] == prior_snapshot_hash
            )
        except (KeyError, TypeError) as exc:
            raise CatalogValidationError(
                "snapshot_json lacks the required wire 0.2 producer fields"
            ) from exc
        if not exact_envelope:
            raise CatalogConflict("snapshot arguments do not match snapshot_json")

        normalized_entries: list[dict[str, Any]] = []
        seen_capabilities: set[str] = set()
        seen_scope_keys: set[tuple[str, str]] = set()
        entry_fields = {
            "metadata_ref", "capability_id", "source_type", "source_scope",
            "source_key", "source_version", "source_hash", "schema_hash",
            "metadata_hash", "metadata", "created_at",
        }
        for index, raw in enumerate(entries):
            item = dict(_closed(raw, entry_fields, entry_fields, f"entries[{index}]"))
            for name in (
                "metadata_ref", "capability_id", "source_scope", "source_key",
                "source_version",
            ):
                item[name] = _string(item[name], f"entries[{index}].{name}")
            if not _CAPABILITY_RE.fullmatch(item["capability_id"]):
                raise CatalogValidationError("external capability_id is not canonical")
            if item["source_type"] not in {"skill", "mcp"}:
                raise CatalogValidationError("external source_type must be skill or mcp")
            if item["capability_id"].split(":")[1] != item["source_type"]:
                raise CatalogValidationError("external source_type does not match capability_id")
            for name in ("source_hash", "schema_hash", "metadata_hash"):
                item[name] = _hash(item[name], f"entries[{index}].{name}")
            _parse_time(item["created_at"], f"entries[{index}].created_at")
            if not isinstance(item["metadata"], Mapping):
                raise CatalogValidationError("external metadata must be an object")
            item["metadata"] = json.loads(canonical_json(item["metadata"]))
            if canonical_hash(item["metadata"]) != item["metadata_hash"]:
                raise CatalogConflict("metadata_hash does not bind metadata")
            scope_key = (item["source_scope"], item["source_key"])
            if item["capability_id"] in seen_capabilities or scope_key in seen_scope_keys:
                raise CatalogValidationError("external metadata entries must be unique")
            seen_capabilities.add(item["capability_id"])
            seen_scope_keys.add(scope_key)
            normalized_entries.append(item)

        normalized_schemas: list[dict[str, Any]] = []
        seen_schemas: set[tuple[str, str]] = set()
        schema_fields = {"schema_ref", "schema_hash", "schema", "created_at"}
        for index, raw in enumerate(schemas):
            item = dict(_closed(raw, schema_fields, schema_fields, f"schemas[{index}]"))
            item["schema_ref"] = _ref(item["schema_ref"], f"schemas[{index}].schema_ref")
            item["schema_hash"] = _hash(item["schema_hash"], f"schemas[{index}].schema_hash")
            _parse_time(item["created_at"], f"schemas[{index}].created_at")
            if not isinstance(item["schema"], Mapping):
                raise CatalogValidationError("external schema must be an object")
            item["schema"] = json.loads(canonical_json(item["schema"]))
            if canonical_hash(item["schema"]) != item["schema_hash"]:
                raise CatalogConflict("schema_hash does not bind schema")
            identity = (str(item["schema_ref"]), item["schema_hash"])
            if identity in seen_schemas:
                raise CatalogValidationError("external schema artifacts must be unique")
            seen_schemas.add(identity)
            normalized_schemas.append(item)

        observed_import_generation = int(
            self._conn.execute(
                "SELECT import_generation FROM external_capability_import_state WHERE singleton=1"
            ).fetchone()[0]
        )

        current_rows = self._conn.execute(
            "SELECT c.capability_id,c.metadata_ref,c.source_scope,c.source_key,c.metadata_hash "
            "FROM external_capability_metadata_current c"
        ).fetchall()
        current = {row["capability_id"]: dict(row) for row in current_rows}
        incoming = {item["capability_id"]: item for item in normalized_entries}
        descriptor_rows = self._conn.execute(
            "SELECT c.capability_id,v.descriptor_json FROM capability_current c "
            "JOIN capability_descriptor_versions v ON v.revision_ref=c.revision_ref"
        ).fetchall()
        current_descriptors = {
            row["capability_id"]: json.loads(row["descriptor_json"])
            for row in descriptor_rows
        }
        removals: list[dict[str, Any]] = []
        complete_scopes = {f"mcp:{server}" for server in complete_servers}
        for capability_id, prior in current.items():
            in_complete_scope = (
                (skills_complete and prior["source_scope"] == "skills")
                or prior["source_scope"] in complete_scopes
            )
            if in_complete_scope and capability_id not in incoming:
                removals.append(prior)
        removal_ids = {item["capability_id"] for item in removals}
        for capability_id, descriptor in current_descriptors.items():
            if capability_id in incoming or capability_id in removal_ids:
                continue
            source = descriptor["source"]
            in_complete_scope = (
                skills_complete
                and source["type"] == "skill"
                and source["namespace"] == "openclaw"
            ) or (
                source["type"] == "mcp"
                and f"mcp:{source['namespace']}" in complete_scopes
            )
            if in_complete_scope:
                removals.append(
                    {
                        "capability_id": capability_id,
                        "metadata_ref": None,
                        "source_scope": (
                            "skills" if source["type"] == "skill"
                            else f"mcp:{source['namespace']}"
                        ),
                        "source_key": capability_id.rsplit(":", 1)[-1],
                    }
                )
                removal_ids.add(capability_id)

        actions: list[tuple[dict[str, Any], str, str | None]] = []
        has_drift = bool(removals)
        for item in normalized_entries:
            prior = current.get(item["capability_id"])
            descriptor = current_descriptors.get(item["capability_id"])
            descriptor_drift = descriptor is not None and not _descriptor_matches_external(
                descriptor, item["metadata"]
            )
            if prior is None:
                action = "changed" if descriptor_drift else "added"
                prior_ref = None
            elif prior["metadata_hash"] == item["metadata_hash"]:
                action = "unchanged"
                prior_ref = prior["metadata_ref"]
            else:
                action = "changed"
                prior_ref = prior["metadata_ref"]
                has_drift = True
            if descriptor_drift:
                has_drift = True
            actions.append((item, action, prior_ref))

        current_epoch = self.epoch
        next_epoch = current_epoch + 1 if has_drift else current_epoch
        imported_at = _timestamp(self._time())
        try:
            self._conn.execute("BEGIN IMMEDIATE")
            live_epoch = int(
                self._conn.execute(
                    "SELECT catalog_epoch FROM capability_catalog_meta WHERE singleton=1"
                ).fetchone()[0]
            )
            live_import_generation = int(
                self._conn.execute(
                    "SELECT import_generation FROM external_capability_import_state "
                    "WHERE singleton=1"
                ).fetchone()[0]
            )

            active_source = self._conn.execute(
                "SELECT source_instance_ref FROM external_capability_active_source "
                "WHERE singleton=1"
            ).fetchone()
            registration = self._conn.execute(
                "SELECT registration_hash FROM external_capability_source_registrations "
                "WHERE source_instance_ref=?",
                (source_instance_ref,),
            ).fetchone()
            head = self._conn.execute(
                "SELECT catalog_generation,snapshot_ref,snapshot_hash "
                "FROM external_capability_source_heads WHERE source_instance_ref=?",
                (source_instance_ref,),
            ).fetchone()
            existing_snapshot = self._conn.execute(
                "SELECT snapshot_hash FROM external_capability_snapshots "
                "WHERE snapshot_ref=?",
                (snapshot_ref,),
            ).fetchone()

            outcome = "accepted"
            rejection_message = ""
            if (
                registration is None
                or active_source is None
                or active_source["source_instance_ref"] != source_instance_ref
            ):
                outcome = "unregistered"
                rejection_message = "snapshot source instance is not the active registration"
            elif head is None:
                if catalog_generation != 1:
                    outcome = "gap"
                    rejection_message = "first accepted snapshot must have generation 1"
                elif prior_snapshot_ref is not None:
                    outcome = "fork"
                    rejection_message = "first accepted snapshot cannot name a prior head"
                elif existing_snapshot is not None:
                    outcome = "equivocation"
                    rejection_message = "snapshot_ref is already bound to other content"
            else:
                head_generation = int(head["catalog_generation"])
                if catalog_generation < head_generation:
                    outcome = "stale"
                    rejection_message = "snapshot generation is older than the current head"
                elif catalog_generation == head_generation:
                    if (
                        snapshot_ref == head["snapshot_ref"]
                        and snapshot_hash == head["snapshot_hash"]
                    ):
                        outcome = "duplicate"
                    else:
                        outcome = "equivocation"
                        rejection_message = (
                            "snapshot generation conflicts with the current head"
                        )
                elif catalog_generation > head_generation + 1:
                    outcome = "gap"
                    rejection_message = "snapshot generation skipped the exact next value"
                elif (
                    prior_snapshot_ref != head["snapshot_ref"]
                    or prior_snapshot_hash != head["snapshot_hash"]
                ):
                    outcome = "fork"
                    rejection_message = "snapshot prior ref/hash does not match the current head"
                elif existing_snapshot is not None:
                    outcome = "equivocation"
                    rejection_message = "snapshot_ref is already bound to other content"

            observed_head_ref = None if head is None else head["snapshot_ref"]
            observed_head_hash = None if head is None else head["snapshot_hash"]
            observed_head_generation = (
                None if head is None else int(head["catalog_generation"])
            )
            ingest_event = {
                "schema_version": SCHEMA_VERSION,
                "source_instance_ref": source_instance_ref,
                "snapshot_ref": snapshot_ref,
                "snapshot_hash": snapshot_hash,
                "catalog_generation": catalog_generation,
                "prior_snapshot_ref": prior_snapshot_ref,
                "prior_snapshot_hash": prior_snapshot_hash,
                "observed_head_ref": observed_head_ref,
                "observed_head_hash": observed_head_hash,
                "observed_head_generation": observed_head_generation,
                "outcome": outcome,
                "created_at": imported_at,
            }
            ingest_event["content_hash"] = canonical_hash(ingest_event)
            ingest_event_ref = f"external-ingest:{ingest_event['content_hash']}"
            self._conn.execute(
                "INSERT OR IGNORE INTO external_capability_snapshot_ingest_events"
                "(event_ref,source_instance_ref,snapshot_ref,snapshot_hash,"
                "catalog_generation,prior_snapshot_ref,prior_snapshot_hash,"
                "observed_head_ref,observed_head_hash,observed_head_generation,"
                "outcome,event_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    ingest_event_ref, source_instance_ref, snapshot_ref, snapshot_hash,
                    catalog_generation, prior_snapshot_ref, prior_snapshot_hash,
                    observed_head_ref, observed_head_hash, observed_head_generation,
                    outcome, canonical_json(ingest_event), imported_at,
                ),
            )
            self._conn.execute(
                "UPDATE external_capability_import_state "
                "SET import_generation=import_generation+1 WHERE singleton=1"
            )
            if outcome == "duplicate":
                events = self._conn.execute(
                    "SELECT event_json FROM external_capability_sync_events "
                    "WHERE snapshot_ref=? ORDER BY rowid",
                    (snapshot_ref,),
                ).fetchall()
                self._conn.commit()
                return {
                    "write_status": "duplicate",
                    "snapshot_ref": snapshot_ref,
                    "snapshot_hash": snapshot_hash,
                    "catalog_epoch": live_epoch,
                    "ingest_event": ingest_event,
                    "events": [json.loads(row["event_json"]) for row in events],
                }
            if outcome != "accepted":
                self._conn.commit()
                raise ExternalSnapshotRejected(outcome, rejection_message)
            if live_epoch != current_epoch:
                raise StaleCatalog("catalog changed during metadata import")
            if live_import_generation != observed_import_generation:
                raise StaleCatalog("external metadata changed during import")
            self._conn.execute(
                "INSERT INTO external_capability_snapshots"
                "(snapshot_ref,snapshot_hash,producer_version,snapshot_json,created_at,"
                "imported_at) VALUES(?,?,?,?,?,?)",
                (
                    snapshot_ref, snapshot_hash, producer_version, snapshot_json,
                    snapshot_created_at, imported_at,
                ),
            )
            self._conn.execute(
                "INSERT INTO external_capability_snapshot_chains"
                "(snapshot_ref,source_instance_ref,exporter_version,catalog_generation,"
                "prior_snapshot_ref,prior_snapshot_hash) VALUES(?,?,?,?,?,?)",
                (
                    snapshot_ref, source_instance_ref, exporter_version,
                    catalog_generation, prior_snapshot_ref, prior_snapshot_hash,
                ),
            )
            self._conn.execute(
                "INSERT INTO external_capability_source_heads"
                "(source_instance_ref,catalog_generation,snapshot_ref,snapshot_hash,updated_at) "
                "VALUES(?,?,?,?,?) ON CONFLICT(source_instance_ref) DO UPDATE SET "
                "catalog_generation=excluded.catalog_generation,"
                "snapshot_ref=excluded.snapshot_ref,snapshot_hash=excluded.snapshot_hash,"
                "updated_at=excluded.updated_at",
                (
                    source_instance_ref, catalog_generation, snapshot_ref,
                    snapshot_hash, imported_at,
                ),
            )
            for schema in normalized_schemas:
                self._conn.execute(
                    "INSERT OR IGNORE INTO external_schema_artifacts"
                    "(schema_ref,schema_hash,schema_json,snapshot_ref,created_at) VALUES(?,?,?,?,?)",
                    (
                        schema["schema_ref"], schema["schema_hash"],
                        canonical_json(schema["schema"]), snapshot_ref, schema["created_at"],
                    ),
                )
            event_records: list[dict[str, Any]] = []
            for item, action, prior_ref in actions:
                self._conn.execute(
                    "INSERT OR IGNORE INTO external_capability_metadata_versions"
                    "(metadata_ref,capability_id,source_type,source_scope,source_key,source_version,"
                    "source_hash,schema_hash,metadata_hash,metadata_json,snapshot_ref,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        item["metadata_ref"], item["capability_id"], item["source_type"],
                        item["source_scope"], item["source_key"], item["source_version"],
                        item["source_hash"], item["schema_hash"], item["metadata_hash"],
                        canonical_json(item["metadata"]), snapshot_ref, item["created_at"],
                    ),
                )
                self._conn.execute(
                    "INSERT INTO external_capability_metadata_current"
                    "(capability_id,metadata_ref,source_scope,source_key,metadata_hash,snapshot_ref) "
                    "VALUES(?,?,?,?,?,?) ON CONFLICT(capability_id) DO UPDATE SET "
                    "metadata_ref=excluded.metadata_ref,source_scope=excluded.source_scope,"
                    "source_key=excluded.source_key,metadata_hash=excluded.metadata_hash,"
                    "snapshot_ref=excluded.snapshot_ref",
                    (
                        item["capability_id"], item["metadata_ref"], item["source_scope"],
                        item["source_key"], item["metadata_hash"], snapshot_ref,
                    ),
                )
                if action == "changed":
                    self._conn.execute(
                        "DELETE FROM capability_current WHERE capability_id=?",
                        (item["capability_id"],),
                    )
                event_records.append(
                    {
                        "snapshot_ref": snapshot_ref,
                        "capability_id": item["capability_id"],
                        "action": action,
                        "prior_metadata_ref": prior_ref,
                        "metadata_ref": item["metadata_ref"],
                        "catalog_epoch": next_epoch,
                    }
                )
            for prior in removals:
                self._conn.execute(
                    "DELETE FROM external_capability_metadata_current WHERE capability_id=?",
                    (prior["capability_id"],),
                )
                self._conn.execute(
                    "DELETE FROM capability_current WHERE capability_id=?",
                    (prior["capability_id"],),
                )
                event_records.append(
                    {
                        "snapshot_ref": snapshot_ref,
                        "capability_id": prior["capability_id"],
                        "action": "removed",
                        "prior_metadata_ref": prior["metadata_ref"],
                        "metadata_ref": None,
                        "catalog_epoch": next_epoch,
                    }
                )
            if has_drift:
                self._conn.execute(
                    "UPDATE capability_catalog_meta SET catalog_epoch=? WHERE singleton=1",
                    (next_epoch,),
                )
            for event in event_records:
                event["content_hash"] = canonical_hash(event)
                event_ref = f"external-sync:{event['content_hash']}"
                self._conn.execute(
                    "INSERT INTO external_capability_sync_events"
                    "(event_ref,snapshot_ref,capability_id,action,prior_metadata_ref,metadata_ref,"
                    "catalog_epoch,event_json,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        event_ref, snapshot_ref, event["capability_id"], event["action"],
                        event["prior_metadata_ref"], event["metadata_ref"], next_epoch,
                        canonical_json(event), imported_at,
                    ),
                )
            self._conn.commit()
        except BaseException:
            self._conn.rollback()
            raise
        return {
            "write_status": "fresh",
            "snapshot_ref": snapshot_ref,
            "snapshot_hash": snapshot_hash,
            "catalog_epoch": next_epoch,
            "ingest_event": ingest_event,
            "events": event_records,
        }

    def get_external_metadata(self, capability_id: str) -> dict[str, Any]:
        row = self._conn.execute(
            "SELECT v.metadata_json FROM external_capability_metadata_current c "
            "JOIN external_capability_metadata_versions v ON v.metadata_ref=c.metadata_ref "
            "WHERE c.capability_id=?",
            (_string(capability_id, "capability_id"),),
        ).fetchone()
        if row is None:
            raise CapabilityNotFound(capability_id)
        return json.loads(row["metadata_json"])

    def load_external_schema(self, schema_ref: str, schema_hash: str) -> dict[str, Any]:
        row = self._conn.execute(
            "SELECT schema_json FROM external_schema_artifacts "
            "WHERE schema_ref=? AND schema_hash=?",
            (_ref(schema_ref, "schema_ref"), _hash(schema_hash, "schema_hash")),
        ).fetchone()
        if row is None:
            raise CapabilityNotFound(f"{schema_ref}@{schema_hash}")
        return json.loads(row["schema_json"])

    def _current_descriptors(self) -> list[CapabilityDescriptor]:
        rows = self._conn.execute(
            "SELECT d.descriptor_json FROM capability_current c JOIN capability_descriptor_versions d ON d.revision_ref=c.revision_ref"
        ).fetchall()
        return [CapabilityDescriptor.from_dict(json.loads(row[0])) for row in rows]

    @staticmethod
    def _visible(descriptor: CapabilityDescriptor, scopes: set[str], now: datetime) -> bool:
        eligibility = descriptor.eligibility
        if eligibility.state in {"unavailable", "quarantined"}:
            return False
        if "*" not in eligibility.visibility_scopes and not scopes.intersection(eligibility.visibility_scopes):
            return False
        return eligibility.valid_until is None or _parse_time(eligibility.valid_until, "valid_until") > now

    @staticmethod
    def _score(descriptor: CapabilityDescriptor, terms: list[str]) -> int:
        fields = {
            "id": descriptor.id.lower(), "name": descriptor.name.lower(), "label": descriptor.label.lower(),
            "summary": descriptor.summary.lower(), "aliases": " ".join(descriptor.aliases).lower(),
            "tags": " ".join(descriptor.tags).lower(), "intents": " ".join(descriptor.intent_examples).lower(),
        }
        weights = {"name": 12, "aliases": 10, "label": 8, "tags": 7, "id": 5, "intents": 4, "summary": 3}
        return sum(weight for term in terms for field, weight in weights.items() if term in fields[field])

    def search(self, query: str, *, visibility_scopes: Sequence[str], limit: int = 5) -> list[CapabilityHit]:
        query = _string(query, "query")
        scopes = set(_unique_strings(visibility_scopes, "visibility_scopes", nonempty=True))
        if type(limit) is not int or limit < 1 or limit > 50:
            raise CatalogValidationError("limit must be between 1 and 50")
        terms = _WORD_RE.findall(query.lower())
        epoch = self.epoch
        ranked: list[tuple[int, CapabilityDescriptor]] = []
        now = self._time()
        for descriptor in self._current_descriptors():
            if self._visible(descriptor, scopes, now):
                score = self._score(descriptor, terms)
                if score:
                    ranked.append((score, descriptor))
        ranked.sort(key=lambda item: (-item[0], item[1].id))
        return [CapabilityHit(d.id, d.revision_ref, d.version, epoch, d.name, d.label, d.summary,
                              d.kind, d.eligibility.state, score, d.content_hash)
                for score, d in ranked[:limit]]

    def describe(self, capability_id: str, *, visibility_scopes: Sequence[str],
                 catalog_epoch: int | None = None) -> CapabilityDescriptor:
        if not _CAPABILITY_RE.fullmatch(_string(capability_id, "capability_id")):
            raise CatalogValidationError("describe requires a namespaced capability ID")
        if catalog_epoch is not None and catalog_epoch != self.epoch:
            raise StaleCatalog("catalog epoch is stale")
        row = self._conn.execute(
            "SELECT d.descriptor_json FROM capability_current c JOIN capability_descriptor_versions d "
            "ON d.revision_ref=c.revision_ref WHERE c.capability_id=?", (capability_id,)
        ).fetchone()
        if row is None:
            raise CapabilityNotFound(capability_id)
        descriptor = CapabilityDescriptor.from_dict(json.loads(row[0]))
        scopes = set(_unique_strings(visibility_scopes, "visibility_scopes", nonempty=True))
        if not self._visible(descriptor, scopes, self._time()):
            raise CapabilityNotFound(capability_id)
        return descriptor

    @staticmethod
    def render_prompt_directory(hits: Sequence[CapabilityHit]) -> str:
        """Render compact discovery hints, deliberately excluding full contracts."""
        compact = [{"id": hit.id, "name": hit.name, "summary": hit.summary,
                    "state": hit.state} for hit in hits]
        return canonical_json({"capabilities": compact})

    def prepare(self, work_order: WorkOrder | Mapping[str, Any], *, capability_id: str,
                revision_ref: str, catalog_epoch: int, descriptor_hash: str,
                source_hash: str, schema_hash: str, policy_ref: str, policy_hash: str,
                principal_ref: str, visibility_scopes: Sequence[str], ttl_seconds: int = 60) -> CapabilityLease:
        if not isinstance(work_order, WorkOrder):
            work_order = WorkOrder.from_dict(work_order)
        if type(ttl_seconds) is not int or ttl_seconds < 1 or ttl_seconds > self.max_lease_seconds:
            raise CatalogValidationError(f"ttl_seconds must be between 1 and {self.max_lease_seconds}")
        live_epoch = self.epoch
        if catalog_epoch != live_epoch:
            raise StaleCatalog(f"catalog epoch is {live_epoch}, not {catalog_epoch}")
        descriptor = self.describe(capability_id, visibility_scopes=visibility_scopes, catalog_epoch=live_epoch)
        expected = (descriptor.revision_ref, descriptor.content_hash, descriptor.source_hash,
                    descriptor.schema_hash, descriptor.eligibility.policy_ref)
        actual = (revision_ref, _hash(descriptor_hash, "descriptor_hash"), _hash(source_hash, "source_hash"),
                  _hash(schema_hash, "schema_hash"), _ref(policy_ref, "policy_ref"))
        if actual != expected:
            raise StaleCatalog("descriptor revision, hash, schema, source, or policy is stale")
        asserted_policy_hash = _hash(policy_hash, "policy_hash")
        principal_ref = _ref(principal_ref, "principal_ref")
        if descriptor.eligibility.state == "auth_required":
            raise AuthorizationRequired(capability_id)
        if descriptor.eligibility.state != "ready":
            raise CapabilityNotEligible(capability_id)
        if capability_id not in work_order.requested_capabilities:
            raise PermissionDenied("WorkOrder did not request this capability ID")
        requested_effects = tuple(work_order.declared_side_effects)
        if not set(requested_effects).issubset(descriptor.permissions.side_effects):
            raise PermissionDenied("WorkOrder requests undeclared capability side effects")
        created_dt = self._time()
        policy = self._resolve_policy(
            policy_ref=descriptor.eligibility.policy_ref,
            principal_ref=principal_ref,
            at=created_dt,
        )
        if policy.content_hash != asserted_policy_hash:
            raise GovernancePolicyRejected("caller policy hash is not current authority")
        if not _permissions_subset(descriptor.permissions, policy.allowed_permissions):
            raise GovernancePolicyRejected("descriptor permissions exceed governance policy")
        if ttl_seconds > policy.max_lease_seconds:
            raise GovernancePolicyRejected("requested lease TTL exceeds governance policy")
        created_at = _timestamp(created_dt)
        expires_at = _timestamp(created_dt + timedelta(seconds=ttl_seconds))
        base = {"schema_version": SCHEMA_VERSION, "created_at": created_at, "expires_at": expires_at,
                "work_order_ref": work_order.id, "work_order_hash": canonical_hash(work_order.to_dict()),
                "capability_id": descriptor.id, "revision_ref": descriptor.revision_ref,
                "descriptor_hash": descriptor.content_hash, "source_hash": descriptor.source_hash,
                "schema_hash": descriptor.schema_hash, "catalog_epoch": live_epoch,
                "policy_ref": descriptor.eligibility.policy_ref, "policy_hash": policy.content_hash,
                "principal_ref": principal_ref, "permissions": descriptor.permissions.to_dict(),
                "allowed_side_effects": list(requested_effects),
                "credential_slot_refs": list(descriptor.permissions.credential_slot_refs)}
        digest = canonical_hash(base)
        wire = {**base, "id": f"lease:{digest}", "content_hash": digest}
        lease = CapabilityLease.from_dict(wire)
        try:
            self._conn.execute(
                "INSERT INTO capability_leases VALUES(?,?,?,?,?,?,?,?)",
                (lease.id, lease.capability_id, lease.revision_ref, lease.work_order_ref,
                 lease.expires_at, lease.content_hash, canonical_json(lease.to_dict()), lease.created_at),
            )
        except sqlite3.IntegrityError as exc:
            row = self._conn.execute("SELECT lease_json FROM capability_leases WHERE lease_id=?", (lease.id,)).fetchone()
            if row is None or json.loads(row[0]) != lease.to_dict():
                raise CatalogConflict("lease identity collision") from exc
        return lease

    def get_lease(self, lease_id: str) -> CapabilityLease:
        """Return an immutable historical record, not an authorization result.

        Every execution path must call :meth:`validate_lease_for_use`; reading a
        lease proves only that it was issued at some earlier point in time.
        """
        row = self._conn.execute("SELECT lease_json FROM capability_leases WHERE lease_id=?",
                                 (_string(lease_id, "lease_id"),)).fetchone()
        if row is None:
            raise CapabilityNotFound(lease_id)
        return CapabilityLease.from_dict(json.loads(row[0]))

    def validate_lease_for_use(
        self,
        lease_id: str,
        *,
        work_order: WorkOrder | Mapping[str, Any],
        principal_ref: str,
        permissions: Mapping[str, Any],
        side_effects: Sequence[str],
    ) -> CapabilityLease:
        """Fail closed unless a historical lease is usable *right now*.

        This method is a control-plane gate; it does not execute the capability
        or grant credentials.  Adapters must use its returned, revalidated
        lease rather than trusting a caller-supplied lease body.
        """

        lease = self.get_lease(lease_id)
        if not isinstance(work_order, WorkOrder):
            work_order = WorkOrder.from_dict(work_order)
        principal_ref = _ref(principal_ref, "principal_ref")
        now = self._time()
        if _parse_time(lease.expires_at, "lease.expires_at") <= now:
            raise LeaseNotUsable("capability lease has expired")
        if lease.catalog_epoch != self.epoch:
            raise LeaseNotUsable("capability lease catalog epoch is stale")
        row = self._conn.execute(
            "SELECT d.descriptor_json FROM capability_current c "
            "JOIN capability_descriptor_versions d ON d.revision_ref=c.revision_ref "
            "WHERE c.capability_id=?",
            (lease.capability_id,),
        ).fetchone()
        if row is None:
            raise LeaseNotUsable("capability is no longer current")
        descriptor = CapabilityDescriptor.from_dict(json.loads(row[0]))
        if (
            descriptor.revision_ref != lease.revision_ref
            or descriptor.content_hash != lease.descriptor_hash
            or descriptor.source_hash != lease.source_hash
            or descriptor.schema_hash != lease.schema_hash
        ):
            raise LeaseNotUsable("capability descriptor changed after lease issuance")
        if principal_ref != lease.principal_ref:
            raise LeaseNotUsable("capability lease belongs to another principal")
        if (
            work_order.id != lease.work_order_ref
            or canonical_hash(work_order.to_dict()) != lease.work_order_hash
            or lease.capability_id not in work_order.requested_capabilities
        ):
            raise LeaseNotUsable("capability lease belongs to another WorkOrder")
        asserted_permissions = CapabilityPermissions.from_dict(permissions)
        if asserted_permissions != lease.permissions:
            raise LeaseNotUsable("use-time permissions differ from the issued lease")
        actual_effects = _unique_strings(side_effects, "side_effects")
        if not set(actual_effects).issubset(lease.allowed_side_effects):
            raise LeaseNotUsable("use-time side effects exceed the issued lease")
        if not set(actual_effects).issubset(work_order.declared_side_effects):
            raise LeaseNotUsable("use-time side effects exceed the WorkOrder")
        policy = self._resolve_policy(
            policy_ref=lease.policy_ref, principal_ref=principal_ref, at=now
        )
        if policy.content_hash != lease.policy_hash:
            raise LeaseNotUsable("governance policy changed after lease issuance")
        if not _permissions_subset(lease.permissions, policy.allowed_permissions):
            raise LeaseNotUsable("lease permissions are no longer allowed")
        approval = self._resolve_approval(
            capability_id=descriptor.id,
            catalog_version=descriptor.version,
            source_ref=descriptor.source.source_ref,
            source_hash=descriptor.source_hash,
            schema_hash=descriptor.schema_hash,
            permissions=descriptor.permissions,
            at=now,
        )
        if approval.receipt_hash != descriptor.approval.receipt_hash:
            raise LeaseNotUsable("capability approval changed after lease issuance")
        return lease
