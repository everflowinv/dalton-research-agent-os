"""Execute admitted read-only bounded-planner probe WorkOrders.

Tier 1 covers one probe operation: SEC Company Facts source-level coverage.
The executor performs a single bounded public HTTPS fetch per probe (no
credentials, no connector authority), selects the latest 10-Q accession that
carries the queried revenue concept inside the configured filing window, and
returns the ResultEnvelope wire for the source-level ``matches`` contract the
BoundedPlannerControlPlane's ``record_outcome`` expects.  HTTP failures and
transport errors become failed envelopes, which the coverage projection maps
to ``source_unavailable`` outcomes — the loop may re-propose the item on a
later round while budget remains.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from .public_http_transport import PublicHttpTransport, PublicHttpRequest
from .store import content_hash


SCHEMA_VERSION = "0.1"
WORKER_REF = "worker:bounded-probe-sec"
PROBE_PERMISSION_SCOPE = "public_sec_read"
PROBE_OPERATION = "get_company_facts"
SEC_FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
DEFAULT_FORM = "10-Q"
DEFAULT_CONCEPT = "Revenues"


class BoundedProbeExecutionError(RuntimeError):
    """The probe WorkOrder does not match the Tier 1 execution contract."""


class _MemorySink:
    """Collect one bounded public response body in memory."""

    def __init__(self) -> None:
        self.chunks: list[bytes] = []

    def write(self, chunk: bytes) -> int:
        if not isinstance(chunk, (bytes, bytearray)):
            raise BoundedProbeExecutionError("public transport wrote non-bytes")
        self.chunks.append(bytes(chunk))
        return len(chunk)

    @property
    def body(self) -> bytes:
        return b"".join(self.chunks)


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _concept_candidates(query_terms: list[Any]) -> list[str]:
    """Ordered revenue-concept candidates, mirroring the lane's frozen allowlist."""

    candidates: list[str] = []
    for term in query_terms:
        if not isinstance(term, str):
            continue
        if term.startswith("Revenues") or term.startswith("RevenueFromContract") or term.startswith("SalesRevenueNet"):
            if term not in candidates:
                candidates.append(term)
    return candidates or [DEFAULT_CONCEPT]


def _select_latest_accession(
    payload: Mapping[str, Any],
    *,
    concepts: Sequence[str],
    form: str,
    filed_from: str,
    filed_to: str,
) -> str | None:
    facts = payload.get("facts")
    if not isinstance(facts, Mapping):
        return None
    us_gaap = facts.get("us-gaap")
    if not isinstance(us_gaap, Mapping):
        return None
    for concept in concepts:
        best: tuple[str, str] | None = None
        concept_node = us_gaap.get(concept)
        if not isinstance(concept_node, Mapping):
            continue
        units = concept_node.get("units")
        if not isinstance(units, Mapping):
            continue
        for series in units.values():
            if not isinstance(series, list):
                continue
            for fact in series:
                if not isinstance(fact, Mapping):
                    continue
                if fact.get("form") != form:
                    continue
                filed = fact.get("filed")
                acc = fact.get("accn")
                if not isinstance(filed, str) or not isinstance(acc, str):
                    continue
                if not filed_from <= filed <= filed_to:
                    continue
                if best is None or (filed, acc) > best:
                    best = (filed, acc)
        if best is not None:
            return best[1].replace("-", "")
    return None


def execute_probe_work_order(
    work_order: Mapping[str, Any],
    *,
    transport: PublicHttpTransport | Any,
    user_agent: str,
    max_response_bytes: int,
    timeout_seconds: float,
    filed_window_days: int = 400,
    clock: Callable[[], datetime] | None = None,
) -> dict[str, Any]:
    """Execute one Tier 1 probe WorkOrder and return its ResultEnvelope wire."""

    metadata = work_order.get("metadata") or {}
    if metadata.get("permission_scope") != PROBE_PERMISSION_SCOPE:
        raise BoundedProbeExecutionError(
            "probe WorkOrder is outside the public SEC read scope"
        )
    if metadata.get("operation") != PROBE_OPERATION:
        raise BoundedProbeExecutionError(
            "probe WorkOrder operation is not Tier 1 executable"
        )
    parameters = metadata.get("parameters") or {}
    locator = parameters.get("locator")
    if not isinstance(locator, str) or not locator.startswith("company-facts/CIK"):
        raise BoundedProbeExecutionError("probe locator is not a company-facts CIK")
    cik = locator.removeprefix("company-facts/CIK")
    if not cik.isdigit():
        raise BoundedProbeExecutionError("probe locator CIK is not numeric")
    query_terms = parameters.get("query_terms") or []
    concepts = _concept_candidates(query_terms)
    form = DEFAULT_FORM
    for term in query_terms:
        if isinstance(term, str) and term.upper() in {"10-Q", "10-K"}:
            form = term.upper()
    now = clock() if clock is not None else datetime.now(timezone.utc)
    filed_to = now.date().isoformat()
    filed_from = (now - timedelta(days=filed_window_days)).date().isoformat()

    identity = {
        "work_order_ref": work_order.get("id"),
        "operation": PROBE_OPERATION,
        "locator": locator,
        "filed_from": filed_from,
        "filed_to": filed_to,
    }
    envelope = {
        "schema_version": SCHEMA_VERSION,
        "id": "result:bounded-probe:" + content_hash(identity)[:32],
        "created_at": _utcnow(),
        "work_order_ref": work_order.get("id"),
        "invocation_ref": "invocation:bounded-probe:" + content_hash(identity)[:32],
        "outputs": {},
        "actual_side_effects": ["read:public-http"],
        "usage_refs": [],
        "artifact_refs": [],
        "error": None,
        "metadata": {"probe": "sec-company-facts", "filed_from": filed_from,
                     "filed_to": filed_to, "bytes_written": 0},
    }

    sink = _MemorySink()
    try:
        response = transport.request(
            PublicHttpRequest(
                "GET",
                SEC_FACTS_URL.format(cik=cik),
                {"User-Agent": user_agent, "Accept": "application/json"},
            ),
            sink,
            allowed_hosts=["data.sec.gov"],
            allow_redirects=False,
            max_redirects=0,
            max_response_bytes=max_response_bytes,
            timeout_seconds=timeout_seconds,
        )
    except Exception as exc:
        envelope["status"] = "failed"
        envelope["error"] = {
            "code": "SOURCE_UNAVAILABLE",
            "message": f"public SEC fetch failed: {type(exc).__name__}",
        }
        return envelope

    envelope["metadata"]["bytes_written"] = getattr(response, "bytes_written", 0)
    status = getattr(response, "status", None)
    if status != 200:
        envelope["status"] = "failed"
        envelope["error"] = {
            "code": "SOURCE_UNAVAILABLE",
            "message": f"SEC returned HTTP {status}",
        }
        return envelope
    try:
        payload = json.loads(sink.body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        envelope["status"] = "failed"
        envelope["error"] = {
            "code": "SOURCE_UNAVAILABLE",
            "message": "SEC company facts body is not valid JSON",
        }
        return envelope
    accession = _select_latest_accession(
        payload, concepts=concepts, form=form,
        filed_from=filed_from, filed_to=filed_to,
    )
    matches = (
        [{"source_location": f"sec:accession:{accession}"}]
        if accession is not None else []
    )
    envelope["status"] = "succeeded"
    envelope["outputs"] = {"matches": matches}
    return envelope


__all__ = [
    "BoundedProbeExecutionError",
    "WORKER_REF",
    "execute_probe_work_order",
]
