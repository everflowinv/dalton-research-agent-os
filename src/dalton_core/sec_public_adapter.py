"""Credential-free SEC submissions adapter used by the isolated canary.

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
from typing import Any, Callable

from .connector_runner import validate_adapter_transport_observation
from .public_http_transport import PublicHttpRequest, PublicHttpTransport
from .store import canonical_json, content_hash


DEFAULT_USER_AGENT = "Dalton Research Agent public-read-only canary"
_ACCESSION_RE = re.compile(r"^\d{10}-\d{2}-\d{6}$")


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


__all__ = [
    "DEFAULT_USER_AGENT", "SecPublicAdapterError", "SecPublicHttpAdapter",
    "normalize_sec_submissions",
]
