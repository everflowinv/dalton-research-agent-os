"""Independent filed historical denominators for a declared income DAG base.

The reported subtotal never fills missing formula terms. Its source history is
carried as a separate, non-forecast driver, and every computable formula period
must agree exactly. The old formula-derived cash contract remains unchanged.
"""
from __future__ import annotations
from copy import deepcopy
from datetime import date, timedelta
from decimal import Decimal
from typing import Any, Mapping, Sequence
from .store import content_hash, canonical_json

RULE_REF = "cash-flow-filed-tie-historical-base:0.1"


def _source_history(source: Mapping[str, Any], unit: str) -> list[dict[str, Any]]:
    from .model_forecast_driver import ForecastModelUnavailable, _cells_of
    if (source.get("status") != "filed" or source.get("statement") != "income"
            or source.get("is_split") or source.get("statement_identity")
            or not isinstance(source.get("cells"), Mapping)):
        raise ForecastModelUnavailable("cash filed tie is not one consolidated income source")
    history = _cells_of(source, str(source["concept"]))
    facts = list(source.get("duration_facts") or [])
    if len(history) > 128 or len(facts) > 512:
        raise ForecastModelUnavailable("cash filed tie exceeds its bounded original history")
    if any(not Decimal(c["value"]).is_finite() for c in history) or any(
            not Decimal(f["value"]).is_finite() for f in facts):
        raise ForecastModelUnavailable("cash filed tie contains a non-finite original amount")
    for cell in history:
        raw = source["cells"][cell["period_end"]]
        if (not cell.get("period_start") or str(raw.get("unit") or "").casefold() != unit
                or not cell["accessions"] or len(set(cell["accessions"])) != len(cell["accessions"])):
            raise ForecastModelUnavailable("cash filed tie lacks exact duration/unit/source")
        matching = [f for f in facts if str(f.get("unit") or "").casefold() == unit
                    and f.get("period_start") and f.get("source_accessions")
                    and f.get("source_forms") and len(f["source_accessions"]) == len(f["source_forms"]) == 1]
        pairs = []
        if cell["basis"] == "reported":
            for fact in matching:
                if (fact["period_start"] == cell["period_start"] and fact["period_end"] == cell["period_end"]
                        and Decimal(fact["value"]) == Decimal(cell["value"])
                        and fact["source_accessions"] == cell["accessions"]):
                    pairs.append([fact])
        elif cell["basis"] == "derived_from_cumulative":
            prior_end = (date.fromisoformat(cell["period_start"]) - timedelta(days=1)).isoformat()
            for earlier in matching:
                for later in matching:
                    if (earlier["period_end"] == prior_end and later["period_end"] == cell["period_end"]
                            and earlier["period_start"] == later["period_start"]
                            and earlier["period_start"] < cell["period_start"]
                            and set(earlier["source_accessions"] + later["source_accessions"]) == set(cell["accessions"])
                            and Decimal(later["value"]) - Decimal(earlier["value"]) == Decimal(cell["value"])):
                        pairs.append([earlier, later])
        else:
            raise ForecastModelUnavailable("cash filed tie uses an unsupported actual basis")
        unique = {canonical_json(p): p for p in pairs}
        if len(unique) != 1:
            raise ForecastModelUnavailable("cash filed tie has missing or ambiguous original operands")
        operands = next(iter(unique.values()))
        if len(operands) == 2:
            cell["derived_from"] = [{"period_start": f["period_start"], "period_end": f["period_end"],
                "value": str(f["value"]), "unit": unit, "accession": str(f["source_accessions"][0]),
                "form": str(f["source_forms"][0])} for f in operands]
        cell["source_forms"] = sorted({str(f["source_forms"][0]) for f in operands})
    return history


def derive(structure: Mapping[str, Any], base_ref: str, cash_source: Mapping[str, Any],
           source: Mapping[str, Any], income_drivers: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    from .cash_flow_derived_base import derived_base_is_forecastable, derived_trailing_share
    from .model_forecast_driver import (ForecastModelUnavailable, _structure_historical_values,
                                      TRAILING_QUARTERS, _RATE_QUANT, _cell_ref)
    roots = [x for x in structure.get("lines") or [] if x.get("ref") == base_ref]
    formulas = [x for x in structure.get("formulas") or [] if x.get("output_ref") == base_ref]
    if (len(roots) != 1 or len(formulas) != 1 or not derived_base_is_forecastable(structure, base_ref)
            or formulas[0].get("operator") != "sum" or not formulas[0].get("tie_out_concept")
            or source.get("concept") != formulas[0]["tie_out_concept"]):
        raise ForecastModelUnavailable("cash filed tie does not bind the declared income sum")
    unit = str(roots[0].get("unit") or "").casefold()
    if str(cash_source.get("unit") or "").casefold() != unit:
        raise ForecastModelUnavailable("cash filed tie and cash source units differ")
    history = _source_history(source, unit)
    source_driver = {"ref": "cash-base-filed:" + base_ref, "kind": "statement_line",
        "label": str(source.get("label") or source["concept"]), "concept": source["concept"],
        "unit": unit, "statement": "income", "status": "filed", "role": None,
        "spec_rows": [base_ref], "note": "Independent reported historical tie; missing formula terms are not filled.",
        "history": history, "structure_line_ref": "cash-base-filed:" + base_ref,
        "forecast_method": "unavailable", "forecast_base_ref": None}
    # Ignore only this contract's independent history driver, never a declared
    # failed/unavailable branch of the actual income DAG.
    actual = [d for d in income_drivers if d.get("ref") != source_driver["ref"]]
    values, refs = _structure_historical_values(actual, structure)
    by_end = {c["period_end"]: c for c in history}
    comparisons = []
    for end, amount in sorted(values.get(base_ref, {}).items()):
        cell = by_end.get(end)
        if cell is None or Decimal(cell["value"]) != amount:
            raise ForecastModelUnavailable("cash filed tie disagrees with a computable income formula period")
        for ref in refs.get(base_ref, {}).get(end, []):
            held = [c for d in actual for c in d.get("history") or []
                    if c.get("concept") == ref.get("concept") and c.get("period_end") == end
                    and ref.get("accession") in (c.get("accessions") or [])]
            if not held or any(c.get("period_start") != cell["period_start"] for c in held):
                raise ForecastModelUnavailable("cash filed tie formula comparison durations differ")
        comparisons.append({"period_start": cell["period_start"], "period_end": end,
                            "formula_value": str(amount), "reported_value": cell["value"]})
    reported_values = {c["period_end"]: Decimal(c["value"]) for c in history}
    reported_refs = {c["period_end"]: [_cell_ref(c)] for c in history}
    cash_cells = [{**c, "concept": cash_source["concept"], "accessions": c.get("source_accessions") or []}
                  for c in cash_source.get("series", {}).get("quarters") or []]
    latest = sorted(cash_cells, key=lambda c:c["period_end"])[-TRAILING_QUARTERS:]
    from .cash_flow_ratio_applicability import refuse_if_inapplicable
    refuse_if_inapplicable(cash_cells, reported_values, reported_refs, [source_driver],
        role=cash_source['role'], base_ref=base_ref,
        input_hash=content_hash({'cash_source':cash_source,'source':source,'structure':structure}), unit=unit)
    denominators = [reported_values.get(c["period_end"]) for c in latest]
    if len(latest) == TRAILING_QUARTERS and all(v is not None for v in denominators):
        if any(v == 0 for v in denominators):
            raise ForecastModelUnavailable("cash filed tie has a zero denominator in the latest window")
        if len({v > 0 for v in denominators}) != 1:
            raise ForecastModelUnavailable("cash filed tie has mixed-sign denominators in the latest window")
    trailing = derived_trailing_share(cash_cells,
        reported_values, reported_refs, [source_driver], quarters=TRAILING_QUARTERS, rate_quant=_RATE_QUANT)
    if trailing is None:
        raise ForecastModelUnavailable("cash filed tie lacks the latest complete same-duration trailing window")
    trailing = {**trailing, "value": str(trailing["value"])}
    targets = sorted(cash_source.get("series", {}).get("quarters") or [], key=lambda c:c["period_end"])[-TRAILING_QUARTERS:]
    target_periods = sorted(set(by_end) | {str(c["period_end"]) for d in actual
        if d.get("statement") == "income" for c in d.get("history") or [] if c.get("period_start")})
    body = {"schema_version": "0.1", "rule_ref": RULE_REF, "source_basis": "independent_reported_tie",
        "base_ref": base_ref, "tie_out_concept": source["concept"], "source": deepcopy(dict(source)),
        "source_hash": content_hash(source), "driver": source_driver, "driver_hash": content_hash(source_driver),
        "comparisons": comparisons, "target_periods": target_periods,
        "cash_window_periods": [c["period_end"] for c in targets],
        "missing_formula_periods": [end for end in target_periods
            if end not in values.get(base_ref, {})], "trailing": trailing}
    return {**body, "content_hash": content_hash(body)}


def drivers(companion: Mapping[str, Any]) -> list[dict[str, Any]]:
    result = {}
    for line in companion.get("lines") or []:
        basis = line.get("historical_base")
        if basis is not None:
            held = basis["driver"]
            if held["ref"] in result and result[held["ref"]] != held:
                raise ValueError("cash filed tie drivers conflict")
            result[held["ref"]] = dict(held)
    return [result[k] for k in sorted(result)]


def validate(companion: Mapping[str, Any], structure: Mapping[str, Any],
             income_drivers: Sequence[Mapping[str, Any]]) -> None:
    from .model_forecast_driver import ForecastModelValidationError, ForecastModelUnavailable
    for line in companion.get("lines") or []:
        basis = line.get("historical_base")
        if basis is None:
            continue
        try:
            rebuilt = derive(structure, str(line["forecast_base_ref"]), line["source"],
                             basis["source"], income_drivers)
        except (ValueError, KeyError, TypeError, ArithmeticError, ForecastModelUnavailable) as exc:
            raise ForecastModelValidationError("cash filed historical base does not replay: " + str(exc)) from exc
        if rebuilt != basis:
            raise ForecastModelValidationError("cash filed historical base/source lineage does not replay")


def validate_assumptions(companion: Mapping[str, Any], assumptions: Sequence[Mapping[str, Any]]) -> None:
    """The automatic filed-tie claim must replay its exact rate and operands.

    Explicit events/manual decisions retain their existing independent gates;
    this checks only estimates claiming this automatic source rule.
    """
    from .model_forecast_driver import ForecastModelValidationError
    bases = {"concept:" + str(line["concept"]): line["historical_base"]
             for line in companion.get("lines") or [] if line.get("historical_base") is not None}
    for assumption in assumptions:
        if (assumption.get("provenance") or {}).get("rule_ref") != RULE_REF:
            continue
        basis = bases.get(str(assumption.get("driver_ref")))
        if (basis is None or assumption.get("kind") != "estimate"
                or assumption.get("measure") != "share_of_line" or assumption.get("unit") != "ratio"
                or Decimal(str(assumption.get("value"))) != Decimal(basis["trailing"]["value"])
                or assumption.get("refs") != basis["trailing"]["refs"]):
            raise ForecastModelValidationError("automatic cash filed-tie rate/source operands do not replay")
