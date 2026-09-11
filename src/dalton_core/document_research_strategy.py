"""Source-neutral planner choices for exact, already-readable document versions.

The planner selects a document and query, never a filesystem path, source
authority override, model route or budget. Source adapters independently
re-resolve the selected registration before execution.
"""
from __future__ import annotations

from collections.abc import Mapping
from datetime import date
import re
from typing import Any

from .store import content_hash

STRATEGY_VERSION = "directed-document:0.1"
FINANCIAL_NOTE_TARGET_REF = "financial_note:diluted_eps_numerator:0.1"
FINANCIAL_NOTE_TARGET_SCHEMA_VERSION = "financial-note-target-0.1"
_STRATEGY_REQUIRED_FIELDS = frozenset({
    "strategy_version", "document_ref", "document_version_hash",
    "query_terms", "query_rationale",
})
STRATEGY_FIELDS = _STRATEGY_REQUIRED_FIELDS | {"evidence_target"}
FINANCIAL_NOTE_TARGET_FIELDS = frozenset({
    "schema_version", "target_ref", "kind", "statement_ingest_ref",
    "statement_filing_hash", "accession", "form", "applicability_kind",
    "periods",
})
_HASH = re.compile(r"[0-9a-f]{64}")
_STATEMENT_INGEST_REF = re.compile(r"statement-ingest:[0-9a-f]{32}")
_ACCESSION = re.compile(r"[0-9]{10}-[0-9]{2}-[0-9]{6}")


class DocumentResearchStrategyError(ValueError):
    pass


def normalize_evidence_target(value: Any) -> dict[str, Any]:
    """Validate the exact typed note target selected from document inventory."""

    if not isinstance(value, Mapping) or set(value) != FINANCIAL_NOTE_TARGET_FIELDS:
        raise DocumentResearchStrategyError("financial note target has an invalid closed shape")
    if (value["schema_version"] != FINANCIAL_NOTE_TARGET_SCHEMA_VERSION
            or value["target_ref"] != FINANCIAL_NOTE_TARGET_REF
            or value["kind"] != "diluted_eps_numerator"
            or value["form"] != "10-K"
            or value["applicability_kind"] not in {"annual", "quarter"}):
        raise DocumentResearchStrategyError("financial note target is unsupported")
    for name in ("statement_ingest_ref", "accession"):
        if not isinstance(value[name], str) or not value[name].strip():
            raise DocumentResearchStrategyError(f"financial note target {name} is empty")
    if _STATEMENT_INGEST_REF.fullmatch(value["statement_ingest_ref"]) is None:
        raise DocumentResearchStrategyError("financial note statement identity is invalid")
    if _ACCESSION.fullmatch(value["accession"]) is None:
        raise DocumentResearchStrategyError("financial note accession is invalid")
    digest = value["statement_filing_hash"]
    if not isinstance(digest, str) or _HASH.fullmatch(digest) is None:
        raise DocumentResearchStrategyError("financial note statement hash is invalid")
    periods = value["periods"]
    if not isinstance(periods, list) or not periods:
        raise DocumentResearchStrategyError("financial note target periods are empty")
    normalized_periods = []
    for period in periods:
        if not isinstance(period, Mapping) or set(period) != {"period_start", "period_end"}:
            raise DocumentResearchStrategyError("financial note target period has an invalid shape")
        try:
            start = date.fromisoformat(period["period_start"]).isoformat()
            end = date.fromisoformat(period["period_end"]).isoformat()
        except (TypeError, ValueError) as exc:
            raise DocumentResearchStrategyError("financial note target period is invalid") from exc
        if (period["period_start"] != start or period["period_end"] != end):
            raise DocumentResearchStrategyError(
                "financial note target period is not canonical")
        if start > end:
            raise DocumentResearchStrategyError("financial note target period is reversed")
        elapsed = (date.fromisoformat(end) - date.fromisoformat(start)).days
        if ((value["applicability_kind"] == "annual" and not 290 < elapsed <= 380)
                or (value["applicability_kind"] == "quarter" and not 60 < elapsed <= 120)):
            raise DocumentResearchStrategyError(
                "financial note period differs from its applicability")
        normalized_periods.append({"period_start": start, "period_end": end})
    if normalized_periods != sorted(normalized_periods, key=lambda item: (
            item["period_start"], item["period_end"])) \
            or len({(item["period_start"], item["period_end"])
                    for item in normalized_periods}) != len(normalized_periods):
        raise DocumentResearchStrategyError(
            "financial note target periods must be sorted and unique")
    return {**dict(value), "periods": normalized_periods}


def validate_financial_note_target(value: Any) -> dict[str, Any]:
    """Public canonical validator shared by target producers and consumers."""

    return normalize_evidence_target(value)


def normalize_strategy(value: Any, *, max_query_terms: int,
                       max_query_term_chars: int) -> dict[str, Any]:
    if (not isinstance(value, Mapping)
            or not _STRATEGY_REQUIRED_FIELDS <= set(value)
            or set(value) - STRATEGY_FIELDS):
        raise DocumentResearchStrategyError("directed document strategy has an invalid closed shape")
    if value["strategy_version"] != STRATEGY_VERSION:
        raise DocumentResearchStrategyError("unsupported directed document strategy")
    for limit in (max_query_terms, max_query_term_chars):
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise DocumentResearchStrategyError("document query policy is unavailable")
    for name in ("document_ref", "query_rationale"):
        if not isinstance(value[name], str) or not value[name].strip():
            raise DocumentResearchStrategyError(f"directed document {name} is empty")
    digest = value["document_version_hash"]
    if not isinstance(digest, str) or _HASH.fullmatch(digest) is None:
        raise DocumentResearchStrategyError("document version hash is invalid")
    terms = value["query_terms"]
    if not isinstance(terms, list) or not 1 <= len(terms) <= max_query_terms:
        raise DocumentResearchStrategyError("query terms exceed configured document policy")
    normalized = []
    for term in terms:
        if not isinstance(term, str) or not term.strip():
            raise DocumentResearchStrategyError("query term is empty")
        term = " ".join(term.split())
        if len(term) > max_query_term_chars or term.casefold() in {t.casefold() for t in normalized}:
            raise DocumentResearchStrategyError("query terms are repeated or exceed configured policy")
        normalized.append(term)
    strategy = {**dict(value), "document_ref": value["document_ref"].strip(),
                "query_rationale": value["query_rationale"].strip(), "query_terms": normalized}
    if "evidence_target" in strategy:
        strategy["evidence_target"] = normalize_evidence_target(strategy["evidence_target"])
    return strategy


def resolve_strategy(value: Any, *, company_ref: str | None,
                     state: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    policy = state.get("document_research_policy")
    if not isinstance(policy, Mapping):
        raise DocumentResearchStrategyError("document research policy is not available in this state")
    strategy = normalize_strategy(value, max_query_terms=policy.get("max_query_terms"),
                                  max_query_term_chars=policy.get("max_query_term_chars"))
    matches = []
    for company in state.get("companies", ()):
        if company.get("company_ref") != company_ref:
            continue
        for document in company.get("readable_documents", ()):
            if (isinstance(document, Mapping)
                    and document.get("document_ref") == strategy["document_ref"]
                    and document.get("document_version_hash") == strategy["document_version_hash"]):
                matches.append(document)
    if len(matches) != 1:
        raise DocumentResearchStrategyError("document version is absent or ambiguous for this company")
    document = matches[0]
    if (document.get("readable") is not True
            or "search_registered_document" not in document.get("operations", ())
            or document.get("authority_hash") != strategy["document_version_hash"]
            or not isinstance(document.get("authority_ref"), str)):
        raise DocumentResearchStrategyError("selected document has no verified directed-reading capability")
    target = strategy.get("evidence_target")
    if target is not None:
        targets = document.get("evidence_targets")
        if not isinstance(targets, list):
            raise DocumentResearchStrategyError(
                "selected document has no verified financial note target")
        matches = []
        for candidate in targets:
            try:
                normalized = normalize_evidence_target(candidate)
            except DocumentResearchStrategyError as exc:
                raise DocumentResearchStrategyError(
                    "document inventory contains an invalid financial note target") from exc
            if normalized == target:
                matches.append(normalized)
        if len(matches) != 1:
            raise DocumentResearchStrategyError(
                "financial note target is absent or ambiguous for this document")
    return strategy, dict(document)


def strategy_identity(value: Mapping[str, Any]) -> dict[str, Any]:
    """A new rationale alone must not purchase another identical query."""
    if (not isinstance(value, Mapping)
            or not _STRATEGY_REQUIRED_FIELDS <= set(value)
            or set(value) - STRATEGY_FIELDS):
        raise DocumentResearchStrategyError("directed document strategy has an invalid closed shape")
    return {key: value[key] for key in sorted(set(value) - {"query_rationale"})}


def inventory_document(*, registration: Mapping[str, Any], company_ref: str,
                       document_ref: str | None = None, title: str = "") -> dict[str, Any]:
    """Project an adapter-verified registration; callers retain the full record.

    The registry must have materialized/revalidated the registration before
    calling this pure projection. This projection grants no execution rights.
    """
    body = {k: v for k, v in registration.items() if k != "content_hash"}
    if content_hash(body) != registration.get("content_hash"):
        raise DocumentResearchStrategyError("document registration hash differs")
    normalized = registration.get("normalized_text", {})
    if normalized.get("status") != "complete" or normalized.get("truncated") is not False:
        raise DocumentResearchStrategyError("registration is not complete readable text")
    return {
        "company_ref": company_ref,
        "document_ref": document_ref or registration["document_ref"],
        "document_version_hash": registration["content_hash"],
        "authority_ref": registration["id"],
        "authority_hash": registration["content_hash"],
        "source_ref": registration["source_ref"],
        "source_content_hash": normalized["text_sha256"],
        "title": title,
        "readable": True,
        "completeness": "complete",
        "operations": ["search_registered_document", "read_registered_document"],
    }
