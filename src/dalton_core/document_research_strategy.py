"""Source-neutral planner choices for exact, already-readable document versions.

The planner selects a document and query, never a filesystem path, source
authority override, model route or budget. Source adapters independently
re-resolve the selected registration before execution.
"""
from __future__ import annotations

from collections.abc import Mapping
import re
from typing import Any

from .store import content_hash

STRATEGY_VERSION = "directed-document:0.1"
STRATEGY_FIELDS = frozenset({
    "strategy_version", "document_ref", "document_version_hash",
    "query_terms", "query_rationale",
})
_HASH = re.compile(r"[0-9a-f]{64}")


class DocumentResearchStrategyError(ValueError):
    pass


def normalize_strategy(value: Any, *, max_query_terms: int,
                       max_query_term_chars: int) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != STRATEGY_FIELDS:
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
    return {**dict(value), "document_ref": value["document_ref"].strip(),
            "query_rationale": value["query_rationale"].strip(), "query_terms": normalized}


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
    return strategy, dict(document)


def strategy_identity(value: Mapping[str, Any]) -> dict[str, Any]:
    """A new rationale alone must not purchase another identical query."""
    if not isinstance(value, Mapping) or set(value) != STRATEGY_FIELDS:
        raise DocumentResearchStrategyError("directed document strategy has an invalid closed shape")
    return {key: value[key] for key in sorted(STRATEGY_FIELDS - {"query_rationale"})}


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
