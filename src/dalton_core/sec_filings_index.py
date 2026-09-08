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

import json
from datetime import datetime, timezone
from typing import Any, Mapping

from .connector_inventory import load_packaged_connector_inventory
from .store import content_hash

KIND = "sec-filings-index"
CAPABILITY_ID = "capability:dalton:connector:sec-filings-index"
OPERATION = "list_filings"
GOVERNANCE_SCHEMA_VERSION = "0.1"
SIDE_EFFECT = "read:public-http"
# The index lives on data.sec.gov; the filing itself lives on the archive host.
FILING_ARCHIVE_HOST = "www.sec.gov"


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


def filings_index_descriptor_spec(
    permissions: Mapping[str, Any],
    created_at: str,
    *,
    capability_policy_ref: str,
) -> dict[str, Any]:
    """The closed capability descriptor for the filings-index operation.

    ``sec_descriptor_spec`` publishes the whole SEC connector under one id,
    ``capability:dalton:connector:sec-edgar``, whose schema hash covers every
    approved operation.  A descriptor published that way cannot carry the
    P10e approval: the signed record names its own capability and binds
    ``list_filings`` alone, so both the id and the schema hash differ.

    Publishing this narrower descriptor beside the shared one is what lets the
    executor run the filings index under the approval the owner actually
    signed, rather than under the company-facts one.
    """

    template, contract = filings_index_contract()
    identity = filings_index_identity()
    return {
        "schema_version": "0.1",
        "id": CAPABILITY_ID,
        "version": 1,
        "created_at": created_at,
        "kind": "connector",
        "name": "sec-filings-index",
        "label": "SEC filings index",
        "summary": "List an issuer's public SEC filings of one form",
        "aliases": ["SEC filings index", "SEC submissions"],
        "tags": ["connector", "public", "SEC"],
        "intent_examples": ["list the latest 10-K filings for an issuer"],
        "source": {
            "type": CAPABILITY_ID.split(":")[1],
            "namespace": CAPABILITY_ID.split(":")[2],
            "source_ref": "artifact:sec-public-source",
            "source_version": "1",
        },
        "contract": {
            "mode": "typed_call",
            "input_schema_ref": contract["input_schema_ref"],
            "output_schema_ref": contract["output_schema_ref"],
            "instruction_ref": None,
            "adapter_ref": template["transport"]["target_ref"],
        },
        "permissions": dict(permissions),
        "eligibility": {
            "state": "ready",
            "visibility_scopes": ["research"],
            "policy_ref": capability_policy_ref,
            "valid_until": None,
        },
        "source_hash": identity["source_hash"],
        "schema_hash": identity["schema_hash"],
    }


def filing_document_url(issuer: str, accession: str, primary_document: str) -> str:
    """The canonical EDGAR archive URL of one filing's primary document.

    EDGAR paths carry the CIK without its leading zeros and the accession
    without its dashes.  Verified live against ACN's FY2025 10-K.
    """

    if not isinstance(issuer, str) or not issuer.strip("0").isdigit():
        raise SecFilingsIndexError("issuer must be a numeric CIK")
    if not isinstance(accession, str) or len(accession.replace("-", "")) != 18:
        raise SecFilingsIndexError("accession must be an 18-digit EDGAR accession")
    if not isinstance(primary_document, str) or not primary_document:
        raise SecFilingsIndexError("filing lacks a primary document path")
    return (
        f"https://{FILING_ARCHIVE_HOST}/Archives/edgar/data/{int(issuer)}/"
        f"{accession.replace('-', '')}/{primary_document}"
    )


def build_filing_url_authorities(
    raw_response: bytes, parameters: Mapping[str, Any]
) -> list[dict[str, Any]]:
    """Rebuild the fetchable filing URLs from the exact submissions bytes.

    Nothing is stored here.  The frozen ``list_filings`` output schema carries
    record refs and hashes only — it deliberately does not carry the primary
    document path, which is the one field that names the filing's URL — so the
    path comes back out of the raw artifact, exactly the way the web search
    lane rebuilds its URLs from the bytes it ranked.

    Only filings the normalizer enumerated get a URL, so the caller's form and
    date filter governs here too and an unrelated filing in the same block can
    never be handed to the fetch lane.

    The record hash is recomputed as an invariant, not as a tamper boundary:
    the normalizer and the lookup below read the same bytes, so it can only
    fire if the two ever disagree about how a filing record is shaped. That is
    worth catching loudly — it would silently misname a URL — but it is not a
    check against an edited artifact, which is the envelope's job.
    """

    from .public_web_connector import public_web_url_ref
    from .sec_public_adapter import normalize_sec_submissions

    try:
        payload = json.loads(raw_response.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SecFilingsIndexError("submissions artifact is not JSON") from exc
    normalized = normalize_sec_submissions(payload, parameters, provider_status=200)
    issuer = str(parameters["issuer"])
    recent = payload["filings"]["recent"]
    accessions = recent["accessionNumber"]
    revisions = recent.get("amendmentOf") or [None] * len(accessions)
    by_accession = {
        accession: {
            "accession": accession,
            "form": recent["form"][index],
            "filing_date": recent["filingDate"][index],
            "primary_document": recent["primaryDocument"][index],
            "revision_of": revisions[index],
        }
        for index, accession in enumerate(accessions)
    }
    authorities: list[dict[str, Any]] = []
    for record in normalized["records"]:
        accession = record["record_ref"].removeprefix("sec:filing:")
        entry = by_accession.get(accession)
        if entry is None:
            raise SecFilingsIndexError("normalized filing is absent from the raw artifact")
        if content_hash(entry) != record["record_hash"]:
            raise SecFilingsIndexError("filing record hash does not bind the raw artifact")
        url = filing_document_url(issuer, accession, entry["primary_document"])
        authorities.append({
            "record_ref": record["record_ref"],
            "accession": accession,
            "form": entry["form"],
            "filing_date": entry["filing_date"],
            "canonical_url": url,
            "url_ref": public_web_url_ref(url),
            "host": FILING_ARCHIVE_HOST,
        })
    return authorities


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
    "FILING_ARCHIVE_HOST",
    "KIND",
    "OPERATION",
    "SecFilingsIndexError",
    "build_filing_url_authorities",
    "build_filings_index_governance_record",
    "filing_document_url",
    "filings_index_descriptor_spec",
    "filings_index_adapter_hash",
    "filings_index_contract",
    "filings_index_identity",
    "filings_index_permissions",
    "filings_index_schema_hash",
    "filings_index_source_hash",
]
