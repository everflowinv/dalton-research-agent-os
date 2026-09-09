"""P13ae: Guidepoint's identity and governance, one approval per operation.

Guidepoint is an expert-network library: transcripts of calls with people who
have worked in the industry.  It is the input the Deep Insight Gate asks for
and the one the research does not have -- filings say what a company reported,
and transcripts of operators say why.

The connector template has been packaged since the inventory was built; what
was missing is what makes a template usable: an identity bound to exactly one
operation, and an owner-approved governance record for it.

**Two operations, two capabilities, two approvals.**  ``search_library`` reads
an index; ``get_transcript`` reads a document.  P9d-1 split AlphaEngine the
same way and for the same reason: a schema hash binds one operation, so one
record cannot be reused for the other without silently widening what was
approved.  Approving the search is not approving the reading.

Credential-free by construction: the transport is host-owned MCP, the template
declares ``credential_material: forbidden``, and the permissions below name a
credential *slot* rather than any material.  Nothing here reads a secret.
"""

from __future__ import annotations

import copy
from datetime import datetime
from typing import Any

from .connector_inventory import load_packaged_connector_inventory
from .store import content_hash

TEMPLATE_KEY = "guidepoint"
SEARCH_OPERATION = "search_library"
TRANSCRIPT_OPERATION = "get_transcript"
OPERATIONS = (SEARCH_OPERATION, TRANSCRIPT_OPERATION)

SEARCH_KIND = "guidepoint-search-library"
TRANSCRIPT_KIND = "guidepoint-get-transcript"
KIND_BY_OPERATION = {
    SEARCH_OPERATION: SEARCH_KIND,
    TRANSCRIPT_OPERATION: TRANSCRIPT_KIND,
}
SEARCH_CAPABILITY_ID = "capability:dalton:connector:guidepoint-search-library"
TRANSCRIPT_CAPABILITY_ID = "capability:dalton:connector:guidepoint-get-transcript"
CAPABILITY_BY_OPERATION = {
    SEARCH_OPERATION: SEARCH_CAPABILITY_ID,
    TRANSCRIPT_OPERATION: TRANSCRIPT_CAPABILITY_ID,
}

GOVERNANCE_SCHEMA_VERSION = "0.1"
CREDENTIAL_SLOT_REF = "credential-slot:guidepoint"
SIDE_EFFECT = "read:guidepoint-library"


class GuidepointError(RuntimeError):
    """The Guidepoint identity or governance record is invalid."""


def _operation(operation: str) -> str:
    if operation not in KIND_BY_OPERATION:
        raise GuidepointError(f"Guidepoint has no frozen {operation!r} operation")
    return operation


def guidepoint_contract(operation: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """The packaged template and its frozen operation contract."""

    operation = _operation(operation)
    template = load_packaged_connector_inventory()["templates"][TEMPLATE_KEY]
    matches = [item for item in template["operations"]
               if item["operation"] == operation]
    if len(matches) != 1:
        raise GuidepointError(
            f"packaged Guidepoint template lacks the frozen {operation} operation"
        )
    return template, matches[0]


def guidepoint_source_hash() -> str:
    """Shared by both operations: the same Guidepoint is the same source."""

    template, _ = guidepoint_contract(SEARCH_OPERATION)
    return content_hash(dict(template["source_identity"]))


def guidepoint_schema_hash(operation: str) -> str:
    """Bound to one operation alone, so an approval cannot widen to the other."""

    _, contract = guidepoint_contract(operation)
    return content_hash({
        "allowed_operations": [operation],
        "input_schema_refs": {operation: contract["input_schema_ref"]},
        "input_schema_hashes": {operation: contract["input_schema_hash"]},
        "output_schema_refs": {operation: contract["output_schema_ref"]},
        "output_schema_hashes": {operation: contract["output_schema_hash"]},
    })


def guidepoint_adapter_hash(operation: str) -> str:
    template, _ = guidepoint_contract(operation)
    return content_hash({
        "target_ref": template["transport"]["target_ref"],
        "source": template["source_identity"]["source_ref"],
        "operation": operation,
    })


def guidepoint_identity(operation: str) -> dict[str, Any]:
    """Source and schema identity of exactly one Guidepoint operation."""

    template, contract = guidepoint_contract(operation)
    return {
        "capability_id": CAPABILITY_BY_OPERATION[operation],
        "source_identity": dict(template["source_identity"]),
        "source_hash": guidepoint_source_hash(),
        "schema_hash": guidepoint_schema_hash(operation),
        "adapter_ref": template["transport"]["target_ref"],
        "adapter_hash": guidepoint_adapter_hash(operation),
        "operation": operation,
        "allowed_operations": [operation],
        "input_schema_ref": contract["input_schema_ref"],
        "input_schema_hash": contract["input_schema_hash"],
        "output_schema_ref": contract["output_schema_ref"],
        "output_schema_hash": contract["output_schema_hash"],
        "input_schema_refs": {operation: contract["input_schema_ref"]},
        "input_schema_hashes": {operation: contract["input_schema_hash"]},
        "output_schema_refs": {operation: contract["output_schema_ref"]},
        "output_schema_hashes": {operation: contract["output_schema_hash"]},
    }


def guidepoint_permissions() -> dict[str, Any]:
    """Permissions of the host-owned, read-only Guidepoint MCP capability.

    Identical in shape to AlphaEngine's, because the boundary is identical: no
    network of our own, no Core access, one credential *slot* the host owns and
    this process never reads, and the only write is into the raw sink where
    every acquired byte is hashed before anything reads it.
    """

    return {
        "risk_class": "low",
        "network": False,
        "filesystem_read": [],
        "filesystem_write": ["runner:raw-sink"],
        "credential_slot_refs": [CREDENTIAL_SLOT_REF],
        "core_db": False,
        "side_effects": [SIDE_EFFECT],
    }


def guidepoint_fixture_hash() -> str:
    template, _ = guidepoint_contract(SEARCH_OPERATION)
    return template["fixture_manifest_hash"]


def _wire_time(value: datetime) -> str:
    return value.astimezone(value.tzinfo).isoformat(timespec="microseconds")


def build_guidepoint_governance_record(
    *,
    operation: str,
    approved_by: str,
    status: str = "proposed",
    effective_from: str = "2026-09-09T00:00:00+00:00",
    max_lease_seconds: int = 120,
    version: int = 1,
) -> dict[str, Any]:
    """Closed, hash-bound governance record for one Guidepoint operation."""

    operation = _operation(operation)
    if status not in {"proposed", "approved"}:
        raise GuidepointError("governance status must be proposed or approved")
    if not isinstance(approved_by, str) or not approved_by.startswith("human:"):
        raise GuidepointError("approved_by must be a human: principal")
    kind = KIND_BY_OPERATION[operation]
    capability_id = CAPABILITY_BY_OPERATION[operation]
    base = {
        "schema_version": GOVERNANCE_SCHEMA_VERSION,
        "id": f"connector-governance:{kind}:v{version}",
        "status": status,
        "capability_id": capability_id,
        "approved_by": approved_by,
        "principal_ref": "principal:dalton-core-trusted-runner",
        "policy_ref": f"policy:dalton:connector-governance:{kind}:v{version}",
        "approval_ref": f"approval:connector-governance:{kind}:v{version}",
        "decision_ref": f"capability-decision:connector-governance:{kind}:v{version}",
        "registry_revision_ref": f"{capability_id}@v{version}",
        "attestation_ref": f"attestation:connector-governance:{kind}:v{version}",
        "effective_from": _wire_time(datetime.fromisoformat(effective_from)),
        "effective_until": None,
        "max_lease_seconds": max_lease_seconds,
        "allowed_permissions": copy.deepcopy(guidepoint_permissions()),
        "expected_source_hash": guidepoint_source_hash(),
        "expected_schema_hash": guidepoint_schema_hash(operation),
    }
    base["content_hash"] = content_hash(base)
    return base


__all__ = [
    "CAPABILITY_BY_OPERATION",
    "CREDENTIAL_SLOT_REF",
    "GuidepointError",
    "KIND_BY_OPERATION",
    "OPERATIONS",
    "SEARCH_CAPABILITY_ID",
    "SEARCH_KIND",
    "SEARCH_OPERATION",
    "SIDE_EFFECT",
    "TRANSCRIPT_CAPABILITY_ID",
    "TRANSCRIPT_KIND",
    "TRANSCRIPT_OPERATION",
    "build_guidepoint_governance_record",
    "guidepoint_adapter_hash",
    "guidepoint_contract",
    "guidepoint_fixture_hash",
    "guidepoint_identity",
    "guidepoint_permissions",
    "guidepoint_schema_hash",
    "guidepoint_source_hash",
]
