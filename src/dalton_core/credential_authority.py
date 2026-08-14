"""Credential authority boundary for future authenticated connectors.

Only grant metadata crosses into Core.  Credential values and host-owned MCP
authentication remain behind an injected authority implementation.  The
credential-free public HTTPS transport does not import or accept this API.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Protocol

from .store import content_hash


class CredentialBoundaryError(Exception):
    pass


class CredentialGrantRejected(CredentialBoundaryError, ValueError):
    pass


_REF_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+._-]*:[^\s]+$")
_OPERATION_RE = re.compile(r"^[a-z][a-z0-9._:-]{0,127}$")


def _text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise CredentialGrantRejected(f"{name} must be a non-empty string")
    return value


def _hash(value: Any, name: str) -> str:
    value = _text(value, name)
    if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
        raise CredentialGrantRejected(f"{name} must be lowercase SHA-256 hex")
    return value


def _ref(value: Any, name: str) -> str:
    value = _text(value, name)
    if not _REF_RE.fullmatch(value) or value.startswith(("file:", "path:", "http:", "https:")):
        raise CredentialGrantRejected(f"{name} must be an opaque namespaced ref")
    return value


def _timestamp(value: Any, name: str) -> str:
    value = _text(value, name)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CredentialGrantRejected(f"{name} must be RFC3339") from exc
    if parsed.tzinfo is None:
        raise CredentialGrantRejected(f"{name} must include timezone")
    return parsed.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _refs(value: Any, name: str, *, nonempty: bool = False) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise CredentialGrantRejected(f"{name} must be an array")
    result = tuple(_text(item, f"{name}[]") for item in value)
    if nonempty and not result:
        raise CredentialGrantRejected(f"{name} must not be empty")
    if len(result) != len(set(result)):
        raise CredentialGrantRejected(f"{name} must contain unique values")
    return result


@dataclass(frozen=True, slots=True)
class CredentialGrantEnvelope:
    schema_version: str
    id: str
    created_at: str
    expires_at: str
    authority_ref: str
    grant_kind: str
    target_ref: str
    connector_profile_ref: str
    connector_profile_hash: str
    capability_lease_ref: str
    capability_lease_hash: str
    adapter_ref: str
    adapter_hash: str
    principal_ref: str
    credential_slot_refs: tuple[str, ...]
    allowed_operations: tuple[str, ...]
    max_calls: int
    content_hash: str

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "CredentialGrantEnvelope":
        fields = {
            "schema_version", "id", "created_at", "expires_at", "authority_ref",
            "grant_kind", "target_ref", "connector_profile_ref",
            "connector_profile_hash", "capability_lease_ref", "capability_lease_hash",
            "adapter_ref", "adapter_hash", "principal_ref", "credential_slot_refs",
            "allowed_operations", "max_calls", "content_hash",
        }
        if not isinstance(value, Mapping) or set(value) != fields:
            raise CredentialGrantRejected("CredentialGrantEnvelope must have a closed shape")
        wire = dict(value)
        if wire["schema_version"] != "0.1":
            raise CredentialGrantRejected("unsupported credential grant schema_version")
        for name in (
            "id", "authority_ref", "target_ref", "connector_profile_ref",
            "capability_lease_ref", "adapter_ref", "principal_ref",
        ):
            wire[name] = _ref(wire[name], name)
        if wire["grant_kind"] not in {"mcp_managed", "https_credential"}:
            raise CredentialGrantRejected("grant_kind is invalid")
        created_at = _timestamp(wire["created_at"], "created_at")
        expires_at = _timestamp(wire["expires_at"], "expires_at")
        if expires_at <= created_at:
            raise CredentialGrantRejected("credential grant expiry must follow issuance")
        wire["created_at"] = created_at
        wire["expires_at"] = expires_at
        for name in (
            "connector_profile_hash", "capability_lease_hash", "adapter_hash",
            "content_hash",
        ):
            wire[name] = _hash(wire[name], name)
        slots = _refs(wire["credential_slot_refs"], "credential_slot_refs", nonempty=True)
        if any(not slot.startswith(("credential-slot:", "credential_slot:")) for slot in slots):
            raise CredentialGrantRejected("credential slots must be logical refs")
        operations = _refs(wire["allowed_operations"], "allowed_operations", nonempty=True)
        if any(not _OPERATION_RE.fullmatch(operation) for operation in operations):
            raise CredentialGrantRejected("allowed_operations must be canonical operation names")
        if isinstance(wire["max_calls"], bool) or not isinstance(wire["max_calls"], int) or wire["max_calls"] < 1:
            raise CredentialGrantRejected("max_calls must be a positive integer")
        declared_hash = wire.pop("content_hash")
        if content_hash(wire) != declared_hash:
            raise CredentialGrantRejected("credential grant content_hash mismatch")
        wire["content_hash"] = declared_hash
        return cls(
            schema_version="0.1",
            id=wire["id"],
            created_at=created_at,
            expires_at=expires_at,
            authority_ref=wire["authority_ref"],
            grant_kind=wire["grant_kind"],
            target_ref=wire["target_ref"],
            connector_profile_ref=wire["connector_profile_ref"],
            connector_profile_hash=wire["connector_profile_hash"],
            capability_lease_ref=wire["capability_lease_ref"],
            capability_lease_hash=wire["capability_lease_hash"],
            adapter_ref=wire["adapter_ref"],
            adapter_hash=wire["adapter_hash"],
            principal_ref=wire["principal_ref"],
            credential_slot_refs=slots,
            allowed_operations=operations,
            max_calls=wire["max_calls"],
            content_hash=declared_hash,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "id": self.id,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "authority_ref": self.authority_ref,
            "grant_kind": self.grant_kind,
            "target_ref": self.target_ref,
            "connector_profile_ref": self.connector_profile_ref,
            "connector_profile_hash": self.connector_profile_hash,
            "capability_lease_ref": self.capability_lease_ref,
            "capability_lease_hash": self.capability_lease_hash,
            "adapter_ref": self.adapter_ref,
            "adapter_hash": self.adapter_hash,
            "principal_ref": self.principal_ref,
            "credential_slot_refs": list(self.credential_slot_refs),
            "allowed_operations": list(self.allowed_operations),
            "max_calls": self.max_calls,
            "content_hash": self.content_hash,
        }


class CredentialHandle(Protocol):
    """Unserializable host-owned handle; implementations must expose no secret value."""


class CredentialAuthorityPort(Protocol):
    def validate_for_use(
        self,
        grant_ref: str,
        *,
        connector_profile_ref: str,
        connector_profile_hash: str,
        capability_lease_ref: str,
        capability_lease_hash: str,
        adapter_ref: str,
        adapter_hash: str,
        principal_ref: str,
        operation: str,
    ) -> tuple[CredentialGrantEnvelope, CredentialHandle]: ...


__all__ = [
    "CredentialAuthorityPort", "CredentialBoundaryError", "CredentialGrantEnvelope",
    "CredentialGrantRejected", "CredentialHandle",
]
