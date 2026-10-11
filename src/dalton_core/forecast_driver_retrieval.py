"""Lexical retrieval recipes for governed forecast-driver material requests.

These recipes find original passages. They do not admit values, close a driver
execution gap or change any stored request, source grade, budget or deadline.
"""
from __future__ import annotations

import copy
from typing import Any, Mapping

RULE_REF = "rule:forecast-driver-material-retrieval:0.1"
PREFIX = "economic_driver_history:"
LEGACY_DATA_ITEM = "segment revenue classification comparable quarterly history quantity price bookings"
TERMS = {
    "segment": ("segment", "segment revenues", "revenue reconciliation", "reconciling items"),
    "volume": ("quantity", "units sold", "unit volume"),
    "price": ("average selling price", "price per unit", "average revenue per unit"),
    "contract_book": ("bookings", "order backlog", "contract backlog", "revenue backlog", "contract balance"),
    "mix": ("total revenues", "consolidated revenue", "revenue reconciliation"),
}


def data_item(kinds: list[str]) -> str:
    selected = [kind for kind in TERMS if kind in kinds]
    return PREFIX + ",".join(selected or list(TERMS))


def terms(item: Any) -> list[str] | None:
    if not isinstance(item, str) or not item.startswith(PREFIX):
        return None
    kinds = item[len(PREFIX):].split(",")
    if not kinds or any(kind not in TERMS for kind in kinds) or len(set(kinds)) != len(kinds):
        return None
    return list(dict.fromkeys(term for kind in kinds for term in TERMS[kind]))


def routing_view(request: Mapping[str, Any]) -> dict[str, Any]:
    """Adapt the finite old gap recipe in memory, preserving its ledger identity.

    Both the stored forecast-gap rule and its requester scope must match. A
    normal bookings request, caller label or unrelated data item is unchanged.
    The old omnibus request retains all its declared economic-driver meanings;
    new requests select their actual specification's driver kinds.
    """
    need = request.get("need") or {}
    requesters = list(request.get("requesters") or []) + [request.get("requester") or {}]
    qualified = (
        (request.get("acceptance") or {}).get("model_gap_rule_ref") == "rule:forecast-material-gaps:1"
        and need.get("kind") == "data"
        and any(isinstance(r, Mapping) and r.get("lane") == "company_model_forecast"
                and r.get("section") == "economic_driver_history" for r in requesters)
    )
    item = need.get("data_item")
    if not qualified or (item != LEGACY_DATA_ITEM and terms(item) is None):
        return dict(request)
    result = copy.deepcopy(dict(request))
    if item == LEGACY_DATA_ITEM:
        result["need"]["data_item"] = data_item(list(TERMS))
    kinds = list(result["need"].get("doc_kinds") or [])
    if "sec_10q" not in kinds:
        kinds.insert(0, "sec_10q")
    result["need"]["doc_kinds"] = kinds
    result["material_routing_rule_ref"] = RULE_REF
    return result


def request_terms(request: Mapping[str, Any]) -> list[str] | None:
    """Prioritize the declared driver labels without inferring their identity."""
    item = (request.get("need") or {}).get("data_item")
    lexical = terms(item)
    if lexical is None:
        return None
    selected = item[len(PREFIX):].split(",")
    identities = (request.get("acceptance") or {}).get("economic_driver_identities") or []
    labels = []
    for identity in identities:
        if not isinstance(identity, Mapping) or identity.get("kind") not in selected:
            continue
        label = identity.get("label")
        if isinstance(label, str) and 0 < len(label.strip()) <= 160:
            labels.append(label.strip())
    return list(dict.fromkeys(labels + lexical))
