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

from .company_model_series import (
    ANNUAL_MAX_DAYS, NINE_MONTH_MAX_DAYS, QUARTER_MAX_DAYS, QUARTER_MIN_DAYS,
)
from .store import content_hash

STRATEGY_VERSION = "directed-document:0.1"
FINANCIAL_NOTE_TARGET_REF = "financial_note:diluted_eps_numerator:0.1"
FINANCIAL_NOTE_TARGET_SCHEMA_VERSION = "financial-note-target-0.1"
FINANCIAL_NOTE_HISTORY_TARGET_SCHEMA_VERSION = "financial-note-target-0.2"
FINANCIAL_NOTE_FORMS = frozenset({"10-Q", "10-K", "10-Q/A", "10-K/A"})
_STRATEGY_REQUIRED_FIELDS = frozenset({
    "strategy_version", "document_ref", "document_version_hash",
    "query_terms", "query_rationale",
})
STRATEGY_FIELDS = _STRATEGY_REQUIRED_FIELDS | {"evidence_target", "numeric_targets", "replaces_numeric_rejection", "resumes_numeric_result"}
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

    fields = (FINANCIAL_NOTE_TARGET_FIELDS | {"statement_context_ref", "statement_context_hash"}
              if isinstance(value, Mapping) and value.get("schema_version") == FINANCIAL_NOTE_HISTORY_TARGET_SCHEMA_VERSION
              else FINANCIAL_NOTE_TARGET_FIELDS)
    if not isinstance(value, Mapping) or set(value) != fields:
        raise DocumentResearchStrategyError("financial note target has an invalid closed shape")
    if (value["schema_version"] not in {FINANCIAL_NOTE_TARGET_SCHEMA_VERSION,
                                        FINANCIAL_NOTE_HISTORY_TARGET_SCHEMA_VERSION}
            or value["target_ref"] != FINANCIAL_NOTE_TARGET_REF
            or value["kind"] != "diluted_eps_numerator"
            or value["form"] not in (
                {"10-K"} if value["schema_version"] == FINANCIAL_NOTE_TARGET_SCHEMA_VERSION
                else FINANCIAL_NOTE_FORMS)
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
    if value["schema_version"] == FINANCIAL_NOTE_HISTORY_TARGET_SCHEMA_VERSION:
        ref, digest = value["statement_context_ref"], value["statement_context_hash"]
        if (ref is None) != (digest is None) or (ref is not None and (
                not isinstance(ref, str) or re.fullmatch(r"statement-context:[0-9a-f]{32}", ref) is None
                or not isinstance(digest, str) or _HASH.fullmatch(digest) is None)):
            raise DocumentResearchStrategyError("financial note statement context identity is invalid")
    periods = value["periods"]
    if not isinstance(periods, list) or not periods:
        raise DocumentResearchStrategyError("financial note target periods are empty")
    if (value["schema_version"] == FINANCIAL_NOTE_HISTORY_TARGET_SCHEMA_VERSION
            and len(periods) != 1):
        raise DocumentResearchStrategyError("history note needs one exact period")
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
        days = (date.fromisoformat(end) - date.fromisoformat(start)).days + 1
        if ((value["applicability_kind"] == "annual"
             and not NINE_MONTH_MAX_DAYS < days <= ANNUAL_MAX_DAYS)
                or (value["applicability_kind"] == "quarter"
                    and not QUARTER_MIN_DAYS <= days <= QUARTER_MAX_DAYS)):
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



def history_note_candidate_scope(target: Any) -> dict[str, str]:
    """Closed text-result scope for a single version0.2 statement period."""
    target = normalize_evidence_target(target)
    if (target["schema_version"] != FINANCIAL_NOTE_HISTORY_TARGET_SCHEMA_VERSION
            or len(target["periods"]) != 1):
        raise DocumentResearchStrategyError("history note needs one exact period")
    period = target["periods"][0]
    return {"metric_or_aspect": "diluted EPS numerator attribution",
            "period": period["period_start"] + "/" + period["period_end"],
            "basis": "filed note"}


def validate_history_note_candidate(target: Any, candidate: Any) -> None:
    """A note is neither generic prose nor a different period's authority."""
    expected = history_note_candidate_scope(target)
    if (not isinstance(candidate, Mapping)
            or any(candidate.get(key) != value for key, value in expected.items())):
        raise DocumentResearchStrategyError("history note candidate metric/period/basis differs from exact target")


def normalize_strategy(value: Any, *, max_query_terms: int,
                       max_query_term_chars: int) -> dict[str, Any]:
    if (not isinstance(value, Mapping)
            or not _STRATEGY_REQUIRED_FIELDS <= set(value)
            or set(value) - STRATEGY_FIELDS):
        raise DocumentResearchStrategyError("directed document strategy has an invalid closed shape")
    from .directed_numeric_research import CONTRACT_REF, normalize_targets
    from .directed_numeric_source_capabilities import NUMERIC_STRATEGIES, SPOKEN_CONTRACT_REF
    if value["strategy_version"] not in {STRATEGY_VERSION, *NUMERIC_STRATEGIES}:
        raise DocumentResearchStrategyError("unsupported directed document strategy")
    numeric = value["strategy_version"] in NUMERIC_STRATEGIES
    if numeric != ("numeric_targets" in value) or (numeric and "evidence_target" in value):
        raise DocumentResearchStrategyError("numeric strategy requires exact typed targets and no note target")
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
    if numeric:
        try:
            strategy["numeric_targets"] = normalize_targets(strategy["numeric_targets"])
            if strategy["strategy_version"] == SPOKEN_CONTRACT_REF and any(t["basis"] != "management-reported" for t in strategy["numeric_targets"]):
                raise DocumentResearchStrategyError("spoken numeric targets must retain management-reported basis")
        except ValueError as exc:
            raise DocumentResearchStrategyError(str(exc)) from exc
    upgrade = strategy.get("replaces_numeric_rejection")
    if upgrade is not None:
        if not numeric or not isinstance(upgrade,Mapping) or set(upgrade)!={"admission_ref","admission_hash","rejection_ref","rejection_hash"}:
            raise DocumentResearchStrategyError("numeric capability upgrade has invalid closed shape")
        if any(not isinstance(upgrade[k],str) or not upgrade[k] for k in ("admission_ref","rejection_ref")) or any(not isinstance(upgrade[k],str) or _HASH.fullmatch(upgrade[k]) is None for k in ("admission_hash","rejection_hash")):
            raise DocumentResearchStrategyError("numeric capability upgrade binding is invalid")
    resume = strategy.get("resumes_numeric_result")
    if resume is not None:
        if not numeric or not isinstance(resume,Mapping) or set(resume)!={"admission_ref","admission_hash","numeric_result_hash"}:
            raise DocumentResearchStrategyError("numeric source resume has invalid closed shape")
        if not isinstance(resume["admission_ref"],str) or not resume["admission_ref"] or any(not isinstance(resume[k],str) or _HASH.fullmatch(resume[k]) is None for k in ("admission_hash","numeric_result_hash")):
            raise DocumentResearchStrategyError("numeric source resume binding is invalid")
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
    if "numeric_targets" in strategy:
        from .directed_numeric_source_capabilities import SPOKEN_CONTRACT_REF
        from .document_figure_grade import FILED, SPOKEN
        grade = document.get("source_grade")
        expected = SPOKEN if strategy["strategy_version"] == SPOKEN_CONTRACT_REF else FILED
        if (strategy["strategy_version"] == SPOKEN_CONTRACT_REF or "source_grade" in document) and grade != expected:
            raise DocumentResearchStrategyError("selected original source grade does not support this numeric strategy")
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
