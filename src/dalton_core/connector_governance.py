"""Generic, hash-bound governance for connector capabilities.

The governance record intentionally has no ``kind`` field.  Its closed wire
shape predates this module, so the connector kind is resolved from the
record's capability id against the registry below.  This keeps existing
AlphaEngine records byte-compatible while allowing the same owner approval
boundary to govern other connectors.
"""

from __future__ import annotations

import copy
import json
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Callable

from .capability_catalog import CapabilityPermissions, canonical_hash
from .connector_inventory import load_packaged_connector_inventory
from .store import canonical_json, content_hash


GOVERNANCE_SCHEMA_VERSION = "0.1"
GOVERNANCE_FIELDS = frozenset({
    "schema_version", "id", "status", "capability_id", "approved_by",
    "principal_ref", "policy_ref", "approval_ref", "decision_ref",
    "registry_revision_ref", "attestation_ref", "effective_from",
    "effective_until", "max_lease_seconds", "allowed_permissions",
    "expected_source_hash", "expected_schema_hash", "content_hash",
})
GOVERNANCE_STATUSES = frozenset({"proposed", "approved"})

ALPHAENGINE_KIND = "alphaengine-get-document"
ALPHAENGINE_SEARCH_KIND = "alphaengine-search-library"
SEC_COMPANY_FACTS_KIND = "sec-company-facts"
ALPHAENGINE_CAPABILITY_ID = (
    "capability:dalton:connector:alphaengine-get-document"
)
ALPHAENGINE_SEARCH_CAPABILITY_ID = (
    "capability:dalton:connector:alphaengine-search-library"
)
SEC_CAPABILITY_ID = "capability:dalton:connector:sec-edgar"


class ConnectorGovernanceError(RuntimeError):
    """A malformed, unknown, or inactive connector governance record."""


class _KindSpec:
    """Lazy kind-specific identity and authority functions.

    The AlphaEngine functions live in the legacy acquisition module, which
    imports this module for its compatibility class.  Lazy callbacks avoid a
    module cycle while still making the registry the single dispatch table.
    """

    def __init__(
        self,
        *,
        capability_id: str,
        template_key: str,
        source_hash: Callable[[], str],
        schema_hash: Callable[[], str],
        permissions: Callable[[], dict[str, Any]],
        fixture_hash: Callable[[], str],
    ) -> None:
        self.capability_id = capability_id
        self.template_key = template_key
        self.source_hash = source_hash
        self.schema_hash = schema_hash
        self.permissions = permissions
        self.fixture_hash = fixture_hash


def _alpha_source_hash() -> str:
    from .alphaengine_core_acquisition import alphaengine_source_hash

    return alphaengine_source_hash()


def _alpha_schema_hash() -> str:
    from .alphaengine_core_acquisition import alphaengine_get_document_schema_hash

    return alphaengine_get_document_schema_hash()


def _alpha_permissions() -> dict[str, Any]:
    from .alphaengine_core_acquisition import live_alphaengine_permissions

    return copy.deepcopy(live_alphaengine_permissions())


def _alpha_fixture_hash() -> str:
    template = load_packaged_connector_inventory()["templates"]["alphaengine"]
    return template["fixture_manifest_hash"]


def _alpha_search_schema_hash() -> str:
    from .alphaengine_core_search import alphaengine_search_schema_hash

    return alphaengine_search_schema_hash()


def _sec_identity() -> dict[str, Any]:
    from .research_plan_executor import sec_connector_identity

    inventory = load_packaged_connector_inventory()
    return sec_connector_identity(inventory["templates"]["sec"], "get_company_facts")


def _sec_source_hash() -> str:
    return _sec_identity()["source_hash"]


def _sec_schema_hash() -> str:
    return _sec_identity()["schema_hash"]


def _sec_permissions() -> dict[str, Any]:
    # Keep this a deep copy: callers must not be able to mutate the harness'
    # frozen public permission declaration through a governance object.
    from .sec_authority_harness import PUBLIC_PERMISSIONS

    return copy.deepcopy(PUBLIC_PERMISSIONS)


def _sec_fixture_hash() -> str:
    return load_packaged_connector_inventory()["templates"]["sec"][
        "fixture_manifest_hash"
    ]


# Capability id is deliberately the dispatch key at load time because it is
# the only kind identity present in the closed governance record.
GOVERNANCE_KIND_REGISTRY: dict[str, _KindSpec] = {
    ALPHAENGINE_KIND: _KindSpec(
        capability_id=ALPHAENGINE_CAPABILITY_ID,
        template_key="alphaengine",
        source_hash=_alpha_source_hash,
        schema_hash=_alpha_schema_hash,
        permissions=_alpha_permissions,
        fixture_hash=_alpha_fixture_hash,
    ),
    # P9d-1: the same AlphaEngine template's ``search_library`` operation is a
    # separate capability with its own approval; the get_document record
    # cannot be reused because its schema hash binds one operation only.
    ALPHAENGINE_SEARCH_KIND: _KindSpec(
        capability_id=ALPHAENGINE_SEARCH_CAPABILITY_ID,
        template_key="alphaengine",
        source_hash=_alpha_source_hash,
        schema_hash=_alpha_search_schema_hash,
        permissions=_alpha_permissions,
        fixture_hash=_alpha_fixture_hash,
    ),
    SEC_COMPANY_FACTS_KIND: _KindSpec(
        capability_id=SEC_CAPABILITY_ID,
        template_key="sec",
        source_hash=_sec_source_hash,
        schema_hash=_sec_schema_hash,
        permissions=_sec_permissions,
        fixture_hash=_sec_fixture_hash,
    ),
}
# Public aliases make the registry discoverable without exposing mutable
# implementation details of a spec.  The old name is useful to callers that
# treat the set as a connector-kind catalog.
CONNECTOR_GOVERNANCE_KINDS = GOVERNANCE_KIND_REGISTRY
GOVERNANCE_KINDS = GOVERNANCE_KIND_REGISTRY

_CAPABILITY_TO_KIND = {
    spec.capability_id: kind for kind, spec in GOVERNANCE_KIND_REGISTRY.items()
}


def governance_kind_for_capability(capability_id: str) -> str:
    try:
        return _CAPABILITY_TO_KIND[capability_id]
    except (KeyError, TypeError) as exc:
        raise ConnectorGovernanceError(
            "governance capability_id is not a registered connector"
        ) from exc


def _kind_spec(kind: str) -> _KindSpec:
    try:
        return GOVERNANCE_KIND_REGISTRY[kind]
    except (KeyError, TypeError) as exc:
        raise ConnectorGovernanceError(f"unknown connector governance kind: {kind}") from exc


def _wire_time(value: str) -> str:
    from datetime import datetime, timezone

    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError, AttributeError) as exc:
        raise ConnectorGovernanceError("effective_from must be RFC3339") from exc
    if parsed.tzinfo is None:
        raise ConnectorGovernanceError("effective_from must include a timezone")
    return parsed.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _with_hash(value: Mapping[str, Any]) -> dict[str, Any]:
    wire = json.loads(canonical_json(value))
    wire["content_hash"] = content_hash(wire)
    return wire


def build_governance_record(
    kind: str,
    *,
    approved_by: str,
    status: str = "proposed",
    effective_from: str = "2026-08-26T00:00:00+00:00",
    max_lease_seconds: int = 120,
    version: int = 1,
) -> dict[str, Any]:
    """Build one closed governance record for a registered connector kind.

    AlphaEngine delegates to its original builder intentionally: this is the
    compatibility promise that old live records remain byte-for-byte stable.
    """

    _kind_spec(kind)
    if kind == ALPHAENGINE_KIND:
        from .alphaengine_core_acquisition import (
            build_governance_record as build_alphaengine_governance_record,
        )

        return build_alphaengine_governance_record(
            approved_by=approved_by,
            status=status,
            effective_from=effective_from,
            max_lease_seconds=max_lease_seconds,
            version=version,
        )

    if kind == ALPHAENGINE_SEARCH_KIND:
        from .alphaengine_core_search import build_search_governance_record

        return build_search_governance_record(
            approved_by=approved_by,
            status=status,
            effective_from=effective_from,
            max_lease_seconds=max_lease_seconds,
            version=version,
        )

    if kind != SEC_COMPANY_FACTS_KIND:  # registry guard; defensive for future kinds
        raise ConnectorGovernanceError(f"unsupported connector governance kind: {kind}")
    spec = GOVERNANCE_KIND_REGISTRY[kind]
    base = {
        "schema_version": GOVERNANCE_SCHEMA_VERSION,
        "id": f"connector-governance:sec-company-facts:v{version}",
        "status": status,
        "capability_id": spec.capability_id,
        "approved_by": approved_by,
        "principal_ref": "principal:dalton-core-trusted-runner",
        "policy_ref": f"policy:dalton:connector-governance:sec-company-facts:v{version}",
        "approval_ref": f"approval:connector-governance:sec-company-facts:v{version}",
        "decision_ref": f"capability-decision:connector-governance:sec-company-facts:v{version}",
        "registry_revision_ref": f"{spec.capability_id}@v{version}",
        "attestation_ref": f"attestation:connector-governance:sec-company-facts:v{version}",
        "effective_from": _wire_time(effective_from),
        "effective_until": None,
        "max_lease_seconds": max_lease_seconds,
        "allowed_permissions": spec.permissions(),
        "expected_source_hash": spec.source_hash(),
        "expected_schema_hash": spec.schema_hash(),
    }
    return _with_hash(base)


class ConnectorGovernance:
    """Static generic approval and policy authority for a connector record."""

    def __init__(self, value: Mapping[str, Any]) -> None:
        if not isinstance(value, Mapping) or set(value) != set(GOVERNANCE_FIELDS):
            raise ConnectorGovernanceError(
                "connector governance record has an invalid closed shape"
            )
        wire = json.loads(canonical_json(value))
        if wire["schema_version"] != GOVERNANCE_SCHEMA_VERSION:
            raise ConnectorGovernanceError("unsupported governance schema_version")
        if wire["status"] not in GOVERNANCE_STATUSES:
            raise ConnectorGovernanceError("governance status is invalid")
        kind = governance_kind_for_capability(wire["capability_id"])
        spec = GOVERNANCE_KIND_REGISTRY[kind]
        approved_by = wire["approved_by"]
        if (
            not isinstance(approved_by, str)
            or not approved_by.startswith("human:")
            or len(approved_by) <= 6
        ):
            raise ConnectorGovernanceError("governance approved_by must be a human actor")
        for name in (
            "id", "principal_ref", "policy_ref", "approval_ref", "decision_ref",
            "registry_revision_ref", "attestation_ref",
        ):
            if not isinstance(wire[name], str) or ":" not in wire[name]:
                raise ConnectorGovernanceError(f"governance {name} must be a namespaced ref")
        self._parse_time(wire["effective_from"], "effective_from")
        if wire["effective_until"] is not None:
            until = self._parse_time(wire["effective_until"], "effective_until")
            if until <= self._parse_time(wire["effective_from"], "effective_from"):
                raise ConnectorGovernanceError("governance interval is invalid")
        lease = wire["max_lease_seconds"]
        if isinstance(lease, bool) or not isinstance(lease, int) or lease < 1:
            raise ConnectorGovernanceError("max_lease_seconds must be a positive integer")
        try:
            CapabilityPermissions.from_dict(wire["allowed_permissions"])
        except Exception as exc:
            raise ConnectorGovernanceError("governance allowed_permissions are invalid") from exc
        for name in ("expected_source_hash", "expected_schema_hash"):
            if not isinstance(wire[name], str) or len(wire[name]) != 64:
                raise ConnectorGovernanceError(f"governance {name} must be SHA-256 hex")
        declared = wire.pop("content_hash")
        if content_hash(wire) != declared:
            raise ConnectorGovernanceError("governance content_hash mismatch")
        wire["content_hash"] = declared
        self.wire = wire
        self._kind = kind
        self._spec = spec

    @staticmethod
    def _parse_time(value: str, name: str):
        from datetime import datetime, timezone

        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except (TypeError, ValueError, AttributeError) as exc:
            raise ConnectorGovernanceError(f"{name} must be RFC3339") from exc
        if parsed.tzinfo is None:
            raise ConnectorGovernanceError(f"{name} must include a timezone")
        return parsed.astimezone(timezone.utc)

    @classmethod
    def load(cls, path: str | Path) -> "ConnectorGovernance":
        return cls(json.loads(Path(path).read_text(encoding="utf-8")))

    @property
    def kind(self) -> str:
        return self._kind

    @property
    def capability_id(self) -> str:
        return self.wire["capability_id"]

    @property
    def id(self) -> str:
        return self.wire["id"]

    @property
    def content_hash(self) -> str:
        return self.wire["content_hash"]

    @property
    def status(self) -> str:
        return self.wire["status"]

    @property
    def approved(self) -> bool:
        return self.status == "approved"

    @property
    def principal_ref(self) -> str:
        return self.wire["principal_ref"]

    @property
    def policy_ref(self) -> str:
        return self.wire["policy_ref"]

    @property
    def approved_by(self) -> str:
        return self.wire["approved_by"]

    @property
    def effective_from(self) -> str:
        return self.wire["effective_from"]

    @property
    def allowed_permissions(self) -> dict[str, Any]:
        return copy.deepcopy(self.wire["allowed_permissions"])

    def _require_approved(self) -> None:
        if not self.approved:
            raise ConnectorGovernanceError(
                f"connector governance {self.id} is {self.status}; owner approval is required"
            )

    def approval(self, query: Mapping[str, Any]) -> dict[str, Any] | None:
        """Resolve a hash-bound approval receipt for the registered kind."""

        self._require_approved()
        if (
            query.get("capability_id") != self.capability_id
            or query.get("source_hash") != self.wire["expected_source_hash"]
            or query.get("schema_hash") != self.wire["expected_schema_hash"]
        ):
            return None
        receipt = {
            "schema_version": "0.1",
            "approval_ref": self.wire["approval_ref"],
            "capability_id": self.capability_id,
            "registry_revision_ref": self.wire["registry_revision_ref"],
            "artifact_ref": query["source_ref"],
            "artifact_hash": query["source_hash"],
            "schema_hash": query["schema_hash"],
            "fixture_manifest_hash": self._spec.fixture_hash(),
            "attestation_ref": self.wire["attestation_ref"],
            "attestation_hash": content_hash(
                {"governance_id": self.id, "governance_hash": self.content_hash}
            ),
            "decision_ref": self.wire["decision_ref"],
            "decision": "approve",
            "approved_by": self.approved_by,
            "approved_permissions": self.allowed_permissions,
            "active": True,
            "effective_from": self.wire["effective_from"],
            "effective_until": self.wire["effective_until"],
        }
        receipt["receipt_hash"] = canonical_hash(receipt)
        return receipt

    def policy(self, query: Mapping[str, Any]) -> dict[str, Any] | None:
        """Resolve the hash-bound lease policy for the registered kind."""

        self._require_approved()
        if query.get("policy_ref") != self.policy_ref:
            return None
        wire = {
            "schema_version": "0.1",
            "policy_ref": self.policy_ref,
            "effective_from": self.wire["effective_from"],
            "effective_until": self.wire["effective_until"],
            "allowed_principal_refs": [self.principal_ref],
            "allowed_permissions": self.allowed_permissions,
            "max_lease_seconds": self.wire["max_lease_seconds"],
        }
        wire["content_hash"] = canonical_hash(wire)
        return wire

    def policy_hash(self) -> str:
        policy = self.policy({"policy_ref": self.policy_ref})
        assert policy is not None
        return policy["content_hash"]


def load_connector_governance(path: str | Path) -> ConnectorGovernance:
    """Load a generic governance record and dispatch its registered kind."""

    return ConnectorGovernance.load(path)


def write_governance_proposal(
    path: str | Path,
    *,
    kind: str,
    approved_by: str,
    effective_from: str = "2026-08-26T00:00:00+00:00",
    max_lease_seconds: int = 120,
    version: int = 1,
) -> dict[str, Any]:
    """Create a proposed record without overwriting an existing file."""

    target = Path(path)
    if target.exists():
        raise FileExistsError(str(target))
    record = build_governance_record(
        kind,
        approved_by=approved_by,
        status="proposed",
        effective_from=effective_from,
        max_lease_seconds=max_lease_seconds,
        version=version,
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive creation closes the check/write race and keeps owner files
    # private from the moment they are created.
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(canonical_json(record) + "\n")
    except BaseException:
        try:
            os.close(fd)
        except OSError:
            pass
        raise
    return record


__all__ = [
    "ALPHAENGINE_CAPABILITY_ID", "ALPHAENGINE_KIND", "CONNECTOR_GOVERNANCE_KINDS",
    "ConnectorGovernance", "ConnectorGovernanceError", "GOVERNANCE_FIELDS",
    "GOVERNANCE_KIND_REGISTRY", "GOVERNANCE_KINDS", "GOVERNANCE_SCHEMA_VERSION",
    "SEC_CAPABILITY_ID", "SEC_COMPANY_FACTS_KIND", "build_governance_record",
    "governance_kind_for_capability", "load_connector_governance",
    "write_governance_proposal",
]
