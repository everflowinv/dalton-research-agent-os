"""S5: who owns the company, who is selling it, and what the institutions did.

Dalton could read what a company *earned* and could not read who was *buying
and selling it*.  A director selling half their holding the week before a
guidance cut, a 13D arriving with an activist's purpose text, an institution
building a position over three quarters -- all of it is filed, primary and
free, and none of it was reachable.

Four operations on the connector Dalton already trusts for filings, because
these are the same SEC, the same identity and the same politeness budget as
``list_filings``.  Four rather than one, because a schema hash binds one
operation: an approval to read what a director sold must not widen into
reading every position an institution holds.

**The grade, and the exclusion it exists to state.**  These are ``filing``
grade evidence -- primary, regulatory, verbatim, with an accession number
behind every digit.  They are *not* financial statements, and the difference
matters more here than anywhere else in this system, because they look like
statements: they are full of large precise numbers filed by the company on a
government form.  A Form 4's "1,200,000 shares owned following the
transaction" will pass any digit check and is not a fact about the business.

So this module names its own grade word, :data:`OWNERSHIP_GRADE`, and the
whole point of the word is what it is *not*:

- it is absent from ``document_figure_grade.GRADE_BY_SPEC``, so no figure can
  ever be graded against one of these documents;
- it is absent from ``research_verification.FIGURE_ADMISSIBLE_GRADES``, so no
  verified figure can carry it;
- these forms are absent from ``statement_snapshot._FORMS``, which admits
  10-Q and 10-K and nothing else, so no statement line can come from here;
- and nothing in the forecast model reads this connector at all.

What they *are* is ``observation``-class evidence and typed ResearchEvents:
``insider_transaction``, ``ownership_change``, ``holdings_change``.  The brain
reads events; the model reads statements; these are events.  Tests assert every
one of those exclusions rather than trusting this docstring.

**Verbatim or nothing.**  Every number these operations emit is the text the
filing contained, and it is bound to the accession it was read from and the
SHA-256 of the exact bytes.  A Form 4 reporting four decimal places reports
them on purpose; a 13F reporting value in thousands is a different number from
one reporting dollars.  A float would lose the first and silently mis-scale the
second, so nothing here is ever parsed into one.
"""

from __future__ import annotations

import copy
from datetime import datetime
from typing import Any, Sequence

from .connector_inventory import load_packaged_connector_inventory
from .store import content_hash

TEMPLATE_KEY = "sec"
SOURCE_REF = "source:sec-edgar"
CONNECTOR_SLUG = "sec"

FORM4_OPERATION = "form4_transactions"
BENEFICIAL_OWNERSHIP_OPERATION = "beneficial_ownership"
FORM144_OPERATION = "form144_notices"
FORM13F_OPERATION = "form13f_holdings"
OPERATIONS: tuple[str, ...] = (
    FORM4_OPERATION,
    BENEFICIAL_OWNERSHIP_OPERATION,
    FORM144_OPERATION,
    FORM13F_OPERATION,
)

FORM4_KIND = "sec-form4-transactions"
BENEFICIAL_OWNERSHIP_KIND = "sec-beneficial-ownership"
FORM144_KIND = "sec-form144-notices"
FORM13F_KIND = "sec-form13f-holdings"
KIND_BY_OPERATION: dict[str, str] = {
    FORM4_OPERATION: FORM4_KIND,
    BENEFICIAL_OWNERSHIP_OPERATION: BENEFICIAL_OWNERSHIP_KIND,
    FORM144_OPERATION: FORM144_KIND,
    FORM13F_OPERATION: FORM13F_KIND,
}
OPERATION_BY_KIND: dict[str, str] = {
    kind: operation for operation, kind in KIND_BY_OPERATION.items()
}
CAPABILITY_BY_OPERATION: dict[str, str] = {
    operation: f"capability:dalton:connector:{kind}"
    for operation, kind in KIND_BY_OPERATION.items()
}

# Which EDGAR form types each operation is allowed to have been pointed at.
# The lane derives an accession from the filings index, so this is the filter
# that decides which rows of that index belong to which operation -- and it is
# a closed list rather than a prefix test, because "13F-HR" and "13F-NT" are
# different documents and "SC 13D" is not "SC 13E3".
FORMS_BY_OPERATION: dict[str, tuple[str, ...]] = {
    FORM4_OPERATION: ("3", "4", "5", "3/A", "4/A", "5/A"),
    BENEFICIAL_OWNERSHIP_OPERATION: (
        "SC 13D", "SC 13G", "SC 13D/A", "SC 13G/A",
    ),
    FORM144_OPERATION: ("144", "144/A"),
    FORM13F_OPERATION: ("13F-HR", "13F-HR/A", "13F-NT"),
}
ALL_OWNERSHIP_FORMS: frozenset[str] = frozenset(
    form for forms in FORMS_BY_OPERATION.values() for form in forms
)

GOVERNANCE_SCHEMA_VERSION = "0.1"
SIDE_EFFECT = "read:public-http"

# -- the grade word ---------------------------------------------------------
#
# Its own word rather than a reuse of ``company-filed-document``, because that
# grade is the one ``FIGURE_ADMISSIBLE_GRADES`` admits and reusing it would
# make every exclusion below a matter of somebody remembering.
OWNERSHIP_GRADE = "regulatory-ownership-filing"
OWNERSHIP_GRADE_MEANING = (
    "filed with the SEC by a person or institution about their holding in the "
    "company; primary and regulatory, and not a statement of what the business "
    "earned -- it may never be read for a financial-statement figure"
)
# What a reader of one of these should believe about it, in the event ledger's
# own vocabulary. ``primary_filing`` is right and is not a licence: the tier
# says how well attested the fact is, and the fact is "this person filed this",
# which is as well attested as facts get.
OWNERSHIP_EVIDENCE_TIER = "primary_filing"
# The mission word a lane needs to record these. Observations, never claims:
# a Claim in this system is a statement about the business that a forecast may
# rest on, and an insider's sale is not one.
WRITE_SCOPE = "observation"

# The document kinds a *figure* may be taken from. Deliberately empty: this is
# the same shape ``document_figure_grade.GRADE_BY_SPEC`` has, so that the
# exclusion is a structure a test can compare rather than a sentence.
FIGURE_GRADE_BY_OPERATION: dict[str, str] = {}


class SecOwnershipError(RuntimeError):
    """The SEC ownership identity or governance record is invalid."""


def _operation(operation: str) -> str:
    if operation not in KIND_BY_OPERATION:
        raise SecOwnershipError(f"sec has no frozen {operation!r} ownership operation")
    return operation


def ownership_contract(operation: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """The packaged SEC template and its frozen contract for one operation."""

    operation = _operation(operation)
    template = load_packaged_connector_inventory()["templates"][TEMPLATE_KEY]
    matches = [item for item in template["operations"]
               if item["operation"] == operation]
    if len(matches) != 1:
        raise SecOwnershipError(
            f"packaged sec template lacks the frozen {operation} operation"
        )
    return template, matches[0]


def ownership_source_hash() -> str:
    """Shared by all four: one SEC is one source.

    The same value ``sec_connector_identity`` computes for ``list_filings``,
    and that is the point -- these operations are not a second source pretending
    to be EDGAR, they are more of what the approved source already serves.
    """

    template, _ = ownership_contract(FORM4_OPERATION)
    return content_hash(dict(template["source_identity"]))


def ownership_schema_hash(operation: str) -> str:
    """Bound to one operation alone, so an approval cannot widen to another."""

    _, contract = ownership_contract(operation)
    return content_hash({
        "allowed_operations": [operation],
        "input_schema_refs": {operation: contract["input_schema_ref"]},
        "input_schema_hashes": {operation: contract["input_schema_hash"]},
        "output_schema_refs": {operation: contract["output_schema_ref"]},
        "output_schema_hashes": {operation: contract["output_schema_hash"]},
    })


def ownership_adapter_hash(operation: str) -> str:
    """Names the parser, so rewriting it is a different capability.

    There is no library here: unlike the statements lane, nothing pip-installed
    reads these documents. The adapter *is* Dalton's own XML reader, so the
    hash names that module and the operation it was asked for.
    """

    template, _ = ownership_contract(operation)
    return content_hash({
        "target_ref": template["transport"]["target_ref"],
        "source": template["source_identity"]["source_ref"],
        "operation": operation,
        "adapter": ADAPTER_REF,
    })


ADAPTER_REF = "adapter:dalton-core-sec-ownership-xml:0.1"


def ownership_identity(operation: str) -> dict[str, Any]:
    """Source and schema identity of exactly one ownership operation."""

    template, contract = ownership_contract(operation)
    return {
        "capability_id": CAPABILITY_BY_OPERATION[operation],
        "source_identity": dict(template["source_identity"]),
        "source_hash": ownership_source_hash(),
        "schema_hash": ownership_schema_hash(operation),
        "adapter_ref": template["transport"]["target_ref"],
        "adapter_hash": ownership_adapter_hash(operation),
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


def ownership_permissions() -> dict[str, Any]:
    """Credential-free public HTTPS and the raw sink -- the SEC declaration."""

    from .sec_authority_harness import PUBLIC_PERMISSIONS

    return copy.deepcopy(PUBLIC_PERMISSIONS)


def ownership_fixture_hash() -> str:
    template, _ = ownership_contract(FORM4_OPERATION)
    return template["fixture_manifest_hash"]


def ownership_output_schema(operation: str) -> dict[str, Any]:
    """The frozen output contract one parsed filing must satisfy."""

    template, contract = ownership_contract(operation)
    ref = contract["output_schema_ref"]
    for document in template["schema_documents"]:
        if document["schema_ref"] == ref:
            return document["document"]
    raise SecOwnershipError(f"packaged sec template has no {operation} output contract")


def _wire_time(value: datetime) -> str:
    return value.astimezone(value.tzinfo).isoformat(timespec="microseconds")


def build_sec_ownership_governance_record(
    *,
    operation: str,
    approved_by: str,
    status: str = "proposed",
    effective_from: str = "2026-09-09T00:00:00+00:00",
    max_lease_seconds: int = 120,
    version: int = 1,
) -> dict[str, Any]:
    """Closed, hash-bound governance record for one ownership operation."""

    operation = _operation(operation)
    if status not in {"proposed", "approved"}:
        raise SecOwnershipError("governance status must be proposed or approved")
    if not isinstance(approved_by, str) or not approved_by.startswith("human:"):
        raise SecOwnershipError("approved_by must be a human: principal")
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
        "allowed_permissions": copy.deepcopy(ownership_permissions()),
        "expected_source_hash": ownership_source_hash(),
        "expected_schema_hash": ownership_schema_hash(operation),
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
    """Name one exact read: this approval, this filing, those bytes.

    Every parsed number carries this and the artifact hash. Two runs against
    the same accession that returned identical bytes are the same invocation;
    a filing that was amended and re-read is a different one, which is exactly
    what "verbatim-bound to the accession and the artifact" has to mean to be
    checkable rather than decorative.
    """

    return "connector-invocation:sec-ownership:" + content_hash({
        "operation": _operation(operation),
        "source_ref": SOURCE_REF,
        "adapter_ref": ADAPTER_REF,
        "adapter_hash": ownership_adapter_hash(operation),
        "governance_ref": governance_ref,
        "governance_hash": governance_hash,
        "parameters": parameters,
        "artifact_hash": artifact_hash,
    })[:32]


# -- what these documents live at -------------------------------------------
#
# Derived, never handed in. The forbidden ``route:arbitrary-attachment-url``
# on this profile means the caller supplies an accession and this function
# supplies the path; there is no input through which a URL can arrive.

ARCHIVE_HOST = "www.sec.gov"
PRIMARY_DOCUMENT = "primary_doc.xml"
FILING_INDEX_DOCUMENT = "index.json"


def archive_directory_url(cik: str, accession: str) -> str:
    """The EDGAR archive directory for one filing of one filer."""

    digits = str(cik).strip().lstrip("0")
    if not digits.isdigit():
        raise SecOwnershipError(f"{cik!r} is not a CIK")
    plain = str(accession).strip().replace("-", "")
    if len(plain) != 18 or not plain.isdigit():
        raise SecOwnershipError(f"{accession!r} is not an accession number")
    return f"https://{ARCHIVE_HOST}/Archives/edgar/data/{digits}/{plain}"


def primary_document_url(cik: str, accession: str) -> str:
    """``primary_doc.xml``: the structured half of every one of these forms."""

    return f"{archive_directory_url(cik, accession)}/{PRIMARY_DOCUMENT}"


def filing_index_url(cik: str, accession: str) -> str:
    """The filing's own index, which names its information table.

    Only 13F needs this: the holdings live in a second document whose file name
    the filer chooses. Reading the filing's own index to learn that name is not
    the forbidden arbitrary-URL route -- the name comes from the filing, the
    directory comes from the accession, and neither comes from a caller.
    """

    return f"{archive_directory_url(cik, accession)}/{FILING_INDEX_DOCUMENT}"


# -- what is already on disk ------------------------------------------------
#
# C1 established that the governed ``list_filings`` call returns the issuer's
# *whole* ``filings.recent`` block -- every form, not the one that was asked
# for -- and that the raw body is spooled and hashed. Every Form 4, 13D/G and
# 144 these five companies have filed in the covered window is therefore
# already on this disk, unparsed. This reads it, and it never fetches.
#
# The same caveat C1 wrote down applies exactly: these rows are in the bytes
# but not in that invocation's ``source_record_refs``, so what may be taken
# from them is an *accession and a date*, not a document to go and fetch on
# the strength of having seen it here. Fetching the primary document is a
# separate, governed act with its own approval, which is why the four
# operations above exist at all.

DEFAULT_LOOKBACK_DAYS = 120
MAX_FILINGS_PER_FORM = 40


def ownership_filings(
    payload: Any,
    *,
    forms: Sequence[str] | None = None,
    since: str | None = None,
    limit: int = MAX_FILINGS_PER_FORM,
) -> list[dict[str, Any]]:
    """Every ownership filing in one spooled submissions artifact, newest first.

    ``form`` is matched exactly against the closed list rather than by prefix:
    "13F-HR" and "13F-NT" are different documents, and a prefix test on "4"
    would take every 40-F ever filed.
    """

    from collections.abc import Mapping as _Mapping

    wanted = frozenset(forms) if forms else ALL_OWNERSHIP_FORMS
    filings = payload.get("filings") if isinstance(payload, _Mapping) else None
    if not isinstance(filings, _Mapping):
        raise SecOwnershipError("payload is not a SEC submissions index")
    recent = filings.get("recent")
    if not isinstance(recent, _Mapping):
        raise SecOwnershipError("submissions index has no recent block")
    accessions = recent.get("accessionNumber")
    if not isinstance(accessions, list):
        raise SecOwnershipError("submissions index has no accessionNumber column")
    length = len(accessions)

    def column(name: str) -> list[Any]:
        values = recent.get(name)
        if not isinstance(values, list) or len(values) != length:
            return [None] * length
        return values

    forms_column = recent.get("form")
    if not isinstance(forms_column, list) or len(forms_column) != length:
        raise SecOwnershipError("submissions index has no form column")
    filed = column("filingDate")
    reported = column("reportDate")
    documents = column("primaryDocument")
    descriptions = column("primaryDocDescription")

    found: list[dict[str, Any]] = []
    for index in range(length):
        form = forms_column[index]
        if not isinstance(form, str) or form not in wanted:
            continue
        filing_date = filed[index]
        if not isinstance(filing_date, str) or len(filing_date) != 10:
            continue
        if since is not None and filing_date < since:
            continue
        accession = accessions[index]
        if not isinstance(accession, str) or not accession:
            continue
        report_date = reported[index]
        found.append({
            "accession": accession,
            "form": form,
            "operation": next(
                (
                    operation for operation, allowed in FORMS_BY_OPERATION.items()
                    if form in allowed
                ),
                None,
            ),
            "filing_date": filing_date,
            "report_date": (
                report_date if isinstance(report_date, str) and len(report_date) == 10
                else None
            ),
            "primary_document": (
                documents[index] if isinstance(documents[index], str) else None
            ),
            "primary_doc_description": (
                descriptions[index] if isinstance(descriptions[index], str) else None
            ),
        })
    found.sort(key=lambda row: (row["filing_date"], row["accession"]), reverse=True)
    return found[: max(0, int(limit))]


def ownership_filings_for_issuer(
    connection: Any,
    state_dir: Any,
    *,
    issuer: str,
    today: str,
    forms: Sequence[str] | None = None,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    limit: int = MAX_FILINGS_PER_FORM,
) -> dict[str, Any]:
    """One issuer's recent ownership filings, from bytes already spooled.

    Returns a reason rather than raising when nothing can be read: a company
    whose discovery run has not happened yet is a normal state of this system
    and the lane reports it in its tick summary.
    """

    from datetime import date as _date, timedelta as _timedelta
    from pathlib import Path as _Path

    from .sec_earnings_release import (
        read_submissions_artifact,
        submissions_artifacts,
    )

    since = (
        _date.fromisoformat(today) - _timedelta(days=max(0, int(lookback_days)))
    ).isoformat()
    root = _Path(state_dir)
    for artifact in submissions_artifacts(connection, issuer=issuer):
        found = read_submissions_artifact(root, artifact["artifact_hash"])
        if found["status"] == "absent":
            continue
        if found["status"] != "read":
            return {"filings": [], "since": since, **found, **artifact}
        try:
            filings = ownership_filings(
                found["payload"], forms=forms, since=since, limit=limit
            )
        except SecOwnershipError as exc:
            return {"status": "unreadable", "reason": str(exc),
                    "filings": [], "since": since, **artifact}
        return {"status": "read", "filings": filings, "since": since, **artifact}
    return {
        "status": "unavailable",
        "reason": (
            f"no spooled SEC filings index for issuer {issuer}; the discovery "
            "lane has not run for this company or its artifact has been pruned"
        ),
        "filings": [],
        "since": since,
    }


__all__ = [
    "ADAPTER_REF",
    "DEFAULT_LOOKBACK_DAYS",
    "MAX_FILINGS_PER_FORM",
    "ownership_filings",
    "ownership_filings_for_issuer",
    "ALL_OWNERSHIP_FORMS",
    "ARCHIVE_HOST",
    "BENEFICIAL_OWNERSHIP_KIND",
    "BENEFICIAL_OWNERSHIP_OPERATION",
    "CAPABILITY_BY_OPERATION",
    "CONNECTOR_SLUG",
    "FIGURE_GRADE_BY_OPERATION",
    "FILING_INDEX_DOCUMENT",
    "FORM13F_KIND",
    "FORM13F_OPERATION",
    "FORM144_KIND",
    "FORM144_OPERATION",
    "FORM4_KIND",
    "FORM4_OPERATION",
    "FORMS_BY_OPERATION",
    "GOVERNANCE_SCHEMA_VERSION",
    "KIND_BY_OPERATION",
    "OPERATIONS",
    "OPERATION_BY_KIND",
    "OWNERSHIP_EVIDENCE_TIER",
    "OWNERSHIP_GRADE",
    "OWNERSHIP_GRADE_MEANING",
    "PRIMARY_DOCUMENT",
    "SIDE_EFFECT",
    "SOURCE_REF",
    "TEMPLATE_KEY",
    "WRITE_SCOPE",
    "SecOwnershipError",
    "archive_directory_url",
    "build_sec_ownership_governance_record",
    "filing_index_url",
    "invocation_ref",
    "ownership_adapter_hash",
    "ownership_contract",
    "ownership_fixture_hash",
    "ownership_identity",
    "ownership_output_schema",
    "ownership_permissions",
    "ownership_schema_hash",
    "ownership_source_hash",
]
