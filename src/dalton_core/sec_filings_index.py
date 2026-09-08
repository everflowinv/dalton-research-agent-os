"""P10e: the governed SEC filings-index operation (``list_filings``).

The Initial Screen's source base wants the latest annual report.  AlphaEngine
carries calls and research only, and the company-facts endpoint carries
numbers, not documents; the filing itself lives at a URL that only the SEC
submissions index names.  Reading that index is a *different* connector
operation from reading company facts, with its own frozen input and output
schema, so it carries its own governance identity rather than riding on the
company-facts approval.

The identity follows the P9d-1 precedent set by AlphaEngine's
``search_library``: one source, two operations, two capabilities.  The source
hash is shared, because the source really is the same SEC; the schema hash
binds ``list_filings`` *alone*, so holding this approval never widens into
company facts and holding the company-facts approval never widens into here.

Note what this deliberately does not do: it does not touch
``sec_connector_identity``.  That function hashes the SEC template's whole
approved operation set, and the live ``sec-company-facts-v2.json`` record the
running writer holds is bound to that hash.  Re-scoping it per operation would
have been the tidier read, but it would silently invalidate an approval that
is in production, so the scoping lives here instead.

This module is the identity and the approval record for that operation.  The
record is generated as ``proposed``: an approval names the person who gave it,
and only the owner can be that person.

    python3 -m dalton_core.connector_governance_cli propose \\
        --kind sec-filings-index --approved-by human:<owner> \\
        --path .../connector-governance/sec-filings-index-v1.json
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .connector_inventory import load_packaged_connector_inventory
from .store import content_hash

KIND = "sec-filings-index"
CAPABILITY_ID = "capability:dalton:connector:sec-filings-index"
OPERATION = "list_filings"
GOVERNANCE_SCHEMA_VERSION = "0.1"
SIDE_EFFECT = "read:public-http"


class SecFilingsIndexError(RuntimeError):
    """The filings-index identity or governance record is invalid."""


def filings_index_contract() -> tuple[dict[str, Any], dict[str, Any]]:
    """The packaged SEC template and its frozen ``list_filings`` operation."""

    template = load_packaged_connector_inventory()["templates"]["sec"]
    matches = [
        item for item in template["operations"] if item["operation"] == OPERATION
    ]
    if len(matches) != 1:
        raise SecFilingsIndexError(
            f"packaged SEC template lacks the frozen {OPERATION} operation"
        )
    return template, matches[0]


def filings_index_source_hash() -> str:
    """Shared with company facts: the same SEC is the same source."""

    template, _ = filings_index_contract()
    return content_hash(dict(template["source_identity"]))


def filings_index_schema_hash() -> str:
    """Bound to ``list_filings`` alone, so the approval cannot widen."""

    _, contract = filings_index_contract()
    return content_hash(
        {
            "allowed_operations": [OPERATION],
            "input_schema_refs": {OPERATION: contract["input_schema_ref"]},
            "input_schema_hashes": {OPERATION: contract["input_schema_hash"]},
            "output_schema_refs": {OPERATION: contract["output_schema_ref"]},
            "output_schema_hashes": {OPERATION: contract["output_schema_hash"]},
        }
    )


def filings_index_adapter_hash() -> str:
    template, _ = filings_index_contract()
    return content_hash(
        {
            "target_ref": template["transport"]["target_ref"],
            "source": template["source_identity"]["source_ref"],
            "operation": OPERATION,
        }
    )


def filings_index_identity() -> dict[str, Any]:
    """Source and schema identity of the SEC submissions operation."""

    template, contract = filings_index_contract()
    return {
        "capability_id": CAPABILITY_ID,
        "source_identity": dict(template["source_identity"]),
        "source_hash": filings_index_source_hash(),
        "schema_hash": filings_index_schema_hash(),
        "adapter_ref": template["transport"]["target_ref"],
        "adapter_hash": filings_index_adapter_hash(),
        "allowed_operations": [OPERATION],
        "input_schema_refs": {OPERATION: contract["input_schema_ref"]},
        "input_schema_hashes": {OPERATION: contract["input_schema_hash"]},
        "output_schema_refs": {OPERATION: contract["output_schema_ref"]},
        "output_schema_hashes": {OPERATION: contract["output_schema_hash"]},
    }


def filings_index_permissions() -> dict[str, Any]:
    """Same credential-free public-HTTPS permissions the company-facts lane has."""

    import copy

    from .sec_authority_harness import PUBLIC_PERMISSIONS

    return copy.deepcopy(PUBLIC_PERMISSIONS)


def _wire_time(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")


def build_filings_index_governance_record(
    *,
    approved_by: str,
    status: str = "proposed",
    effective_from: str = "2026-09-07T00:00:00+00:00",
    max_lease_seconds: int = 120,
    version: int = 1,
) -> dict[str, Any]:
    """Closed, hash-bound governance record for the SEC filings-index capability."""

    if status not in {"proposed", "approved"}:
        raise SecFilingsIndexError("governance status must be proposed or approved")
    if not isinstance(approved_by, str) or not approved_by.startswith("human:"):
        raise SecFilingsIndexError("approved_by must be a human: principal")
    base = {
        "schema_version": GOVERNANCE_SCHEMA_VERSION,
        "id": f"connector-governance:{KIND}:v{version}",
        "status": status,
        "capability_id": CAPABILITY_ID,
        "approved_by": approved_by,
        "principal_ref": "principal:dalton-core-trusted-runner",
        "policy_ref": f"policy:dalton:connector-governance:{KIND}:v{version}",
        "approval_ref": f"approval:connector-governance:{KIND}:v{version}",
        "decision_ref": f"capability-decision:connector-governance:{KIND}:v{version}",
        "registry_revision_ref": f"{CAPABILITY_ID}@v{version}",
        "attestation_ref": f"attestation:connector-governance:{KIND}:v{version}",
        "effective_from": _wire_time(datetime.fromisoformat(effective_from)),
        "effective_until": None,
        "max_lease_seconds": max_lease_seconds,
        "allowed_permissions": filings_index_permissions(),
        "expected_source_hash": filings_index_source_hash(),
        "expected_schema_hash": filings_index_schema_hash(),
    }
    base["content_hash"] = content_hash(base)
    return base


__all__ = [
    "CAPABILITY_ID",
    "KIND",
    "OPERATION",
    "SecFilingsIndexError",
    "build_filings_index_governance_record",
    "filings_index_adapter_hash",
    "filings_index_contract",
    "filings_index_identity",
    "filings_index_permissions",
    "filings_index_schema_hash",
    "filings_index_source_hash",
]
