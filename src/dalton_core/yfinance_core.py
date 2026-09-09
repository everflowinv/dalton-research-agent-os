"""P11a: Yahoo Finance identity -- prices and street estimates, read by a library.

Dalton could see filings and could not see the market. A model that cannot say
what a company trades at cannot produce a valuation, and the whole S6 branch of
the Initial Screen was fail-closed behind that absence.

The owner chose Yahoo through ``yfinance`` because it is free, because the same
library already backs three of their own tools, and because a paid vendor is a
decision to make once the market layer has proved it is worth paying for.

**What is being accepted here, plainly.** Yahoo publishes no API for this. The
library scrapes endpoints that can change shape without notice, there are no
terms that cover the use, and nobody to appeal to when a request is refused.
The trade is the same one P13ag made for ``edgartools``: the library does its
own HTTP, so Dalton's transport does not verify the bytes, and instead the raw
library output is canonicalised, hashed and kept as the artifact before
anything is read out of it. Every stored bar names the invocation and the
artifact hash it came from, so any number can be taken back to a specific,
replayable call. That is weaker than byte verification, and it is written down
rather than implied.

**What this connector deliberately cannot do.** Yahoo also serves income
statements, balance sheets and cash-flow statements. This connector exposes
none of them, and the valuation layer refuses to read fundamentals from this
source at all. A filed figure has a primary source with an accession number
behind it; a scraped second-hand copy of the same figure is a worse number
wearing the same clothes, and the moment one is admitted the whole "every
number goes back to a filing" discipline is decoration.

Two operations, two capabilities, two approvals. ``daily_prices`` is what a
market printed; ``analyst_estimates`` is what sell-side analysts said. They are
different kinds of thing, and a schema hash binds one operation, so one record
cannot be reused for the other without widening what was approved.
"""

from __future__ import annotations

import copy
from datetime import datetime
from typing import Any

from .connector_inventory import load_packaged_connector_inventory
from .store import content_hash

TEMPLATE_KEY = "yfinance"
DAILY_PRICES_OPERATION = "daily_prices"
ANALYST_ESTIMATES_OPERATION = "analyst_estimates"
OPERATIONS = (DAILY_PRICES_OPERATION, ANALYST_ESTIMATES_OPERATION)

DAILY_PRICES_KIND = "yfinance-daily-prices"
ANALYST_ESTIMATES_KIND = "yfinance-analyst-estimates"
KIND_BY_OPERATION = {
    DAILY_PRICES_OPERATION: DAILY_PRICES_KIND,
    ANALYST_ESTIMATES_OPERATION: ANALYST_ESTIMATES_KIND,
}
DAILY_PRICES_CAPABILITY_ID = "capability:dalton:connector:yfinance-daily-prices"
ANALYST_ESTIMATES_CAPABILITY_ID = (
    "capability:dalton:connector:yfinance-analyst-estimates"
)
CAPABILITY_BY_OPERATION = {
    DAILY_PRICES_OPERATION: DAILY_PRICES_CAPABILITY_ID,
    ANALYST_ESTIMATES_OPERATION: ANALYST_ESTIMATES_CAPABILITY_ID,
}

GOVERNANCE_SCHEMA_VERSION = "0.1"
SIDE_EFFECT = "read:public-http"
SOURCE_REF = "source:yahoo-finance"
# What the adapter is: a pinned scraper, not a service. The hash binds the
# identity to the library that produced the numbers, because the library is the
# part the owner is being asked to trust.
ADAPTER_LIBRARY = "yfinance"


class YFinanceError(RuntimeError):
    """The Yahoo Finance identity or governance record is invalid."""


def _operation(operation: str) -> str:
    if operation not in KIND_BY_OPERATION:
        raise YFinanceError(f"yfinance has no frozen {operation!r} operation")
    return operation


def yfinance_contract(operation: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """The packaged template and its frozen operation contract."""

    operation = _operation(operation)
    template = load_packaged_connector_inventory()["templates"][TEMPLATE_KEY]
    matches = [item for item in template["operations"]
               if item["operation"] == operation]
    if len(matches) != 1:
        raise YFinanceError(
            f"packaged yfinance template lacks the frozen {operation} operation"
        )
    return template, matches[0]


def yfinance_source_hash() -> str:
    """Shared by both operations: one Yahoo is one source."""

    template, _ = yfinance_contract(DAILY_PRICES_OPERATION)
    return content_hash(dict(template["source_identity"]))


def yfinance_schema_hash(operation: str) -> str:
    """Bound to one operation alone, so an approval cannot widen to the other."""

    _, contract = yfinance_contract(operation)
    return content_hash({
        "allowed_operations": [operation],
        "input_schema_refs": {operation: contract["input_schema_ref"]},
        "input_schema_hashes": {operation: contract["input_schema_hash"]},
        "output_schema_refs": {operation: contract["output_schema_ref"]},
        "output_schema_hashes": {operation: contract["output_schema_hash"]},
    })


def yfinance_adapter_hash(operation: str) -> str:
    """Names the library, so swapping it is a different capability."""

    template, _ = yfinance_contract(operation)
    return content_hash({
        "target_ref": template["transport"]["target_ref"],
        "source": template["source_identity"]["source_ref"],
        "operation": operation,
        "library": ADAPTER_LIBRARY,
    })


def yfinance_identity(operation: str) -> dict[str, Any]:
    """Source and schema identity of exactly one yfinance operation."""

    template, contract = yfinance_contract(operation)
    return {
        "capability_id": CAPABILITY_BY_OPERATION[operation],
        "source_identity": dict(template["source_identity"]),
        "source_hash": yfinance_source_hash(),
        "schema_hash": yfinance_schema_hash(operation),
        "adapter_ref": template["transport"]["target_ref"],
        "adapter_hash": yfinance_adapter_hash(operation),
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


def yfinance_permissions() -> dict[str, Any]:
    """Credential-free public HTTPS, and the raw sink.

    The identical declaration every public-web lane carries, not a second copy
    of it. Which hosts the library may reach is the template's own allowlist --
    Yahoo's two query hosts -- which this shape has no field for and does not
    need.
    """

    from .sec_authority_harness import PUBLIC_PERMISSIONS

    return copy.deepcopy(PUBLIC_PERMISSIONS)


def yfinance_fixture_hash() -> str:
    template, _ = yfinance_contract(DAILY_PRICES_OPERATION)
    return template["fixture_manifest_hash"]


def yfinance_output_schema(operation: str) -> dict[str, Any]:
    """The frozen output contract one observation must satisfy."""

    template, contract = yfinance_contract(operation)
    ref = contract["output_schema_ref"]
    for document in template["schema_documents"]:
        if document["schema_ref"] == ref:
            return document["document"]
    raise YFinanceError(f"packaged yfinance template has no {operation} output contract")


def _wire_time(value: datetime) -> str:
    return value.astimezone(value.tzinfo).isoformat(timespec="microseconds")


def build_yfinance_governance_record(
    *,
    operation: str,
    approved_by: str,
    status: str = "proposed",
    effective_from: str = "2026-09-09T00:00:00+00:00",
    max_lease_seconds: int = 120,
    version: int = 1,
) -> dict[str, Any]:
    """Closed, hash-bound governance record for one yfinance operation."""

    operation = _operation(operation)
    if status not in {"proposed", "approved"}:
        raise YFinanceError("governance status must be proposed or approved")
    if not isinstance(approved_by, str) or not approved_by.startswith("human:"):
        raise YFinanceError("approved_by must be a human: principal")
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
        "allowed_permissions": copy.deepcopy(yfinance_permissions()),
        "expected_source_hash": yfinance_source_hash(),
        "expected_schema_hash": yfinance_schema_hash(operation),
    }
    base["content_hash"] = content_hash(base)
    return base


def invocation_ref(
    *,
    operation: str,
    governance_ref: str,
    governance_hash: str,
    parameters: dict[str, Any],
    artifact_hash: str,
) -> str:
    """Name one exact call: this approval, these parameters, that raw output.

    Every stored bar carries this. Two runs of the same window against the same
    approval that returned the same bytes are the same invocation; a run whose
    bytes differ -- a restatement, a split, a Yahoo correction -- is a different
    one, which is precisely the distinction a version chain needs to make.
    """

    return "connector-invocation:yfinance:" + content_hash({
        "operation": _operation(operation),
        "source_ref": SOURCE_REF,
        "adapter_library": ADAPTER_LIBRARY,
        "adapter_hash": yfinance_adapter_hash(operation),
        "governance_ref": governance_ref,
        "governance_hash": governance_hash,
        "parameters": parameters,
        "artifact_hash": artifact_hash,
    })[:32]


__all__ = [
    "ADAPTER_LIBRARY",
    "ANALYST_ESTIMATES_CAPABILITY_ID",
    "ANALYST_ESTIMATES_KIND",
    "ANALYST_ESTIMATES_OPERATION",
    "CAPABILITY_BY_OPERATION",
    "DAILY_PRICES_CAPABILITY_ID",
    "DAILY_PRICES_KIND",
    "DAILY_PRICES_OPERATION",
    "KIND_BY_OPERATION",
    "OPERATIONS",
    "SOURCE_REF",
    "TEMPLATE_KEY",
    "YFinanceError",
    "build_yfinance_governance_record",
    "invocation_ref",
    "yfinance_adapter_hash",
    "yfinance_contract",
    "yfinance_fixture_hash",
    "yfinance_identity",
    "yfinance_output_schema",
    "yfinance_permissions",
    "yfinance_schema_hash",
    "yfinance_source_hash",
]
