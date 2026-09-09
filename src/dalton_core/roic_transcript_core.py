"""P13ah: earnings call transcripts from roic.ai, as a second independent source.

The Playbook's Initial Screen asks for four quarters of calls, and the Deep
Insight Gate rests on what operators actually said. AlphaEngine carries
transcripts and is capped at 130 calls a day. Live, CTSH sat at one call of the
four it needs with that cap exhausted, and its screen could not be rewritten --
one source being busy stopped a company.

So this is the same requirement served a second way. Public web, no credential:
a transcript is a page on roic.ai.

Two operations, two capabilities, two approvals -- ``list_transcripts`` says
what exists, ``get_transcript`` reads one. Same split as AlphaEngine (P9d-1),
SEC (P10e) and Guidepoint (P13ae), for the same reason: a schema hash binds one
operation, so one record cannot be reused for the other without widening what
was approved.

Note for whoever generalises this: it is the third module of this exact shape,
which is the point at which a shared descriptor would be worth more than a
fourth copy. It was left alone here because two of the three already sit behind
owner-approved records whose hashes must not move, and that refactor deserves
its own pass with those hashes pinned as the test.
"""

from __future__ import annotations

import copy
from datetime import datetime
from typing import Any

from .connector_inventory import load_packaged_connector_inventory
from .store import content_hash

TEMPLATE_KEY = "roic-transcript"
LIST_OPERATION = "list_transcripts"
GET_OPERATION = "get_transcript"
OPERATIONS = (LIST_OPERATION, GET_OPERATION)

LIST_KIND = "roic-list-transcripts"
GET_KIND = "roic-get-transcript"
KIND_BY_OPERATION = {LIST_OPERATION: LIST_KIND, GET_OPERATION: GET_KIND}
LIST_CAPABILITY_ID = "capability:dalton:connector:roic-list-transcripts"
GET_CAPABILITY_ID = "capability:dalton:connector:roic-get-transcript"
CAPABILITY_BY_OPERATION = {
    LIST_OPERATION: LIST_CAPABILITY_ID,
    GET_OPERATION: GET_CAPABILITY_ID,
}

GOVERNANCE_SCHEMA_VERSION = "0.1"
SIDE_EFFECT = "read:public-http"


class RoicTranscriptError(RuntimeError):
    """The roic transcript identity or governance record is invalid."""


def _operation(operation: str) -> str:
    if operation not in KIND_BY_OPERATION:
        raise RoicTranscriptError(f"roic has no frozen {operation!r} operation")
    return operation


def roic_contract(operation: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """The packaged template and its frozen operation contract."""

    operation = _operation(operation)
    template = load_packaged_connector_inventory()["templates"][TEMPLATE_KEY]
    matches = [item for item in template["operations"]
               if item["operation"] == operation]
    if len(matches) != 1:
        raise RoicTranscriptError(
            f"packaged roic template lacks the frozen {operation} operation"
        )
    return template, matches[0]


def roic_source_hash() -> str:
    """Shared by both operations: one roic.ai is one source."""

    template, _ = roic_contract(LIST_OPERATION)
    return content_hash(dict(template["source_identity"]))


def roic_schema_hash(operation: str) -> str:
    """Bound to one operation alone, so an approval cannot widen to the other."""

    _, contract = roic_contract(operation)
    return content_hash({
        "allowed_operations": [operation],
        "input_schema_refs": {operation: contract["input_schema_ref"]},
        "input_schema_hashes": {operation: contract["input_schema_hash"]},
        "output_schema_refs": {operation: contract["output_schema_ref"]},
        "output_schema_hashes": {operation: contract["output_schema_hash"]},
    })


def roic_adapter_hash(operation: str) -> str:
    template, _ = roic_contract(operation)
    return content_hash({
        "target_ref": template["transport"]["target_ref"],
        "source": template["source_identity"]["source_ref"],
        "operation": operation,
    })


def roic_identity(operation: str) -> dict[str, Any]:
    """Source and schema identity of exactly one roic operation."""

    template, contract = roic_contract(operation)
    return {
        "capability_id": CAPABILITY_BY_OPERATION[operation],
        "source_identity": dict(template["source_identity"]),
        "source_hash": roic_source_hash(),
        "schema_hash": roic_schema_hash(operation),
        "adapter_ref": template["transport"]["target_ref"],
        "adapter_hash": roic_adapter_hash(operation),
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


def roic_permissions() -> dict[str, Any]:
    """Credential-free public HTTPS, and the raw sink.

    The same declaration the other public-web lanes carry. Which host it may
    reach is the template's allowlist -- one host, roic.ai -- not this shape,
    which has no field for it.
    """

    from .sec_authority_harness import PUBLIC_PERMISSIONS

    return copy.deepcopy(PUBLIC_PERMISSIONS)


def roic_fixture_hash() -> str:
    template, _ = roic_contract(LIST_OPERATION)
    return template["fixture_manifest_hash"]


def _wire_time(value: datetime) -> str:
    return value.astimezone(value.tzinfo).isoformat(timespec="microseconds")


def build_roic_governance_record(
    *,
    operation: str,
    approved_by: str,
    status: str = "proposed",
    effective_from: str = "2026-09-09T00:00:00+00:00",
    max_lease_seconds: int = 120,
    version: int = 1,
) -> dict[str, Any]:
    """Closed, hash-bound governance record for one roic operation."""

    operation = _operation(operation)
    if status not in {"proposed", "approved"}:
        raise RoicTranscriptError("governance status must be proposed or approved")
    if not isinstance(approved_by, str) or not approved_by.startswith("human:"):
        raise RoicTranscriptError("approved_by must be a human: principal")
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
        "allowed_permissions": copy.deepcopy(roic_permissions()),
        "expected_source_hash": roic_source_hash(),
        "expected_schema_hash": roic_schema_hash(operation),
    }
    base["content_hash"] = content_hash(base)
    return base


__all__ = [
    "CAPABILITY_BY_OPERATION",
    "GET_CAPABILITY_ID",
    "GET_KIND",
    "GET_OPERATION",
    "KIND_BY_OPERATION",
    "LIST_CAPABILITY_ID",
    "LIST_KIND",
    "LIST_OPERATION",
    "OPERATIONS",
    "RoicTranscriptError",
    "build_roic_governance_record",
    "roic_adapter_hash",
    "roic_contract",
    "roic_fixture_hash",
    "roic_identity",
    "roic_permissions",
    "roic_schema_hash",
    "roic_source_hash",
]
