"""Bound the Investment Memo's frozen material to what one model call can read.

``collect_frozen_input`` gathers every authority the memo may cite.  On the live
Core that is several megabytes for one company -- 2,900 claim versions, 1,500
research events, a 330 KB dossier and a 240 KB driver model for Accenture --
while a memo call admits at most ``MAX_INPUT_TOKENS`` bytes of prompt
(``cockpit_model`` compares the prompt's UTF-8 length with that bound and
refuses before sending).  Handing the whole collection to the model therefore
never drafts anything; it is refused, for free, every time.

This module turns the collection into a deterministic, bounded view:

- Every row keeps its exact ``ref`` and the ``hash`` of the full authority
  record it came from, so the memo's ``input_bindings`` still name the records
  and their hashes, and every number the memo cites is still a claim version
  or a forecast cell that is in the material (ADR-0011).
- The *text* of a row is a compact projection of the record: long strings are
  cut, long lists are shortened with an explicit ``_omitted`` count, and the
  record kinds whose bulk is history (price bars, fundamental windows, model
  cells) drop that bulk -- the cells travel as their own rows.
- Claims fill whatever budget the fixed rows leave: quantitative claims first,
  newest first, then qualitative claims, newest first.

Same input, same output: the projection depends only on the records and the
constants below.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

# Bytes of material JSON a producer prompt may carry.  The prompt adds the
# instructions and the closed output contract (a few KB), and the verifier
# prompt adds the whole draft to the rows the draft cites, so the material
# stays well inside the 120 KB call bound.
MATERIAL_BUDGET_BYTES = 72_000

# Per-kind caps on one row's compact text, in priority order.  Kinds not named
# here get ``DEFAULT_ROW_CAP``.
ROW_CAPS: Mapping[str, int] = {
    "deep_insight_gate_versions": 12_000,
    "industry_framework": 6_000,
    "company_dossier_versions": 8_000,
    "forecast_model_versions": 5_000,
    "sensitivity_projections": 5_000,
    "valuation_snapshot_versions": 3_500,
    "debate_map_versions": 3_500,
    "consensus_estimate_versions": 2_000,
    "catalyst_calendar_versions": 1_500,
    "market_price_series_versions": 800,
    "prior_model_versions": 1_500,
    "thesis_reflections": 1_500,
    "event_judgements": 1_200,
    "research_events": 400,
    "forecast_cell": 220,
    "valuation_metric": 360,
    "claim": 520,
}
DEFAULT_ROW_CAP = 1_500
# The share of the budget the fixed rows may take; the rest is for claims, the
# only refs a memo section's prose may cite.
FIXED_SHARE = 0.66
# Kinds the memo cannot be drafted without (``collect_frozen_input`` holds
# when one is missing); they are never dropped for space.
REQUIRED_KINDS = frozenset({
    "company_dossier_versions", "deep_insight_gate_versions", "forecast_model_versions",
    "sensitivity_projections", "valuation_snapshot_versions", "industry_framework"})
PRIORITY: tuple[str, ...] = (
    "deep_insight_gate_versions", "industry_framework", "company_dossier_versions",
    "forecast_model_versions", "sensitivity_projections", "valuation_snapshot_versions",
    "forecast_cell", "valuation_metric", "debate_map_versions", "consensus_estimate_versions",
    "catalyst_calendar_versions", "market_price_series_versions", "prior_model_versions",
    "thesis_reflections", "event_judgements", "research_events",
)
# Batch D: rows whose text is itself a citation source (a market block, a
# material-pack number, passage or our-model row).  Their text is carried
# verbatim -- never compacted or cut -- because the deliverable authority
# re-checks a cited figure against exactly this text, and a passage's ref is
# the hash of its exact words.  They come first among the fixed rows: they are
# what the Key Information and valuation figures cite.
PASSTHROUGH_KINDS: tuple[str, ...] = ("market_block", "pack_number", "pack_ours", "pack_passage")
PASSTHROUGH_CAP = 2_000
MAX_PASSTHROUGH_ROWS = 60
# How many rows of the repeated kinds are worth a place at all.
MAX_RESEARCH_EVENTS = 8
MAX_OTHER_DELIVERABLES = 2
# Forecast cells of these result lines only: the memo argues about revenue,
# profit and earnings, not about every expense line.
KEY_RESULT_ROLES = frozenset({"revenue", "gross_profit", "operating_income", "net_income",
                              "diluted_eps", "eps"})
# Fields dropped from a record before it is compacted: the bulk is history the
# memo cannot argue with line by line, or provenance it never needs to read.
DROPPED_FIELDS: Mapping[str, frozenset[str]] = {
    # ``unit_provenance`` alone is ~200 KB of per-slot provenance on the live
    # ACN dossier; left in, no shrink step fit the row cap and the row fell back
    # to its scalars -- ids and hashes, not one word of the dossier.
    "company_dossier_versions": frozenset({
        "unit_provenance", "input_fingerprints", "drafted_at", "bindings"}),
    "industry_framework": frozenset({"drafted_at"}),
    "deep_insight_gate_versions": frozenset({"unit_provenance", "input_fingerprints"}),
    "forecast_model_versions": frozenset({
        "results", "statements", "financial_statement_structure",
        "financial_statement_structure_replay", "cash_flow_companion",
        "forecast_structure_binding"}),
    "valuation_snapshot_versions": frozenset({
        "fundamental_windows", "price_history", "share_history", "input_coverage"}),
    "market_price_series_versions": frozenset({"bars", "observations", "history"}),
}
# Nested citation bookkeeping dropped from prose documents: each sentence of a
# dossier carries up to five 78-character claim refs, which is most of its
# bytes, and none of them is citable from a memo section that has not got the
# Claim row itself.  The prose is what the drafter reads.
NESTED_DROPPED: Mapping[str, frozenset[str]] = {
    "company_dossier_versions": frozenset({"refs", "sources", "structure"}),
    "industry_framework": frozenset({"refs", "sources", "structure", "link_index"}),
}
CLAIM_FIELDS = ("id", "normalized_statement", "metric_or_aspect", "period", "value",
                "unit", "scale", "currency", "claim_kind", "basis", "context")
_SHRINK_STEPS = ((600, 16), (300, 10), (160, 6), (80, 3), (40, 2))


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def shrink(value: Any, *, max_text: int, max_items: int, depth: int = 0) -> Any:
    """Cut long strings and long lists, saying how much was left out."""

    if isinstance(value, str):
        return value if len(value) <= max_text else value[:max_text] + "…"
    if isinstance(value, Mapping):
        if depth > 6:
            return {"_omitted": "nested"}
        return {str(key): shrink(item, max_text=max_text, max_items=max_items,
                                 depth=depth + 1)
                for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))}
    if isinstance(value, (list, tuple)):
        if depth > 6:
            return [{"_omitted": len(value)}] if value else []
        kept = [shrink(item, max_text=max_text, max_items=max_items, depth=depth + 1)
                for item in list(value)[:max_items]]
        if len(value) > max_items:
            kept.append({"_omitted": len(value) - max_items})
        return kept
    return value


def compact_text(record: Mapping[str, Any], cap: int) -> str:
    """The largest shrink step of ``record`` whose JSON fits ``cap`` bytes."""

    for max_text, max_items in _SHRINK_STEPS:
        text = _json(shrink(record, max_text=max_text, max_items=max_items))
        if len(text.encode("utf-8")) <= cap:
            return text
    scalars = {key: (value[:120] + "…" if isinstance(value, str) and len(value) > 120
                     else value)
               for key, value in sorted(record.items())
               if isinstance(value, (str, int, float, bool)) or value is None}
    text = _json(scalars)
    while len(text.encode("utf-8")) > cap and scalars:
        scalars.pop(sorted(scalars)[-1])
        text = _json(scalars)
    # Then the substance, field by field, at the largest shrink step that
    # still fits: a row that is only ids and hashes gives a drafter nothing to
    # argue from, and one oversized field must not cost every other field.
    out = dict(scalars)
    nested = sorted((key for key, value in record.items() if key not in scalars
                     and not (isinstance(value, (str, int, float, bool)) or value is None)),
                    key=lambda key: (len(_json(shrink(record[key], max_text=40, max_items=2))), key))
    for key in nested:
        for max_text, max_items in _SHRINK_STEPS:
            trial = _json({**out, key: shrink(record[key], max_text=max_text,
                                               max_items=max_items, depth=1)})
            if len(trial.encode("utf-8")) <= cap:
                out[key] = shrink(record[key], max_text=max_text, max_items=max_items, depth=1)
                text = trial
                break
    return text


def drop_nested(value: Any, keys: frozenset[str]) -> Any:
    if isinstance(value, Mapping):
        return {key: drop_nested(item, keys) for key, item in value.items() if key not in keys}
    if isinstance(value, list):
        return [drop_nested(item, keys) for item in value]
    return value


def _record(row: Mapping[str, Any]) -> dict[str, Any]:
    try:
        value = json.loads(str(row.get("text") or "{}"))
    except (TypeError, ValueError):
        return {"text": str(row.get("text") or "")}
    return value if isinstance(value, dict) else {"value": value}


def _framework_for(record: dict[str, Any], company_ref: str) -> dict[str, Any]:
    comparison = dict(record.get("cross_company_comparison") or {})
    if comparison.get("cells"):
        comparison["cells"] = [
            {key: cell.get(key) for key in ("ref", "company_ref", "metric", "quarter",
                                            "value", "status")}
            for cell in comparison["cells"]
            if isinstance(cell, Mapping) and cell.get("company_ref") == company_ref]
    out = {key: value for key, value in record.items()
           if key not in {"cross_company_comparison", "bindings"}}
    out["cross_company_comparison"] = comparison
    return out


def _claim_rank(row: Mapping[str, Any]) -> tuple[int, str, str]:
    record = _record(row)
    quantitative = record.get("claim_kind") == "quantitative" or record.get("value") not in (
        None, "", "None")
    return (0 if quantitative else 1, "~" if not record.get("created_at")
            else _invert(str(record.get("created_at"))), str(row.get("ref")))


def _invert(text: str) -> str:
    # Newest first under an ascending sort, without a clock.
    return "".join(chr(0x10FFFF - ord(char)) if ord(char) < 0x10FFFF else char
                   for char in text)


def _cell_is_key(record: Mapping[str, Any]) -> bool:
    ref = str(record.get("ref") or "")
    head = ref.split("@", 1)[0]
    return any(head == f"result:{role}" for role in KEY_RESULT_ROLES)


def bound_material(rows: Sequence[Mapping[str, Any]], *, company_ref: str,
                   budget_bytes: int = MATERIAL_BUDGET_BYTES) -> list[dict[str, Any]]:
    """The bounded, deterministic view of the frozen material."""

    by_kind: dict[str, list[Mapping[str, Any]]] = {}
    for row in rows:
        by_kind.setdefault(str(row.get("kind")), []).append(row)
    fixed: list[dict[str, Any]] = []

    fixed_budget = int(budget_bytes * FIXED_SHARE)
    spent = [0]

    def take(row: Mapping[str, Any], record: Mapping[str, Any], kind: str) -> None:
        cap = ROW_CAPS.get(kind, DEFAULT_ROW_CAP)
        candidate = {"ref": str(row["ref"]), "hash": str(row["hash"]), "kind": kind,
                     "text": compact_text(record, cap)}
        if row.get("version_ref"):
            # A computed cell's citation names the version it lives in.
            candidate["version_ref"] = str(row["version_ref"])
        size = len(_json(candidate).encode("utf-8"))
        if kind not in REQUIRED_KINDS and spent[0] + size > fixed_budget:
            return
        spent[0] += size
        fixed.append(candidate)

    for kind in PASSTHROUGH_KINDS:
        for row in list(by_kind.get(kind, ()))[:MAX_PASSTHROUGH_ROWS]:
            text = str(row.get("text") or "")
            if len(text) > PASSTHROUGH_CAP:
                continue
            candidate = {"ref": str(row["ref"]), "hash": str(row["hash"]), "kind": kind, "text": text}
            for key in ("source_kind", "period"):
                if row.get(key):
                    candidate[key] = str(row[key])
            size = len(_json(candidate).encode("utf-8"))
            if spent[0] + size > fixed_budget:
                continue
            spent[0] += size
            fixed.append(candidate)
    ordered_kinds = [kind for kind in PRIORITY if kind in by_kind] + sorted(
        kind for kind in by_kind if kind not in PRIORITY and kind != "claim"
        and kind not in PASSTHROUGH_KINDS)
    for kind in ordered_kinds:
        items = by_kind[kind]
        if kind == "research_events":
            items = sorted(items, key=lambda row: _invert(
                str(_record(row).get("occurred_at") or _record(row).get("created_at") or "")))[
                    :MAX_RESEARCH_EVENTS]
        elif kind == "forecast_cell":
            items = [row for row in items if _cell_is_key(_record(row))]
        elif kind not in PRIORITY:
            items = sorted(items, key=lambda row: _invert(
                str(_record(row).get("created_at") or "")))[:MAX_OTHER_DELIVERABLES]
        for row in items:
            record = _record(row)
            if kind == "industry_framework":
                record = _framework_for(record, company_ref)
            dropped = DROPPED_FIELDS.get(kind, frozenset())
            record = {key: value for key, value in record.items() if key not in dropped}
            if kind in NESTED_DROPPED:
                record = drop_nested(record, NESTED_DROPPED[kind])
            if kind == "forecast_cell":
                period = record.get("period")
                record = {key: record.get(key) for key in ("ref", "value", "kind", "status")}
                # The period's end date names it; the calendar object is bulk.
                record["period"] = (period.get("end") if isinstance(period, Mapping)
                                    else period)
            elif kind == "valuation_metric":
                record = {key: record.get(key) for key in (
                    "metric", "label", "value", "unit", "formula", "percentile", "as_of")
                    if record.get(key) is not None}
            take(row, record, kind)
    used = sum(len(_json(row).encode("utf-8")) for row in fixed)
    claims: list[dict[str, Any]] = []
    for row in sorted(by_kind.get("claim", ()), key=_claim_rank):
        record = _record(row)
        compact = {key: record.get(key) for key in CLAIM_FIELDS
                   if record.get(key) not in (None, "", "None")}
        candidate = {"ref": str(row["ref"]), "hash": str(row["hash"]), "kind": "claim",
                     "text": compact_text(compact, ROW_CAPS["claim"])}
        size = len(_json(candidate).encode("utf-8"))
        if used + size > budget_bytes:
            continue
        used += size
        claims.append(candidate)
    return claims + fixed


def material_bytes(rows: Sequence[Mapping[str, Any]]) -> int:
    return len(_json(list(rows)).encode("utf-8"))


__all__ = ["MATERIAL_BUDGET_BYTES", "bound_material", "compact_text", "material_bytes",
           "shrink"]
