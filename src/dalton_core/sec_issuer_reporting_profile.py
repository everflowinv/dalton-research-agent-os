"""Read-only reporting-family evidence from an exact held SEC submissions root.

This selects original document forms, never a financial period or accounting
standard. A 6-K remains a disclosure until its own content proves more.
"""
from datetime import date, datetime
import hashlib
import json
import re

from .store import content_hash
from .sec_submissions_pages import _record, envelope_parameters, ROOT_CURSOR
from .source_discovery_read_budget import bounded_rows
from .raw_spool import RawSpoolError

CONTRACT_REF = "sec-issuer-reporting-profile:0.1"
MAX_ROOT_BYTES = 4_000_000
MAX_RECENT_FILINGS = 10_000
MAX_PROFILE_AGE_DAYS = 90
FOREIGN_FORMS = frozenset(("20-F", "20-F/A", "40-F", "40-F/A", "6-K", "6-K/A"))
FOREIGN_SPECS = {"annual-report-20f": "20-F", "annual-report-20f-amendment": "20-F/A",
                 "annual-report-40f": "40-F", "annual-report-40f-amendment": "40-F/A",
                 "foreign-disclosure-6k": "6-K", "foreign-disclosure-6k-amendment": "6-K/A"}
_ANNUAL = {"10-K": "domestic", "20-F": "foreign_20f", "40-F": "foreign_40f"}


class SecReportingProfileError(ValueError):
    pass


class SecReportingProfileUnavailable(SecReportingProfileError):
    """No usable root: the existing governed index bootstrap remains available."""


def profile_from_discovery(connection, spool, *, discovery_ref, company_ref, issuer, as_of):
    """Close discovery/envelope/invocation/call/raw identity before selection."""
    if not isinstance(as_of, date) or isinstance(as_of, datetime):
        raise SecReportingProfileError("reporting profile requires a calendar date")
    d = _record(connection, "coverage_mission_source_discoveries", "record_id", discovery_ref)
    e = _record(connection, "connector_source_envelopes", "source_envelope_id", d["source_envelope_ref"])
    invocation = _record(connection, "connector_invocations", "connector_invocation_id", d["connector_invocation_ref"])
    parameters = envelope_parameters(connection, e)
    if (d["company_ref"] != company_ref or d["source_ref"] != "source:sec-edgar"
            or e["source"] != "source:sec-edgar" or e["operation"] != "list_filings"
            or e["status"] not in {"complete", "partial", "empty"}
            or e["content_hash"] != d["source_envelope_hash"]
            or e["connector_invocation_ref"] != d["connector_invocation_ref"]
            or invocation["content_hash"] != d["connector_invocation_hash"]
            or parameters["issuer"] != issuer
            or parameters.get("cursor") not in (None, ROOT_CURSOR)
            or d["parameters"] != parameters
            or d["query_hash"] != content_hash({"operation": "list_filings", "parameters": parameters})):
        raise SecReportingProfileError("reporting profile source or query binding differs")
    created = datetime.fromisoformat(d["created_at"]).date()
    if not 0 <= (as_of - created).days <= MAX_PROFILE_AGE_DAYS:
        raise SecReportingProfileUnavailable("held SEC reporting profile is stale or future-dated")
    try:
        reader = spool() if callable(spool) else spool
        raw = reader.read_object(e["raw_response_hash"])
    except RawSpoolError as exc:
        raise SecReportingProfileUnavailable("SEC reporting profile original is unavailable") from exc
    if len(raw) > MAX_ROOT_BYTES or hashlib.sha256(raw).hexdigest() != e["raw_response_hash"]:
        raise SecReportingProfileError("SEC reporting profile original bytes differ or exceed bound")
    payload = json.loads(raw)
    if str(payload.get("cik", "")).zfill(10) != issuer:
        raise SecReportingProfileError("SEC reporting profile original issuer differs")
    recent = payload["filings"]["recent"]
    fields = ("form", "accessionNumber", "filingDate", "primaryDocument")
    size = len(recent["form"])
    if size > MAX_RECENT_FILINGS or any(not isinstance(recent.get(k), list) or len(recent[k]) != size for k in fields):
        raise SecReportingProfileError("SEC reporting profile filing columns differ or exceed bound")
    annuals = []
    for i, form in enumerate(recent["form"]):
        base = form.removesuffix("/A") if isinstance(form, str) else ""
        if base not in _ANNUAL:
            continue
        filed = date.fromisoformat(recent["filingDate"][i])
        accession, primary = recent["accessionNumber"][i], recent["primaryDocument"][i]
        if (not isinstance(accession, str) or re.fullmatch(r"\d{10}-\d{2}-\d{6}", accession) is None
                or not isinstance(primary, str) or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\.(?:htm|html|txt)", primary) is None):
            raise SecReportingProfileError("SEC reporting profile annual document identity differs")
        if filed <= as_of:
            annuals.append({"form": form, "filed": filed.isoformat(), "accession": accession,
                            "primary_document": primary, "family": _ANNUAL[base]})
    if not annuals:
        raise SecReportingProfileUnavailable("held SEC index does not prove an annual reporting family")
    latest_day = max(item["filed"] for item in annuals)
    latest = [item for item in annuals if item["filed"] == latest_day]
    if len({item["family"] for item in latest}) != 1:
        raise SecReportingProfileError("latest SEC annual reporting families conflict")
    selected = max(latest, key=lambda item: item["accession"])
    body = {"contract_ref": CONTRACT_REF, "company_ref": company_ref, "issuer": issuer,
            "family": selected["family"], "annual_descriptor": selected,
            "discovery_ref": d["id"], "discovery_hash": d["content_hash"],
            "source_envelope_ref": e["id"], "source_envelope_hash": e["content_hash"],
            "raw_hash": e["raw_response_hash"], "query_hash": d["query_hash"]}
    return {**body, "content_hash": content_hash(body)}


def current_profile(connection, spool, *, mission_version_ref, company_ref, issuer, as_of, deadline=None):
    """Latest current mission root candidate; never scan another company/history."""
    rows = bounded_rows(connection,
        "SELECT record_id FROM coverage_mission_source_discoveries "
        "WHERE mission_version_ref=? AND company_ref=? AND source_ref='source:sec-edgar' "
        "ORDER BY created_at DESC,record_id DESC LIMIT 8",
        (mission_version_ref, company_ref), deadline=deadline)
    for row in rows:
        d = _record(connection, "coverage_mission_source_discoveries", "record_id", row[0])
        # An archived column document is not a complete root profile.
        if d.get("parameters", {}).get("cursor") not in (None, ROOT_CURSOR):
            continue
        return profile_from_discovery(connection, spool, discovery_ref=row[0],
            company_ref=company_ref, issuer=issuer, as_of=as_of)
    return None


def allows_form(profile, form):
    if profile is None:
        return form not in FOREIGN_FORMS
    family = profile["family"]
    if family == "domestic":
        return form not in FOREIGN_FORMS
    annual = "40-F" if family == "foreign_40f" else "20-F"
    return form in {annual, annual + "/A", "6-K", "6-K/A"}


def verify_profile(connection, spool, profile, *, company_ref, issuer, as_of):
    if not isinstance(profile, dict) or profile.get("contract_ref") != CONTRACT_REF:
        raise SecReportingProfileError("foreign query lacks a reporting profile")
    expected = profile_from_discovery(connection, spool, discovery_ref=profile["discovery_ref"],
        company_ref=company_ref, issuer=issuer, as_of=as_of)
    if profile != expected:
        raise SecReportingProfileError("foreign reporting profile proof differs from held authority")
    return expected


def routing_query_hash(parameters, profile):
    """Local routing identity; the governed connector parameters stay unchanged."""
    return content_hash({"contract_ref": CONTRACT_REF, "profile_hash": profile["content_hash"],
                         "connector_query_hash": content_hash({"operation": "list_filings", "parameters": parameters})})
