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


SOURCE_FAMILY_SELECTION_REF = "company-model-source-family-income-context:0.3"


def source_family_revenue_groups(groups: list[list[dict[str, Any]]]) -> list[list[dict[str, Any]]]:
    """Latest income chain, then latest reported disclosure families, then history.

    A lone hedge/AOCI row is not a reported revenue partition. Families require
    exact-source member and reported-total choices. Their arithmetic status is
    retained by the candidate contract; selection never proves reconciliation.
    Original conflict groups and every admitted cell's authority stay intact.
    """
    from .model_revenue_group_candidates import candidates
    cells = [cell for group in groups for cell in group]
    families = candidates(cells)["groups"]
    if not families:
        return coherent_revenue_groups(groups)

    def window(cell):
        return tuple(str(cell.get(k) or "") for k in (
            "filing_content_hash", "accession", "statement_context_ref",
            "period_start", "period_end", "unit"))

    # One atomic unit is still the existing complete same-source conflict group.
    owners = {}
    for index, group in enumerate(groups):
        for cell in group:
            owners.setdefault(cell_identity_ref(cell), set()).add(index)
    family_packets: dict[tuple[str, ...], set[int]] = {}
    for family in families:
        key = tuple(str(family[k] or "") for k in (
            "filing_hash", "accession", "context_ref", "period_start", "period_end", "unit"))
        refs = [family["reported_group_total_ref"], family["reported_company_total_ref"]]
        for member in family["members"]:
            refs.extend([member["identity_ref"], *(member.get("equivalent_refs") or [])])
        packet = family_packets.setdefault(key, set())
        for ref in refs:
            packet.update(owners.get(ref, set()))

    income = [(i, cell) for i, group in enumerate(groups) for cell in group
              if cell.get("statement") == "income" and not cell.get("dimensions")
              and not cell.get("dimension_axis")]
    latest = max((str(cell.get("period_end") or "") for _, cell in income), default="")
    current_packets: dict[tuple[str, ...], set[int]] = {}
    for index, cell in income:
        if cell.get("period_end") == latest:
            current_packets.setdefault(window(cell), set()).add(index)
    # When a current family exists it stays atomic with its income chain.
    for key, packet in current_packets.items():
        packet.update(family_packets.pop(key, set()))

    def ordered(packets):
        return sorted(packets, key=lambda key: (key[4], key[3], key[1], key[2], key[0], key[5]), reverse=True)

    selected = set()
    result = []
    for packets in (current_packets, family_packets):
        for key in ordered(packets):
            indices = packets[key] - selected
            if indices:
                result.append([cell for i in sorted(indices) for cell in groups[i]])
                selected.update(indices)
    remaining = [group for i, group in enumerate(groups) if i not in selected]
    result.extend(coherent_revenue_groups(remaining))
    return result
