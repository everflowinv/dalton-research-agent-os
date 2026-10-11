"""Bind model driver choices to complete, admitted statement identities.

References select identities, never amounts. No subset matching, label matching,
or inferred dimension is permitted at this boundary.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .store import content_hash

IDENTITY_CATALOG_REF = "company-model-basis-identity-catalog:0.1"
CATALOG_DIMENSION = "catalog_ref"


def cell_identity_ref(cell: Mapping[str, Any]) -> str:
    identity = {
        "concept": cell.get("concept"), "statement": cell.get("statement"),
        "dimensions": cell.get("dimensions"),
        "statement_context_ref": cell.get("statement_context_ref"),
        "filing_content_hash": cell.get("filing_content_hash"),
        "line_content_hash": cell.get("line_content_hash"),
        "accession": cell.get("accession"),
    }
    return "model-basis-identity:" + content_hash(identity)


def resolve_identity(state: Mapping[str, Any], concept: Any,
                     identity: Mapping[str, Any]) -> dict[str, Any]:
    """Resolve an explicitly selected catalog entry from this exact state."""
    from .company_model_inputs import validate_statement_identity

    if not isinstance(identity, Mapping) or set(identity) != {"statement", "dimensions"}:
        raise ValueError("economic driver identity has invalid fields")
    dimensions = identity.get("dimensions")
    if isinstance(dimensions, Mapping) and set(dimensions) == {CATALOG_DIMENSION}:
        context = state.get("numeric_context") or {}
        if context.get("identity_catalog_contract") != IDENTITY_CATALOG_REF:
            raise ValueError("economic driver identity catalog is not admitted")
        selected = dimensions[CATALOG_DIMENSION]
        candidates = [cell for cell in context.get("cells") or []
                      if cell.get("concept") == concept
                      and cell.get("statement") == identity.get("statement")
                      and isinstance(cell.get("dimensions"), Mapping)
                      and cell.get("status") == "filed"
                      and cell_identity_ref(cell) == selected]
        identities = {content_hash({"statement": c["statement"],
                                    "dimensions": c["dimensions"]}): c
                      for c in candidates}
        if len(identities) != 1:
            raise ValueError("economic driver identity catalog ref is not in the admitted exact numeric context")
        cell = next(iter(identities.values()))
        identity = {"statement": cell["statement"], "dimensions": dict(cell["dimensions"])}
    checked = validate_statement_identity(identity)
    if not any(cell.get("concept") == concept
               and cell.get("statement") == checked["statement"]
               and cell.get("dimensions") == checked["dimensions"]
               for cell in (state.get("numeric_context") or {}).get("cells") or []):
        raise ValueError("economic driver identity is not in the admitted exact numeric context")
    return checked


def coherent_revenue_groups(groups: list[list[dict[str, Any]]]
                            ) -> list[list[dict[str, Any]]]:
    """Keep dimensional revenue and its consolidated income chain together.

    Different filings, contexts, dated windows, and units never mix.
    This protects disclosure membership; it does not assert a segment sum ties.
    Conflicting rows stay together as they did before this grouping.
    """
    from .model_forecast_driver import CONCEPT_ROLES, REVENUE

    def source_period(cell):
        return tuple(str(cell.get(k) or "") for k in (
            "filing_content_hash", "accession", "statement_context_ref",
            "period_start", "period_end", "unit",
        ))

    dimensional_periods = {source_period(group[0]) for group in groups
        if group[0].get("statement") == "income"
        and CONCEPT_ROLES.get(group[0].get("concept")) == REVENUE
        and (group[0].get("dimensions") or group[0].get("dimension_axis"))}
    protected: dict[tuple[str, ...], list[dict[str, Any]]] = {}
    other: list[list[dict[str, Any]]] = []
    for group in groups:
        first = group[0]
        key = source_period(first)
        revenue = first.get("statement") == "income" and CONCEPT_ROLES.get(first.get("concept")) == REVENUE
        income_chain = (key in dimensional_periods and first.get("statement") == "income"
                        and not (first.get("dimensions") or first.get("dimension_axis")))
        if not revenue and not income_chain:
            other.append(group)
            continue
        # Standalone consolidated revenue still has its original small group;
        # a dimensional disclosure shares an atomic package with every held
        # consolidated income component/subtotal in that exact source window.
        if key not in dimensional_periods:
            key += (str(first.get("concept") or ""),)
        protected.setdefault(key, []).extend(group)
    ordered = sorted(protected.values(), key=lambda group: (
        str(group[0].get("period_end") or ""),
        str(group[0].get("period_start") or ""),
        str(group[0].get("accession") or ""),
        str(group[0].get("concept") or ""),
    ), reverse=True)
    return [*ordered, *other]
