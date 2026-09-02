"""Credential-free SEC submissions and Company Facts adapters.

The SEC JSON response is a provider document, not Dalton's normalized
``list_filings`` schema.  This module keeps the source-specific normalizer in
one place so the runner and the authority resolver can run the same
normalization over the exact persisted raw bytes.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Callable

from .connector_runner import validate_adapter_transport_observation
from .public_http_transport import PublicHttpRequest, PublicHttpTransport
from .store import canonical_json, content_hash


DEFAULT_USER_AGENT = "Dalton Research Agent public-read-only canary"
_ACCESSION_RE = re.compile(r"^\d{10}-\d{2}-\d{6}$")
_XBRL_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9]{0,127}$")
_TAXONOMY_RE = re.compile(r"^[A-Za-z][A-Za-z0-9-]{0,63}$")
_FRAME_RE = re.compile(r"^CY(?P<year>\d{4})Q(?P<quarter>[1-4])$")


class SecPublicAdapterError(ValueError):
    """The public response cannot be normalized without guessing."""


def _text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SecPublicAdapterError(f"{name} must be a non-empty string")
    return value.strip()


def _issuer(value: Any) -> str:
    text = _text(value, "issuer")
    digits = text.removeprefix("CIK").lstrip("0") or "0"
    if not digits.isdigit() or len(digits) > 10:
        raise SecPublicAdapterError("issuer must be a CIK with at most ten digits")
    return digits.zfill(10)


def _date(value: Any, name: str) -> str:
    text = _text(value, name)
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        raise SecPublicAdapterError(f"{name} must be YYYY-MM-DD")
    try:
        date.fromisoformat(text)
    except ValueError as exc:
        raise SecPublicAdapterError(f"{name} is not a calendar date") from exc
    return text


def _as_array(recent: Mapping[str, Any], name: str) -> list[Any]:
    value = recent.get(name)
    if not isinstance(value, list):
        raise SecPublicAdapterError(f"SEC filings.recent.{name} must be an array")
    return value


def _strict_json(raw: bytes) -> Any:
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        names: set[str] = set()
        result: dict[str, Any] = {}
        for key, value in items:
            if key in names:
                raise SecPublicAdapterError("SEC JSON contains duplicate object keys")
            names.add(key)
            result[key] = value
        return result

    def constant(value: str) -> Any:
        raise SecPublicAdapterError(f"SEC JSON contains non-standard number: {value}")

    try:
        return json.loads(raw.decode("utf-8"), object_pairs_hook=pairs, parse_constant=constant)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SecPublicAdapterError("SEC response is not strict UTF-8 JSON") from exc


def _retry_after_ms(headers: Mapping[str, str]) -> int | None:
    raw = headers.get("retry-after")
    if raw is None:
        return None
    if not re.fullmatch(r"[0-9]{1,6}", raw.strip()):
        return None
    value = int(raw.strip()) * 1000
    return value if 1 <= value <= 600_000 else None


def normalize_sec_submissions(
    payload: Any,
    parameters: Mapping[str, Any],
    *,
    provider_status: int,
) -> dict[str, Any]:
    """Normalize one SEC submissions document into the frozen output schema."""
    if not isinstance(payload, Mapping):
        raise SecPublicAdapterError("SEC submissions body must be an object")
    if not isinstance(parameters, Mapping):
        raise SecPublicAdapterError("adapter parameters must be an object")
    issuer = _issuer(parameters.get("issuer"))
    form = _text(parameters.get("form"), "form")
    date_from = _date(parameters.get("date_from"), "date_from")
    date_to = _date(parameters.get("date_to"), "date_to")
    if date_from > date_to:
        raise SecPublicAdapterError("date_from must not be after date_to")
    limit = parameters.get("limit")
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise SecPublicAdapterError("limit must be a positive integer")
    reported_cik = payload.get("cik")
    if reported_cik is not None and _issuer(str(reported_cik)) != issuer:
        raise SecPublicAdapterError("SEC CIK does not match the requested issuer")
    filings = payload.get("filings")
    if not isinstance(filings, Mapping) or not isinstance(filings.get("recent"), Mapping):
        raise SecPublicAdapterError("SEC submissions body lacks filings.recent")
    recent = filings["recent"]
    columns = {
        name: _as_array(recent, name)
        for name in ("accessionNumber", "form", "filingDate", "primaryDocument")
    }
    lengths = {len(value) for value in columns.values()}
    if len(lengths) != 1:
        raise SecPublicAdapterError("SEC recent filing columns have different lengths")
    revisions = recent.get("amendmentOf")
    if revisions is None:
        revisions = [None] * len(columns["accessionNumber"])
    if not isinstance(revisions, list) or len(revisions) != len(columns["accessionNumber"]):
        raise SecPublicAdapterError("SEC amendmentOf column is malformed")

    # Validate provider identity across the complete recent document before
    # applying the caller's form/date filter.  Otherwise a duplicate outside
    # the selected window could be hidden by normalization and later make the
    # persisted authority ambiguous.
    all_accessions: set[str] = set()
    for index, value in enumerate(columns["accessionNumber"]):
        accession = _text(value, f"accessionNumber[{index}]")
        if not _ACCESSION_RE.fullmatch(accession):
            raise SecPublicAdapterError("SEC accession format is invalid")
        if accession in all_accessions:
            raise SecPublicAdapterError("SEC submissions contain duplicate accessions")
        all_accessions.add(accession)

    records: list[dict[str, Any]] = []
    refs: list[str] = []
    for index in range(len(columns["accessionNumber"])):
        accession = _text(columns["accessionNumber"][index], f"accessionNumber[{index}]")
        if not _ACCESSION_RE.fullmatch(accession):
            raise SecPublicAdapterError("SEC accession format is invalid")
        filing_form = _text(columns["form"][index], f"form[{index}]")
        filing_date = _date(columns["filingDate"][index], f"filingDate[{index}]")
        primary_document = _text(columns["primaryDocument"][index], f"primaryDocument[{index}]")
        # SEC's submissions feed legitimately uses relative XSL paths for
        # older ownership filings.  Keep those as data; reject only absolute
        # or traversal paths because this adapter never dereferences them.
        path_parts = primary_document.split("/")
        if (
            "\\" in primary_document
            or primary_document.startswith("/")
            or any(part in {"", ".", ".."} for part in path_parts)
        ):
            raise SecPublicAdapterError("SEC primary document path is unsafe")
        form_matches = filing_form == form or (form == "10-Q" and filing_form == "10-Q/A")
        if not form_matches or filing_date < date_from or filing_date > date_to:
            continue
        revision = revisions[index]
        if filing_form.endswith("/A") and revision is None:
            # The SEC submissions document does not normally expose the
            # amended accession. Without an explicit provider field this
            # adapter cannot invent a revision edge.
            raise SecPublicAdapterError(
                "SEC amendment lacks explicit revision authority"
            )
        if revision is not None:
            revision = _text(revision, f"amendmentOf[{index}]")
            if not _ACCESSION_RE.fullmatch(revision):
                raise SecPublicAdapterError("SEC amendment accession format is invalid")
        record = {
            "accession": accession,
            "form": filing_form,
            "filing_date": filing_date,
            "primary_document": primary_document,
            "revision_of": revision,
        }
        record_ref = f"sec:filing:{accession}"
        records.append({
            "record_ref": record_ref,
            "revision_of_ref": None if revision is None else f"sec:filing:{revision}",
            "record_hash": content_hash(record),
        })
        refs.append(record_ref)
    if len(refs) != len(set(refs)):
        raise SecPublicAdapterError("SEC submissions contain duplicate accessions")
    recent_dates = [
        _date(item, "filingDate") for item in columns["filingDate"]
    ]
    if not recent_dates or date_from < min(recent_dates) or date_to > max(recent_dates):
        # ``filings.files`` points at additional provider documents, but this
        # minimal public WorkOrder has no second fetch authority.  Refusing a
        # window outside recent avoids claiming enumerated completeness.
        raise SecPublicAdapterError("requested date window is outside SEC recent coverage")
    if len(records) > limit:
        # A bounded page cannot claim enumerated completeness when it silently
        # truncates the provider result.  Pagination for this endpoint is not
        # available in the frozen contract, so fail closed instead.
        raise SecPublicAdapterError("SEC result exceeds the declared limit")
    return {
        "records": records,
        "source_record_refs": refs,
        "request_cursor": None,
        "next_cursor": None,
        "page_ordinal": 1,
        "provider_status": provider_status,
    }


def _xbrl_name(value: Any, name: str) -> str:
    text = _text(value, name)
    if _XBRL_NAME_RE.fullmatch(text) is None:
        raise SecPublicAdapterError(f"{name} is not a safe XBRL name")
    return text


def _taxonomy(value: Any) -> str:
    text = _text(value, "taxonomy")
    if _TAXONOMY_RE.fullmatch(text) is None:
        raise SecPublicAdapterError("taxonomy is not a safe XBRL taxonomy name")
    return text


def _decimal_integer(value: Any, name: str) -> Decimal:
    if isinstance(value, bool):
        raise SecPublicAdapterError(f"{name} must be an integer")
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, str) and re.fullmatch(r"-?(0|[1-9][0-9]*)", value):
        return Decimal(value)
    raise SecPublicAdapterError(f"{name} must be an exact integer")


# Closed form registry for the company-facts quarterly comparison.  ``10-Q``
# is the historical rule; ``10-K`` (P9b, 2026-09-02) admits the fourth-quarter
# pair that some issuers (Accenture) report inside the annual filing.  Both
# forms use the same same-accession quarterly selection below; a 10-K that
# carries only fiscal-year facts fails closed.
SEC_COMPANY_FACTS_FORMS: tuple[str, ...] = ("10-Q", "10-K")


def company_facts_selection_basis(form: str) -> str:
    """Frozen selection-basis label for one admitted company-facts form."""

    if form not in SEC_COMPANY_FACTS_FORMS:
        raise SecPublicAdapterError("company facts form is outside the frozen registry")
    return f"ordered_allowlist_latest_{form}"


def _quarterly_fact(
    value: Any,
    *,
    index: int,
    expected_form: str,
    filed_from: str,
    filed_to: str,
) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        raise SecPublicAdapterError(f"SEC company concept unit row[{index}] must be an object")
    form = _text(value.get("form"), f"units.row[{index}].form")
    if form != expected_form:
        return None
    frame = value.get("frame")
    if not isinstance(frame, str) or _FRAME_RE.fullmatch(frame) is None:
        return None
    accession = _text(value.get("accn"), f"units.row[{index}].accn")
    if _ACCESSION_RE.fullmatch(accession) is None:
        raise SecPublicAdapterError("SEC company concept accession format is invalid")
    start = _date(value.get("start"), f"units.row[{index}].start")
    end = _date(value.get("end"), f"units.row[{index}].end")
    filed = _date(value.get("filed"), f"units.row[{index}].filed")
    if start > end:
        raise SecPublicAdapterError("SEC company concept fact period is inverted")
    if filed < filed_from or filed > filed_to:
        return None
    duration = (date.fromisoformat(end) - date.fromisoformat(start)).days + 1
    if duration < 70 or duration > 105:
        return None
    amount = _decimal_integer(value.get("val"), f"units.row[{index}].val")
    fy = value.get("fy")
    if isinstance(fy, bool) or not isinstance(fy, int) or fy < 1900 or fy > 2200:
        raise SecPublicAdapterError("SEC company concept fy is invalid")
    fp = _text(value.get("fp"), f"units.row[{index}].fp")
    record = {
        "accession": accession,
        "start": start,
        "end": end,
        "filed": filed,
        "fy": fy,
        "fp": fp,
        "form": form,
        "frame": frame,
        "value": format(amount, "f"),
    }
    record["record_hash"] = content_hash(record)
    return record


def _prior_frame(frame: str) -> str:
    match = _FRAME_RE.fullmatch(frame)
    if match is None:
        raise SecPublicAdapterError("SEC company concept frame is invalid")
    return f"CY{int(match.group('year')) - 1}Q{match.group('quarter')}"


def normalize_sec_company_concept(
    payload: Any,
    parameters: Mapping[str, Any],
    *,
    provider_status: int,
) -> dict[str, Any]:
    """Select one exact current/prior quarterly pair and recompute YoY growth.

    The pair must come from the same accession of the requested form (10-Q,
    or 10-K for issuers that report the fourth-quarter pair inside the annual
    filing).  That binds both values to one comparative filing and avoids
    silently mixing later restatements, different duration contexts, annual
    facts, or two taxonomy concepts.  A 10-K that only carries fiscal-year
    facts fails closed here; the FY - 9M derivation is a separate, not yet
    frozen rule.
    """

    if not isinstance(payload, Mapping):
        raise SecPublicAdapterError("SEC company concept body must be an object")
    if not isinstance(parameters, Mapping):
        raise SecPublicAdapterError("adapter parameters must be an object")
    expected_fields = {
        "cik", "taxonomy", "concept", "unit", "form", "filed_from", "filed_to",
    }
    if set(parameters) != expected_fields:
        raise SecPublicAdapterError("company concept parameters have an invalid closed shape")
    cik = _issuer(parameters.get("cik"))
    taxonomy = _taxonomy(parameters.get("taxonomy"))
    concept = _xbrl_name(parameters.get("concept"), "concept")
    unit = _text(parameters.get("unit"), "unit")
    form = _text(parameters.get("form"), "form")
    filed_from = _date(parameters.get("filed_from"), "filed_from")
    filed_to = _date(parameters.get("filed_to"), "filed_to")
    filing_window_days = (
        date.fromisoformat(filed_to) - date.fromisoformat(filed_from)
    ).days
    if filing_window_days < 0 or filing_window_days > 400:
        raise SecPublicAdapterError(
            "company concept filing window must span 0..400 days"
        )
    if taxonomy != "us-gaap" or unit != "USD" or form not in SEC_COMPANY_FACTS_FORMS:
        raise SecPublicAdapterError(
            "company concept canary is closed to us-gaap/USD/10-Q|10-K"
        )
    reported_cik = _issuer(str(payload.get("cik")))
    if reported_cik != cik:
        raise SecPublicAdapterError("SEC company concept CIK does not match the request")
    if _text(payload.get("taxonomy"), "payload.taxonomy") != taxonomy:
        raise SecPublicAdapterError("SEC company concept taxonomy does not match the request")
    if _text(payload.get("tag"), "payload.tag") != concept:
        raise SecPublicAdapterError("SEC company concept tag does not match the request")
    entity_name = _text(payload.get("entityName"), "payload.entityName")
    label = _text(payload.get("label"), "payload.label")
    units = payload.get("units")
    if not isinstance(units, Mapping) or set(units) != {unit}:
        raise SecPublicAdapterError("SEC company concept units do not match the exact requested unit")
    rows = units[unit]
    if not isinstance(rows, list):
        raise SecPublicAdapterError("SEC company concept unit payload must be an array")
    facts = [
        fact
        for index, row in enumerate(rows)
        if (fact := _quarterly_fact(
            row,
            index=index,
            expected_form=form,
            filed_from=filed_from,
            filed_to=filed_to,
        )) is not None
    ]
    if not facts:
        raise SecPublicAdapterError("SEC company concept has no eligible quarterly facts")

    # Later filings repeat historical values.  A current candidate must have
    # its exact prior-year comparative in the same accession.  Evaluate the
    # most recent period first and fail closed on ambiguous contexts.
    current: dict[str, Any] | None = None
    prior: dict[str, Any] | None = None
    for candidate in sorted(
        facts,
        key=lambda item: (item["end"], item["filed"], item["accession"]),
        reverse=True,
    ):
        prior_candidates = [
            item
            for item in facts
            if item["accession"] == candidate["accession"]
            and item["frame"] == _prior_frame(candidate["frame"])
            and item["fp"] == candidate["fp"]
            and 350
            <= (
                date.fromisoformat(candidate["end"])
                - date.fromisoformat(item["end"])
            ).days
            <= 380
            and abs(
                (date.fromisoformat(candidate["end"]) - date.fromisoformat(candidate["start"])).days
                - (date.fromisoformat(item["end"]) - date.fromisoformat(item["start"])).days
            )
            <= 7
        ]
        unique_prior = {
            (item["start"], item["end"], item["value"], item["record_hash"]): item
            for item in prior_candidates
        }
        if len(unique_prior) > 1:
            raise SecPublicAdapterError(
                "SEC company concept comparative period is ambiguous"
            )
        if len(unique_prior) == 1:
            current = candidate
            prior = next(iter(unique_prior.values()))
            break
    if current is None or prior is None:
        raise SecPublicAdapterError(
            "SEC company concept lacks a same-filing prior-year quarterly comparison"
        )

    try:
        current_value = Decimal(current["value"])
        prior_value = Decimal(prior["value"])
        if prior_value <= 0:
            raise SecPublicAdapterError("prior-year company concept value must be positive")
        growth = ((current_value / prior_value) - Decimal(1)) * Decimal(100)
        growth = growth.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except (InvalidOperation, ZeroDivisionError) as exc:
        raise SecPublicAdapterError("company concept growth cannot be computed exactly") from exc
    growth_text = format(growth, "f")
    if growth_text == "-0.00":
        growth_text = "0.00"
    record_refs = [
        f"sec:company-concept:{item['accession']}:{taxonomy}:{concept}:{item['frame']}"
        for item in (current, prior)
    ]
    normalized = {
        "schema_version": "0.1",
        "entity_name": entity_name,
        "cik": cik,
        "taxonomy": taxonomy,
        "concept": concept,
        "label": label,
        "unit": unit,
        "form": form,
        "filed_from": filed_from,
        "filed_to": filed_to,
        "current": current,
        "prior": prior,
        "growth_percent": growth_text,
        "source_record_refs": record_refs,
        "next_cursor": None,
        "provider_status": provider_status,
    }
    normalized["content_hash"] = content_hash(normalized)
    return normalized


def _revenue_concept_candidates(value: Any) -> list[str]:
    if not isinstance(value, list) or not 1 <= len(value) <= 8:
        raise SecPublicAdapterError(
            "concept_candidates must contain 1..8 XBRL concepts"
        )
    candidates = [_xbrl_name(item, "concept_candidates[]") for item in value]
    if len(set(candidates)) != len(candidates):
        raise SecPublicAdapterError("concept_candidates must be unique")
    return candidates


def _latest_company_facts_accession(
    facts: Mapping[str, Any],
    *,
    unit: str,
    form: str,
    filed_from: str,
    filed_to: str,
) -> str:
    latest_filed: str | None = None
    latest_accessions: set[str] = set()
    for concept in facts.values():
        if not isinstance(concept, Mapping):
            continue
        units = concept.get("units")
        if not isinstance(units, Mapping):
            continue
        rows = units.get(unit)
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, Mapping) or row.get("form") != form:
                continue
            try:
                filed = _date(row.get("filed"), "companyfacts.row.filed")
                accession = _text(row.get("accn"), "companyfacts.row.accn")
            except SecPublicAdapterError:
                continue
            if (
                filed < filed_from
                or filed > filed_to
                or _ACCESSION_RE.fullmatch(accession) is None
            ):
                continue
            if latest_filed is None or filed > latest_filed:
                latest_filed = filed
                latest_accessions = {accession}
            elif filed == latest_filed:
                latest_accessions.add(accession)
    if latest_filed is None:
        raise SecPublicAdapterError(
            f"SEC company facts has no {form} accession in the filing window"
        )
    if len(latest_accessions) != 1:
        raise SecPublicAdapterError(
            f"SEC company facts latest {form} accession is ambiguous"
        )
    return next(iter(latest_accessions))


def normalize_sec_company_facts(
    payload: Any,
    parameters: Mapping[str, Any],
    *,
    provider_status: int,
) -> dict[str, Any]:
    """Resolve one revenue concept on the latest 10-Q accession.

    The plan freezes an ordered, closed allowlist.  A concept is eligible only
    when its exact current/prior pair belongs to the latest 10-Q accession in
    the bounded filing window.  The first eligible concept wins, so the
    immutable plan allowlist is also the explicit semantic priority.
    """

    if not isinstance(payload, Mapping):
        raise SecPublicAdapterError("SEC company facts body must be an object")
    if not isinstance(parameters, Mapping):
        raise SecPublicAdapterError("adapter parameters must be an object")
    expected_fields = {
        "cik", "taxonomy", "concept_candidates", "unit", "form",
        "filed_from", "filed_to",
    }
    if set(parameters) != expected_fields:
        raise SecPublicAdapterError("company facts parameters have an invalid closed shape")
    cik = _issuer(parameters.get("cik"))
    taxonomy = _taxonomy(parameters.get("taxonomy"))
    candidates = _revenue_concept_candidates(parameters.get("concept_candidates"))
    unit = _text(parameters.get("unit"), "unit")
    form = _text(parameters.get("form"), "form")
    filed_from = _date(parameters.get("filed_from"), "filed_from")
    filed_to = _date(parameters.get("filed_to"), "filed_to")
    filing_window_days = (
        date.fromisoformat(filed_to) - date.fromisoformat(filed_from)
    ).days
    if filing_window_days < 0 or filing_window_days > 400:
        raise SecPublicAdapterError(
            "company facts filing window must span 0..400 days"
        )
    if taxonomy != "us-gaap" or unit != "USD" or form not in SEC_COMPANY_FACTS_FORMS:
        raise SecPublicAdapterError(
            "company facts resolver is closed to us-gaap/USD/10-Q|10-K"
        )
    if _issuer(str(payload.get("cik"))) != cik:
        raise SecPublicAdapterError("SEC company facts CIK does not match the request")
    entity_name = _text(payload.get("entityName"), "payload.entityName")
    all_facts = payload.get("facts")
    if not isinstance(all_facts, Mapping):
        raise SecPublicAdapterError("SEC company facts body lacks facts")
    taxonomy_facts = all_facts.get(taxonomy)
    if not isinstance(taxonomy_facts, Mapping):
        raise SecPublicAdapterError("SEC company facts lacks the requested taxonomy")
    latest_accession = _latest_company_facts_accession(
        taxonomy_facts,
        unit=unit,
        form=form,
        filed_from=filed_from,
        filed_to=filed_to,
    )
    eligible: list[dict[str, Any]] = []
    skippable = (
        "has no eligible quarterly facts",
        "lacks a same-filing prior-year quarterly comparison",
    )
    for candidate in candidates:
        concept = taxonomy_facts.get(candidate)
        if concept is None:
            continue
        if not isinstance(concept, Mapping):
            raise SecPublicAdapterError("SEC company facts concept must be an object")
        concept_payload = {
            "cik": payload.get("cik"),
            "taxonomy": taxonomy,
            "tag": candidate,
            "label": concept.get("label"),
            "entityName": entity_name,
            "units": concept.get("units"),
        }
        exact_parameters = {
            "cik": cik,
            "taxonomy": taxonomy,
            "concept": candidate,
            "unit": unit,
            "form": form,
            "filed_from": filed_from,
            "filed_to": filed_to,
        }
        try:
            normalized = normalize_sec_company_concept(
                concept_payload,
                exact_parameters,
                provider_status=provider_status,
            )
        except SecPublicAdapterError as exc:
            if any(message in str(exc) for message in skippable):
                continue
            raise
        if normalized["current"]["accession"] == latest_accession:
            eligible.append(normalized)
    if not eligible:
        raise SecPublicAdapterError(
            f"no allowlisted revenue concept resolves on the latest {form} accession"
        )
    selected = dict(eligible[0])
    selected.pop("content_hash")
    selected["concept_candidates"] = candidates
    selected["eligible_concepts"] = [item["concept"] for item in eligible]
    selected["latest_accession"] = latest_accession
    selected["selection_basis"] = company_facts_selection_basis(form)
    selected["content_hash"] = content_hash(selected)
    return selected


def _observation(
    request: Mapping[str, Any],
    *,
    outcome: str,
    provider_request_id: str | None,
    provider_status: int | None,
    structured_output: Mapping[str, Any] | None,
    source_record_refs: list[str],
    cursor: str | None,
    error: Mapping[str, Any] | None,
    retry_after_ms: int | None = None,
    bytes_written: int = 0,
) -> dict[str, Any]:
    base = {
        "protocol_version": "0.1",
        "request_hash": request["content_hash"],
        "outcome": outcome,
        "provider_request_id": provider_request_id,
        "provider_status_code": provider_status,
        "retry_after_ms": retry_after_ms,
        "structured_output": structured_output,
        "source_record_refs": source_record_refs,
        "cursor": cursor,
        "provider_usage": {
            "calls": 1,
            "bytes": bytes_written,
            "records": len(source_record_refs),
        },
        "error": None if error is None else dict(error),
    }
    base["content_hash"] = content_hash(base)
    return validate_adapter_transport_observation(base)


class SecPublicHttpAdapter:
    """Bound GET adapter for the public SEC submissions endpoint."""

    def __init__(
        self,
        *,
        transport: PublicHttpTransport | None = None,
        user_agent: str = DEFAULT_USER_AGENT,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.transport = transport or PublicHttpTransport()
        self.user_agent = _text(user_agent, "user_agent")
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def __call__(self, request: Mapping[str, Any], raw_sink: Any, credential_handle: Any | None = None) -> dict[str, Any]:
        if credential_handle is not None:
            raise SecPublicAdapterError("public SEC adapter does not accept credential handles")
        if request.get("operation", "list_filings") != "list_filings":
            raise SecPublicAdapterError("SEC submissions adapter operation is not list_filings")
        parameters = request["parameters"]
        issuer = _issuer(parameters["issuer"])
        url = f"https://data.sec.gov/submissions/CIK{issuer}.json"
        try:
            deadline = datetime.fromisoformat(str(request["deadline_at"]).replace("Z", "+00:00"))
            if deadline.tzinfo is None:
                raise ValueError
            now = self.clock()
            if not isinstance(now, datetime) or now.tzinfo is None:
                raise ValueError
            remaining = (deadline.astimezone(timezone.utc) - now.astimezone(timezone.utc)).total_seconds()
        except (TypeError, ValueError) as exc:
            raise SecPublicAdapterError("adapter deadline is invalid") from exc
        if remaining <= 0:
            raise SecPublicAdapterError("adapter deadline has expired")
        response = self.transport.request(
            PublicHttpRequest("GET", url, {
                "User-Agent": self.user_agent,
                "Accept": "application/json",
            }),
            raw_sink,
            allowed_hosts=request["allowed_hosts"],
            allow_redirects=request["network_policy"]["allow_redirects"],
            max_redirects=request["network_policy"]["max_redirects"],
            max_response_bytes=request["max_response_bytes"],
            timeout_seconds=max(0.001, remaining),
        )
        provider_request_id = "sec-http:" + hashlib.sha256(response.body).hexdigest()
        if response.status == 429:
            retry_after = _retry_after_ms(response.headers)
            if retry_after is None:
                return _observation(
                    request, outcome="failed", provider_request_id=provider_request_id,
                    provider_status=response.status, structured_output=None,
                    source_record_refs=[], cursor=None,
                    error={"code": "rate_limited_missing_retry_after", "message": "SEC 429 lacked a bounded Retry-After", "retryable": True},
                    bytes_written=response.bytes_written,
                )
            return _observation(
                request, outcome="rate_limited", provider_request_id=provider_request_id,
                provider_status=response.status, structured_output=None,
                source_record_refs=[], cursor=None,
                error=None, bytes_written=response.bytes_written,
                retry_after_ms=retry_after,
            )
        if response.status != 200:
            return _observation(
                request, outcome="failed", provider_request_id=provider_request_id,
                provider_status=response.status, structured_output=None,
                source_record_refs=[], cursor=None,
                error={"code": "http_status", "message": f"SEC returned HTTP {response.status}", "retryable": response.status in {408, 429, 500, 502, 503, 504}},
                bytes_written=response.bytes_written,
            )
        try:
            payload = _strict_json(response.body)
            structured = normalize_sec_submissions(
                payload, parameters, provider_status=response.status
            )
        except (UnicodeDecodeError, json.JSONDecodeError, SecPublicAdapterError) as exc:
            return _observation(
                request, outcome="failed", provider_request_id=provider_request_id,
                provider_status=response.status, structured_output=None,
                source_record_refs=[], cursor=None,
                error={"code": "normalization_error", "message": str(exc), "retryable": False},
                bytes_written=response.bytes_written,
            )
        return _observation(
            request, outcome="succeeded", provider_request_id=provider_request_id,
            provider_status=response.status, structured_output=structured,
            source_record_refs=list(structured["source_record_refs"]), cursor=None,
            error=None, bytes_written=response.bytes_written,
        )


class SecCompanyFactsHttpAdapter:
    """Bound GET adapter for latest-accession SEC Company Facts resolution."""

    def __init__(
        self,
        *,
        transport: PublicHttpTransport | None = None,
        user_agent: str = DEFAULT_USER_AGENT,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.transport = transport or PublicHttpTransport()
        self.user_agent = _text(user_agent, "user_agent")
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def __call__(
        self,
        request: Mapping[str, Any],
        raw_sink: Any,
        credential_handle: Any | None = None,
    ) -> dict[str, Any]:
        if credential_handle is not None:
            raise SecPublicAdapterError(
                "public SEC company facts adapter does not accept credential handles"
            )
        if request.get("operation", "get_company_facts") != "get_company_facts":
            raise SecPublicAdapterError(
                "SEC company concept adapter operation is not get_company_facts"
            )
        parameters = request["parameters"]
        cik = _issuer(parameters.get("cik"))
        url = f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
        try:
            deadline = datetime.fromisoformat(
                str(request["deadline_at"]).replace("Z", "+00:00")
            )
            if deadline.tzinfo is None:
                raise ValueError
            now = self.clock()
            if not isinstance(now, datetime) or now.tzinfo is None:
                raise ValueError
            remaining = (
                deadline.astimezone(timezone.utc) - now.astimezone(timezone.utc)
            ).total_seconds()
        except (TypeError, ValueError) as exc:
            raise SecPublicAdapterError("adapter deadline is invalid") from exc
        if remaining <= 0:
            raise SecPublicAdapterError("adapter deadline has expired")
        response = self.transport.request(
            PublicHttpRequest(
                "GET",
                url,
                {"User-Agent": self.user_agent, "Accept": "application/json"},
            ),
            raw_sink,
            allowed_hosts=request["allowed_hosts"],
            allow_redirects=request["network_policy"]["allow_redirects"],
            max_redirects=request["network_policy"]["max_redirects"],
            max_response_bytes=request["max_response_bytes"],
            timeout_seconds=max(0.001, remaining),
        )
        provider_request_id = "sec-http:" + hashlib.sha256(response.body).hexdigest()
        if response.status == 429:
            retry_after = _retry_after_ms(response.headers)
            if retry_after is not None:
                return _observation(
                    request,
                    outcome="rate_limited",
                    provider_request_id=provider_request_id,
                    provider_status=response.status,
                    structured_output=None,
                    source_record_refs=[],
                    cursor=None,
                    error=None,
                    retry_after_ms=retry_after,
                    bytes_written=response.bytes_written,
                )
        if response.status != 200:
            return _observation(
                request,
                outcome="failed",
                provider_request_id=provider_request_id,
                provider_status=response.status,
                structured_output=None,
                source_record_refs=[],
                cursor=None,
                error={
                    "code": "http_status",
                    "message": f"SEC returned HTTP {response.status}",
                    "retryable": response.status in {408, 429, 500, 502, 503, 504},
                },
                retry_after_ms=None,
                bytes_written=response.bytes_written,
            )
        try:
            structured = normalize_sec_company_facts(
                _strict_json(response.body),
                parameters,
                provider_status=response.status,
            )
        except SecPublicAdapterError as exc:
            return _observation(
                request,
                outcome="failed",
                provider_request_id=provider_request_id,
                provider_status=response.status,
                structured_output=None,
                source_record_refs=[],
                cursor=None,
                error={
                    "code": "normalization_error",
                    "message": str(exc),
                    "retryable": False,
                },
                bytes_written=response.bytes_written,
            )
        return _observation(
            request,
            outcome="succeeded",
            provider_request_id=provider_request_id,
            provider_status=response.status,
            structured_output=structured,
            source_record_refs=list(structured["source_record_refs"]),
            cursor=None,
            error=None,
            bytes_written=response.bytes_written,
        )


# Compatibility alias for the pre-resolver development name.
SecCompanyConceptHttpAdapter = SecCompanyFactsHttpAdapter


class SecPublicRouterAdapter:
    """Dispatch the two approved public SEC operations without URL authority."""

    def __init__(
        self,
        *,
        transport: PublicHttpTransport | None = None,
        user_agent: str = DEFAULT_USER_AGENT,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        shared_transport = transport or PublicHttpTransport()
        self.submissions = SecPublicHttpAdapter(
            transport=shared_transport, user_agent=user_agent, clock=clock
        )
        self.company_concept = SecCompanyFactsHttpAdapter(
            transport=shared_transport, user_agent=user_agent, clock=clock
        )

    def __call__(
        self,
        request: Mapping[str, Any],
        raw_sink: Any,
        credential_handle: Any | None = None,
    ) -> dict[str, Any]:
        operation = request.get("operation")
        if operation == "list_filings":
            return self.submissions(request, raw_sink, credential_handle)
        if operation == "get_company_facts":
            return self.company_concept(request, raw_sink, credential_handle)
        raise SecPublicAdapterError("SEC public router operation is not approved")


__all__ = [
    "DEFAULT_USER_AGENT", "SecCompanyConceptHttpAdapter", "SecCompanyFactsHttpAdapter",
    "SecPublicAdapterError",
    "SecPublicHttpAdapter", "SecPublicRouterAdapter", "normalize_sec_company_concept",
    "normalize_sec_company_facts", "normalize_sec_submissions",
]
