"""P13ag: SEC financial statements read as statements, not one concept at a time.

Dalton already reads SEC company facts, one XBRL concept per dispatch. That is
correct and slow: five successful runs in a day produced six figures. What it
cannot produce at all is the *structure* -- which concept is which line of which
statement, what it rolls up into, and the dimension axes a company reports
against. A model needs that before it can have line items.

``edgartools`` parses the filing's own presentation and calculation linkbases
and returns exactly that. Measured on one EPAM 10-Q: 22 top-level income lines
(the company's real expense structure), revenue split by pricing basis, and six
dimension axes of segment detail -- all bound to one accession.

**This does not replace reading the filings.** Anything the parser does not
reach still has to come from the original text, and that lane stays. This is a
second way of reading one source, not a replacement for the first.

So the source is shared -- ``source:sec-edgar``, the same SEC, the same source
hash as the filings connector -- while the connector, the schema and the
approval are its own. P10e split ``list_filings`` from ``get_company_facts`` on
exactly that principle.

The owner accepted the trade this rests on: the parser does its own HTTP, so
the bytes are not verified by Dalton's transport the way company-facts bytes
are. The raw parser output is hashed and kept as the artifact, and every line
carries the accession it came from, so any figure can be taken back to SEC and
checked. That is weaker than byte verification and is written down rather than
implied.
"""

from __future__ import annotations

import copy
from datetime import datetime
from typing import Any

from .connector_inventory import load_packaged_connector_inventory
from .store import content_hash

TEMPLATE_KEY = "sec-financials"
OPERATION = "get_financial_statements"
KIND = "sec-financial-statements"
CAPABILITY_ID = "capability:dalton:connector:sec-financial-statements"
GOVERNANCE_SCHEMA_VERSION = "0.1"
SIDE_EFFECT = "read:public-http"
# What the adapter is: a pinned parser, not a service. The hash binds the
# identity to the library that produced the parse, because the parse is the
# part the owner is being asked to trust.
ADAPTER_LIBRARY = "edgartools"


class SecFinancialsError(RuntimeError):
    """The SEC financial-statements identity or governance record is invalid."""


def sec_financials_contract() -> tuple[dict[str, Any], dict[str, Any]]:
    """The packaged template and its frozen statements operation."""

    template = load_packaged_connector_inventory()["templates"][TEMPLATE_KEY]
    matches = [item for item in template["operations"]
               if item["operation"] == OPERATION]
    if len(matches) != 1:
        raise SecFinancialsError(
            f"packaged template lacks the frozen {OPERATION} operation"
        )
    return template, matches[0]


def sec_financials_source_hash() -> str:
    """Shared with the filings connector: the same SEC is the same source."""

    template, _ = sec_financials_contract()
    return content_hash(dict(template["source_identity"]))


def sec_financials_output_schema(*, version: int = 3) -> dict[str, Any]:
    """Closed output schema for v2 or v3; v2 remains byte-compatible."""

    if version not in {2, 3}:
        raise SecFinancialsError("financial statements contract version must be 2 or 3")
    template, contract = sec_financials_contract()
    document = next(item["document"] for item in template["schema_documents"]
                    if item["schema_ref"] == contract["output_schema_ref"])
    result = copy.deepcopy(document)
    if version == 2:
        line = result["properties"]["filings"]["items"]["properties"]["lines"]["items"]
        line["properties"].pop("dimension_count")
        line["required"].remove("dimension_count")
    return result


def sec_financials_schema_hash(*, version: int = 3) -> str:
    """Bound to this operation alone, so the approval cannot widen."""

    _, contract = sec_financials_contract()
    output_hash = content_hash(sec_financials_output_schema(version=version))
    return content_hash({
        "allowed_operations": [OPERATION],
        "input_schema_refs": {OPERATION: contract["input_schema_ref"]},
        "input_schema_hashes": {OPERATION: contract["input_schema_hash"]},
        "output_schema_refs": {OPERATION: contract["output_schema_ref"]},
        "output_schema_hashes": {OPERATION: output_hash},
    })


def sec_financials_adapter_hash() -> str:
    template, _ = sec_financials_contract()
    return content_hash({
        "target_ref": template["transport"]["target_ref"],
        "source": template["source_identity"]["source_ref"],
        "operation": OPERATION,
        "library": ADAPTER_LIBRARY,
    })


def sec_financials_identity(*, version: int = 3) -> dict[str, Any]:
    """Source and schema identity of the statements operation."""

    template, contract = sec_financials_contract()
    return {
        "capability_id": CAPABILITY_ID,
        "source_identity": dict(template["source_identity"]),
        "source_hash": sec_financials_source_hash(),
        "schema_hash": sec_financials_schema_hash(version=version),
        "adapter_ref": template["transport"]["target_ref"],
        "adapter_hash": sec_financials_adapter_hash(),
        "operation": OPERATION,
        "allowed_operations": [OPERATION],
        "input_schema_ref": contract["input_schema_ref"],
        "input_schema_hash": contract["input_schema_hash"],
        "output_schema_ref": contract["output_schema_ref"],
        "output_schema_hash": content_hash(sec_financials_output_schema(version=version)),
        "input_schema_refs": {OPERATION: contract["input_schema_ref"]},
        "input_schema_hashes": {OPERATION: contract["input_schema_hash"]},
        "output_schema_refs": {OPERATION: contract["output_schema_ref"]},
        "output_schema_hashes": {OPERATION: content_hash(sec_financials_output_schema(version=version))},
    }


def sec_financials_permissions() -> dict[str, Any]:
    """Credential-free public HTTPS to SEC, and the raw sink.

    The same boundary the filings lane has, because it is the same SEC over the
    same public HTTPS. ``network`` is true here and false for the MCP
    connectors: this process does reach out. Which hosts it may reach is the
    template's own allowlist, not this declaration.
    """

    from .sec_authority_harness import PUBLIC_PERMISSIONS

    # The identical declaration the filings lane carries, not a second copy of
    # it: the boundary is the same public HTTPS to the same SEC with no
    # credential. The hosts are bound by the template's own allowlist, which
    # the capability permission shape has no field for and does not need.
    return copy.deepcopy(PUBLIC_PERMISSIONS)


def sec_financials_fixture_hash() -> str:
    template, _ = sec_financials_contract()
    return template["fixture_manifest_hash"]


def _wire_time(value: datetime) -> str:
    return value.astimezone(value.tzinfo).isoformat(timespec="microseconds")


def build_sec_financials_governance_record(
    *,
    approved_by: str,
    status: str = "proposed",
    effective_from: str = "2026-09-09T00:00:00+00:00",
    max_lease_seconds: int = 120,
    version: int = 3,
) -> dict[str, Any]:
    """Closed, hash-bound governance record for the statements capability."""

    if status not in {"proposed", "approved"}:
        raise SecFinancialsError("governance status must be proposed or approved")
    if not isinstance(approved_by, str) or not approved_by.startswith("human:"):
        raise SecFinancialsError("approved_by must be a human: principal")
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
        "allowed_permissions": copy.deepcopy(sec_financials_permissions()),
        "expected_source_hash": sec_financials_source_hash(),
        "expected_schema_hash": sec_financials_schema_hash(version=version),
    }
    base["content_hash"] = content_hash(base)
    return base


__all__ = [
    "ADAPTER_LIBRARY",
    "CAPABILITY_ID",
    "KIND",
    "OPERATION",
    "SecFinancialsError",
    "build_sec_financials_governance_record",
    "sec_financials_adapter_hash",
    "sec_financials_contract",
    "sec_financials_fixture_hash",
    "sec_financials_identity",
    "sec_financials_permissions",
    "sec_financials_output_schema",
    "sec_financials_schema_hash",
    "sec_financials_source_hash",
]
