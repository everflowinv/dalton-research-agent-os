"""Credential authority boundary for future authenticated connectors.

Only grant metadata crosses into Core.  Credential values and host-owned MCP
authentication remain behind an injected authority implementation.  The
credential-free public HTTPS transport does not import or accept this API.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Protocol

from .store import canonical_json, content_hash


class CredentialBoundaryError(Exception):
    pass


class CredentialGrantRejected(CredentialBoundaryError, ValueError):
    pass


class CredentialHandle(Protocol):
    """Unserializable host-owned handle; implementations expose no value."""


_REF_RE = re.compile(
    r"^[A-Za-z][A-Za-z0-9+._-]*:[A-Za-z0-9][A-Za-z0-9:._-]*$"
)
_OPERATION_RE = re.compile(r"^[a-z][a-z0-9._:-]{0,127}$")
_SCHEMA_PATH = Path(__file__).with_name("credential_authority_schema.sql")
_REVOCATION_REASONS = frozenset(
    {"manual", "rotation", "permission_change", "security_incident", "source_disabled"}
)


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
    compact = re.sub(r"[^a-z0-9]+", "", value.lower())
    if (
        not _REF_RE.fullmatch(value)
        or value.startswith(("file:", "path:", "http:", "https:"))
        or any(
            marker in compact
            for marker in (
                "systemprompt", "apikey", "accesstoken", "refreshtoken",
                "password", "cookiematerial", "secretmaterial",
                "credentialvalue", "oauthconfig", "serverconfig",
            )
        )
    ):
        raise CredentialGrantRejected(f"{name} must be an opaque namespaced ref")
    return value


def _handle(value: Any) -> CredentialHandle:
    if value is None or isinstance(
        value,
        (
            bool, int, float, complex, str, bytes, bytearray, memoryview,
            Mapping, list, tuple, set, frozenset,
        ),
    ):
        raise CredentialGrantRejected(
            "credential handle must be an opaque host-owned object"
        )
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
        slots = tuple(
            _ref(item, "credential_slot_refs[]")
            for item in _refs(
                wire["credential_slot_refs"],
                "credential_slot_refs",
                nonempty=True,
            )
        )
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


@dataclass(frozen=True, slots=True)
class CredentialUseReceipt:
    schema_version: str
    id: str
    created_at: str
    grant_ref: str
    grant_hash: str
    runner_request_ref: str
    runner_request_hash: str
    connector_invocation_ref: str
    connector_invocation_hash: str
    reservation_ref: str
    reservation_hash: str
    physical_attempt_number: int
    operation: str
    content_hash: str

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "CredentialUseReceipt":
        fields = {
            "schema_version", "id", "created_at", "grant_ref", "grant_hash",
            "runner_request_ref", "runner_request_hash", "connector_invocation_ref",
            "connector_invocation_hash", "reservation_ref", "reservation_hash",
            "physical_attempt_number", "operation", "content_hash",
        }
        if not isinstance(value, Mapping) or set(value) != fields:
            raise CredentialGrantRejected("CredentialUseReceipt must have a closed shape")
        wire = dict(value)
        if wire["schema_version"] != "0.1":
            raise CredentialGrantRejected("unsupported credential use schema_version")
        for name in (
            "id", "grant_ref", "runner_request_ref", "connector_invocation_ref",
            "reservation_ref",
        ):
            wire[name] = _ref(wire[name], name)
        for name in (
            "grant_hash", "runner_request_hash", "connector_invocation_hash",
            "reservation_hash", "content_hash",
        ):
            wire[name] = _hash(wire[name], name)
        wire["created_at"] = _timestamp(wire["created_at"], "created_at")
        if (
            isinstance(wire["physical_attempt_number"], bool)
            or not isinstance(wire["physical_attempt_number"], int)
            or wire["physical_attempt_number"] < 1
        ):
            raise CredentialGrantRejected("physical_attempt_number must be positive")
        wire["operation"] = _text(wire["operation"], "operation")
        if not _OPERATION_RE.fullmatch(wire["operation"]):
            raise CredentialGrantRejected("operation is invalid")
        declared = wire.pop("content_hash")
        if content_hash(wire) != declared:
            raise CredentialGrantRejected("credential use content_hash mismatch")
        wire["content_hash"] = declared
        return cls(**wire)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "id": self.id,
            "created_at": self.created_at,
            "grant_ref": self.grant_ref,
            "grant_hash": self.grant_hash,
            "runner_request_ref": self.runner_request_ref,
            "runner_request_hash": self.runner_request_hash,
            "connector_invocation_ref": self.connector_invocation_ref,
            "connector_invocation_hash": self.connector_invocation_hash,
            "reservation_ref": self.reservation_ref,
            "reservation_hash": self.reservation_hash,
            "physical_attempt_number": self.physical_attempt_number,
            "operation": self.operation,
            "content_hash": self.content_hash,
        }


@dataclass(frozen=True, slots=True)
class CredentialRevocationEvent:
    schema_version: str
    id: str
    created_at: str
    grant_ref: str
    grant_hash: str
    reason_code: str
    actor_ref: str
    content_hash: str

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "CredentialRevocationEvent":
        fields = {
            "schema_version", "id", "created_at", "grant_ref", "grant_hash",
            "reason_code", "actor_ref", "content_hash",
        }
        if not isinstance(value, Mapping) or set(value) != fields:
            raise CredentialGrantRejected(
                "CredentialRevocationEvent must have a closed shape"
            )
        wire = dict(value)
        if wire["schema_version"] != "0.1":
            raise CredentialGrantRejected("unsupported revocation schema_version")
        for name in ("id", "grant_ref", "actor_ref"):
            wire[name] = _ref(wire[name], name)
        wire["grant_hash"] = _hash(wire["grant_hash"], "grant_hash")
        wire["created_at"] = _timestamp(wire["created_at"], "created_at")
        if wire["reason_code"] not in _REVOCATION_REASONS:
            raise CredentialGrantRejected("revocation reason is invalid")
        declared = _hash(wire.pop("content_hash"), "content_hash")
        if content_hash(wire) != declared:
            raise CredentialGrantRejected("revocation content_hash mismatch")
        wire["content_hash"] = declared
        return cls(**wire)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "id": self.id,
            "created_at": self.created_at,
            "grant_ref": self.grant_ref,
            "grant_hash": self.grant_hash,
            "reason_code": self.reason_code,
            "actor_ref": self.actor_ref,
            "content_hash": self.content_hash,
        }


@dataclass(frozen=True, slots=True)
class CredentialUseAuthorization:
    grant: CredentialGrantEnvelope
    receipt: CredentialUseReceipt
    handle: CredentialHandle


class CredentialAuthorityStore:
    """Durable grant/revoke/max-call authority without credential material."""

    def __init__(
        self,
        store: Any,
        *,
        handle_resolver: Callable[[CredentialGrantEnvelope], CredentialHandle],
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not hasattr(store, "connection") or not hasattr(store, "_transaction"):
            raise TypeError("CredentialAuthorityStore requires a DaltonStore")
        if not callable(handle_resolver):
            raise TypeError("credential handle resolver must be callable")
        self._store = store
        self._connection: sqlite3.Connection = store.connection
        self._handle_resolver = handle_resolver
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._connection.executescript(_SCHEMA_PATH.read_text(encoding="utf-8"))

    def _now(self) -> str:
        value = self._clock()
        if not isinstance(value, datetime) or value.tzinfo is None:
            raise CredentialGrantRejected("credential authority clock must be aware")
        return value.astimezone(timezone.utc).isoformat(timespec="microseconds")

    @staticmethod
    def _derived_id(prefix: str, idempotency_key: str) -> str:
        return f"{prefix}:{content_hash({'kind': prefix, 'key': idempotency_key})}"

    @staticmethod
    def _stored(row: sqlite3.Row | None, name: str) -> dict[str, Any]:
        if row is None:
            raise CredentialGrantRejected(f"{name} is missing")
        return json.loads(row["record_json"])

    def register_grant(
        self, spec: Mapping[str, Any], *, idempotency_key: str
    ) -> dict[str, Any]:
        grant = CredentialGrantEnvelope.from_dict(spec)
        now = self._now()
        if now < grant.created_at or now >= grant.expires_at:
            raise CredentialGrantRejected("credential grant is not currently effective")
        request_hash = grant.content_hash
        with self._store._transaction() as cur:
            duplicate = self._idempotent(
                cur, idempotency_key, "register_credential_grant", request_hash
            )
            if duplicate is not None:
                return duplicate
            row = cur.execute(
                "SELECT record_json,content_hash FROM credential_grants WHERE grant_id=?",
                (grant.id,),
            ).fetchone()
            if row is not None:
                if row["content_hash"] != grant.content_hash:
                    raise CredentialGrantRejected("credential grant id conflicts")
                result = {"write_status": "duplicate", **json.loads(row["record_json"])}
            else:
                wire = grant.to_dict()
                cur.execute(
                    "INSERT INTO credential_grants"
                    "(grant_id,record_json,content_hash,created_at,expires_at) VALUES(?,?,?,?,?)",
                    (
                        grant.id, canonical_json(wire), grant.content_hash,
                        grant.created_at, grant.expires_at,
                    ),
                )
                result = {"write_status": "fresh", **wire}
            self._save_idempotent(
                cur, idempotency_key, "register_credential_grant", request_hash, result
            )
            return result

    def revoke(
        self,
        grant_ref: str,
        *,
        grant_hash: str,
        reason_code: str,
        actor_ref: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        grant_ref = _ref(grant_ref, "grant_ref")
        grant_hash = _hash(grant_hash, "grant_hash")
        actor_ref = _ref(actor_ref, "actor_ref")
        base = {
            "schema_version": "0.1",
            "id": self._derived_id("credential-revocation", idempotency_key),
            "created_at": self._now(),
            "grant_ref": grant_ref,
            "grant_hash": grant_hash,
            "reason_code": reason_code,
            "actor_ref": actor_ref,
        }
        event = CredentialRevocationEvent.from_dict(
            {**base, "content_hash": content_hash(base)}
        )
        request_hash = event.content_hash
        with self._store._transaction() as cur:
            duplicate = self._idempotent(
                cur, idempotency_key, "revoke_credential_grant", request_hash
            )
            if duplicate is not None:
                return duplicate
            grant = self._grant_with_cursor(cur, grant_ref)
            if grant.content_hash != grant_hash:
                raise CredentialGrantRejected("credential grant hash is stale")
            prior = cur.execute(
                "SELECT record_json,content_hash FROM credential_revocation_events "
                "WHERE grant_ref=?",
                (grant_ref,),
            ).fetchone()
            if prior is not None:
                raise CredentialGrantRejected("credential grant is already revoked")
            wire = event.to_dict()
            cur.execute(
                "INSERT INTO credential_revocation_events"
                "(event_id,grant_ref,grant_hash,reason_code,actor_ref,record_json,content_hash,created_at) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (
                    event.id, event.grant_ref, event.grant_hash, event.reason_code,
                    event.actor_ref, canonical_json(wire), event.content_hash,
                    event.created_at,
                ),
            )
            result = {"write_status": "fresh", **wire}
            self._save_idempotent(
                cur, idempotency_key, "revoke_credential_grant", request_hash, result
            )
            return result

    def peek_for_use(self, **authority: Any) -> CredentialGrantEnvelope:
        now = self._now()
        with self._store._transaction() as cur:
            grant = self._select_grant_with_cursor(cur, now=now, **authority)
            used = int(
                cur.execute(
                    "SELECT COUNT(*) AS n FROM credential_use_receipts WHERE grant_ref=?",
                    (grant.id,),
                ).fetchone()["n"]
            )
            if used >= grant.max_calls:
                raise CredentialGrantRejected("credential grant max_calls is exhausted")
            return grant

    def authorize_use(
        self,
        *,
        runner_request_ref: str,
        runner_request_hash: str,
        connector_invocation_ref: str,
        connector_invocation_hash: str,
        reservation_ref: str,
        reservation_hash: str,
        physical_attempt_number: int,
        idempotency_key: str,
        **authority: Any,
    ) -> dict[str, Any]:
        now = self._now()
        with self._store._transaction() as cur:
            grant = self._select_grant_with_cursor(cur, now=now, **authority)
            request_hash = content_hash(
                {
                    "grant_ref": grant.id,
                    "grant_hash": grant.content_hash,
                    "runner_request_ref": runner_request_ref,
                    "runner_request_hash": runner_request_hash,
                    "connector_invocation_ref": connector_invocation_ref,
                    "connector_invocation_hash": connector_invocation_hash,
                    "reservation_ref": reservation_ref,
                    "reservation_hash": reservation_hash,
                    "physical_attempt_number": physical_attempt_number,
                    "operation": authority["operation"],
                }
            )
            duplicate = self._idempotent(
                cur, idempotency_key, "authorize_credential_use", request_hash
            )
            if duplicate is not None:
                return duplicate
            base = {
                "schema_version": "0.1",
                "id": self._derived_id("credential-use", idempotency_key),
                "created_at": now,
                "grant_ref": grant.id,
                "grant_hash": grant.content_hash,
                "runner_request_ref": runner_request_ref,
                "runner_request_hash": runner_request_hash,
                "connector_invocation_ref": connector_invocation_ref,
                "connector_invocation_hash": connector_invocation_hash,
                "reservation_ref": reservation_ref,
                "reservation_hash": reservation_hash,
                "physical_attempt_number": physical_attempt_number,
                "operation": authority["operation"],
            }
            receipt = CredentialUseReceipt.from_dict(
                {**base, "content_hash": content_hash(base)}
            )
            used = int(
                cur.execute(
                    "SELECT COUNT(*) AS n FROM credential_use_receipts WHERE grant_ref=?",
                    (grant.id,),
                ).fetchone()["n"]
            )
            if used >= grant.max_calls:
                raise CredentialGrantRejected("credential grant max_calls is exhausted")
            wire = receipt.to_dict()
            cur.execute(
                "INSERT INTO credential_use_receipts"
                "(use_id,grant_ref,grant_hash,runner_request_ref,runner_request_hash,"
                "connector_invocation_ref,connector_invocation_hash,reservation_ref,"
                "reservation_hash,physical_attempt_number,operation,record_json,content_hash,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    receipt.id, receipt.grant_ref, receipt.grant_hash,
                    receipt.runner_request_ref, receipt.runner_request_hash,
                    receipt.connector_invocation_ref, receipt.connector_invocation_hash,
                    receipt.reservation_ref, receipt.reservation_hash,
                    receipt.physical_attempt_number, receipt.operation,
                    canonical_json(wire), receipt.content_hash, receipt.created_at,
                ),
            )
            result = {"write_status": "fresh", **wire}
            self._save_idempotent(
                cur, idempotency_key, "authorize_credential_use", request_hash, result
            )
            return result

    def validate_use(
        self,
        use_ref: str,
        *,
        use_hash: str,
        runner_request_ref: str,
        runner_request_hash: str,
        connector_invocation_ref: str,
        connector_invocation_hash: str,
        reservation_ref: str,
        reservation_hash: str,
        physical_attempt_number: int,
        **authority: Any,
    ) -> CredentialUseAuthorization:
        use_ref = _ref(use_ref, "use_ref")
        use_hash = _hash(use_hash, "use_hash")
        now = self._now()
        with self._store._transaction() as cur:
            row = cur.execute(
                "SELECT record_json FROM credential_use_receipts WHERE use_id=?",
                (use_ref,),
            ).fetchone()
            receipt = CredentialUseReceipt.from_dict(
                self._stored(row, "credential use receipt")
            )
            expected = (
                use_hash, runner_request_ref, runner_request_hash,
                connector_invocation_ref, connector_invocation_hash,
                reservation_ref, reservation_hash, physical_attempt_number,
                authority["operation"],
            )
            actual = (
                receipt.content_hash, receipt.runner_request_ref,
                receipt.runner_request_hash, receipt.connector_invocation_ref,
                receipt.connector_invocation_hash, receipt.reservation_ref,
                receipt.reservation_hash, receipt.physical_attempt_number,
                receipt.operation,
            )
            if expected != actual:
                raise CredentialGrantRejected("credential use receipt authority drifted")
            grant = self._select_grant_with_cursor(
                cur, now=now, expected_grant_ref=receipt.grant_ref, **authority
            )
            if grant.content_hash != receipt.grant_hash:
                raise CredentialGrantRejected("credential use grant hash drifted")
        handle = _handle(self._handle_resolver(grant))
        return CredentialUseAuthorization(grant=grant, receipt=receipt, handle=handle)

    def get_use_receipt(self, use_ref: str) -> dict[str, Any]:
        row = self._connection.execute(
            "SELECT record_json FROM credential_use_receipts WHERE use_id=?",
            (_ref(use_ref, "use_ref"),),
        ).fetchone()
        return self._stored(row, "credential use receipt")

    def _select_grant_with_cursor(
        self,
        cur: sqlite3.Cursor,
        *,
        connector_profile_ref: str,
        connector_profile_hash: str,
        capability_lease_ref: str,
        capability_lease_hash: str,
        adapter_ref: str,
        adapter_hash: str,
        principal_ref: str,
        credential_slot_refs: list[str] | tuple[str, ...],
        operation: str,
        now: str,
        expected_grant_ref: str | None = None,
    ) -> CredentialGrantEnvelope:
        rows = cur.execute(
            "SELECT record_json FROM credential_grants ORDER BY grant_id"
        ).fetchall()
        matches: list[CredentialGrantEnvelope] = []
        for row in rows:
            grant = CredentialGrantEnvelope.from_dict(json.loads(row["record_json"]))
            if expected_grant_ref is not None and grant.id != expected_grant_ref:
                continue
            if (
                grant.grant_kind == "mcp_managed"
                and grant.connector_profile_ref == connector_profile_ref
                and grant.connector_profile_hash == connector_profile_hash
                and grant.capability_lease_ref == capability_lease_ref
                and grant.capability_lease_hash == capability_lease_hash
                and grant.target_ref == adapter_ref
                and grant.adapter_ref == adapter_ref
                and grant.adapter_hash == adapter_hash
                and grant.principal_ref == principal_ref
                and tuple(sorted(grant.credential_slot_refs))
                == tuple(
                    sorted(
                        _refs(
                            list(credential_slot_refs),
                            "credential_slot_refs",
                            nonempty=True,
                        )
                    )
                )
                and operation in grant.allowed_operations
                and grant.created_at <= now < grant.expires_at
                and cur.execute(
                    "SELECT 1 FROM credential_revocation_events WHERE grant_ref=?",
                    (grant.id,),
                ).fetchone() is None
            ):
                matches.append(grant)
        if len(matches) != 1:
            raise CredentialGrantRejected(
                "credential authority requires exactly one active exact grant"
            )
        return matches[0]

    def _grant_with_cursor(
        self, cur: sqlite3.Cursor, grant_ref: str
    ) -> CredentialGrantEnvelope:
        row = cur.execute(
            "SELECT record_json FROM credential_grants WHERE grant_id=?",
            (grant_ref,),
        ).fetchone()
        return CredentialGrantEnvelope.from_dict(self._stored(row, "credential grant"))

    @staticmethod
    def _idempotent(
        cur: sqlite3.Cursor, key: str, operation: str, request_hash: str
    ) -> dict[str, Any] | None:
        key = _text(key, "idempotency_key")
        row = cur.execute(
            "SELECT operation,request_hash,result_json FROM credential_idempotency_keys "
            "WHERE idempotency_key=?",
            (key,),
        ).fetchone()
        if row is None:
            return None
        if row["operation"] != operation or row["request_hash"] != request_hash:
            raise CredentialGrantRejected("credential idempotency key conflicts")
        result = json.loads(row["result_json"])
        result["write_status"] = "duplicate"
        return result

    def _save_idempotent(
        self,
        cur: sqlite3.Cursor,
        key: str,
        operation: str,
        request_hash: str,
        result: Mapping[str, Any],
    ) -> None:
        cur.execute(
            "INSERT INTO credential_idempotency_keys"
            "(idempotency_key,operation,request_hash,result_json,created_at) VALUES(?,?,?,?,?)",
            (
                _text(key, "idempotency_key"), operation, request_hash,
                canonical_json(result), self._now(),
            ),
        )


class CredentialAuthorityPort(Protocol):
    def peek_for_use(
        self, *,
        connector_profile_ref: str,
        connector_profile_hash: str,
        capability_lease_ref: str,
        capability_lease_hash: str,
        adapter_ref: str,
        adapter_hash: str,
        principal_ref: str,
        credential_slot_refs: list[str] | tuple[str, ...],
        operation: str,
    ) -> CredentialGrantEnvelope: ...

    def authorize_use(self, **authority: Any) -> dict[str, Any]: ...

    def validate_use(self, use_ref: str, **authority: Any) -> CredentialUseAuthorization: ...


__all__ = [
    "CredentialAuthorityPort", "CredentialAuthorityStore", "CredentialBoundaryError",
    "CredentialGrantEnvelope", "CredentialGrantRejected", "CredentialHandle",
    "CredentialRevocationEvent", "CredentialUseAuthorization", "CredentialUseReceipt",
]
