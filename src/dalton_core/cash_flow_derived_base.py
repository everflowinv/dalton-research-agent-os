"""Source-bound monetary income DAG bases for the cash-flow companion.

This never turns a derived amount into a filed fact. Materialization still
proves every original filing tie; this module checks the executable dependency
shape and retains the actual operands when deriving a historical share.
"""
from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP
from datetime import date, timedelta
from typing import Any, Mapping, Sequence

CONTRACT_REF = "cash-flow-derived-income-base:0.1"
_LEAF_METHODS = frozenset({
    "quarterly_growth", "year_over_year_growth", "seasonal_factor",
    "share_of_line", "carry_forward_mean", "zero_one_off",
})


def derived_base_is_forecastable(structure: Mapping[str, Any], base_ref: str) -> bool:
    """Only a complete, acyclic, same-currency duration income sum graph.

    Balance rolls and interest on rolled balances are deliberately outside this
    first contract: cash is computed after income, so it cannot be its base.
    The existing filed-base contract is not changed by this predicate.
    """
    raw = structure.get("lines") or []
    lines = {str(x.get("ref")): x for x in raw if isinstance(x, Mapping)}
    if len(lines) != len(raw):
        return False
    root = lines.get(base_ref) or {}
    unit = str(root.get("unit") or "").casefold()
    if root.get("kind") != "derived" or len(unit) != 3 or not unit.isalpha():
        return False
    formulas: dict[str, Mapping[str, Any]] = {}
    for formula in structure.get("formulas") or []:
        if not isinstance(formula, Mapping):
            return False
        ref = str(formula.get("output_ref"))
        if ref in formulas:
            return False
        formulas[ref] = formula
    active: set[str] = set()
    proved: set[tuple[str, bool]] = set()

    def visit(ref: str, balance_basis: bool = False) -> bool:
        if ref in active:
            return False
        if (ref, balance_basis) in proved:
            return True
        line = lines.get(ref)
        if line is None or str(line.get("unit") or "").casefold() != unit:
            return False
        if balance_basis:
            # An independent filed balance held constant is not a cash roll.
            return (line.get("kind") == "filed" and bool(line.get("concept"))
                    and line.get("statement") == "balance"
                    and line.get("period_kind") == "instant"
                    and line.get("forecast_method") == "hold_last")
        if line.get("statement") != "income" or line.get("period_kind") != "duration":
            return False
        active.add(ref)
        if line.get("kind") == "derived":
            formula = formulas.get(ref) or {}
            terms = formula.get("terms")
            valid = (line.get("forecast_method") == "formula"
                     and formula.get("operator") == "sum"
                     and bool(formula.get("tie_out_concept"))
                     and isinstance(terms, list) and bool(terms)
                     and all(isinstance(x, Mapping) and visit(str(x.get("line_ref")))
                             for x in terms))
        else:
            method = line.get("forecast_method")
            valid = (line.get("kind") == "filed" and bool(line.get("concept"))
                     and method in _LEAF_METHODS | {"rate_times_balance"})
            if valid and method == "share_of_line":
                valid = visit(str(line.get("forecast_base_ref")))
            elif valid and method == "rate_times_balance":
                valid = visit(str(line.get("forecast_base_ref")), balance_basis=True)
        active.remove(ref)
        if valid:
            proved.add((ref, balance_basis))
        return bool(valid)

    return visit(base_ref)


def derived_trailing_share(
    cash_cells: Sequence[Mapping[str, Any]],
    values: Mapping[str, Decimal], refs: Mapping[str, Sequence[Mapping[str, Any]]],
    income_drivers: Sequence[Mapping[str, Any]], *,
    quarters: int, rate_quant: Decimal,
) -> dict[str, Any] | None:
    """Replay all operands of the latest cash window, without skipping a hole.

    Each derived denominator is backed by its original income operand refs.
    Neither a synthetic concept nor a filed-cell ref for the subtotal is made.
    A missing period/source, unequal duration, zero or mixed-sign base leaves
    the share unavailable; it never invents a base or fills a missing value.
    """
    used = sorted(cash_cells, key=lambda x: str(x.get("period_end")))[-quarters:]
    if len(used) != quarters:
        return None
    try:
        for previous, current in zip(used, used[1:]):
            if date.fromisoformat(str(current["period_start"])) != (
                    date.fromisoformat(str(previous["period_end"])) + timedelta(days=1)):
                return None
    except (KeyError, TypeError, ValueError):
        return None
    source_cells: dict[tuple[str, str, str], list[Mapping[str, Any]]] = {}
    for driver in income_drivers:
        for cell in driver.get("history") or []:
            for accession in cell.get("accessions") or []:
                key = (str(cell.get("concept")), str(cell.get("period_end")), str(accession))
                source_cells.setdefault(key, []).append(cell)
    samples: list[Decimal] = []
    signs: set[bool] = set()
    all_refs: list[dict[str, Any]] = []
    for cash in used:
        end = str(cash.get("period_end"))
        base = values.get(end)
        operands = refs.get(end) or []
        if base is None or base == 0 or not cash.get("period_start") or not operands:
            return None
        for ref in operands:
            if ref.get("kind") != "input_cell" or not ref.get("accession"):
                return None
            held = source_cells.get((str(ref.get("concept")), end, str(ref["accession"])))
            if not held or any(x.get("period_start") != cash["period_start"] for x in held):
                return None
        signs.add(base > 0)
        samples.append((Decimal(str(cash["value"])) / base).quantize(rate_quant, ROUND_HALF_UP))
        accessions = cash.get("accessions") or []
        if not accessions:
            return None
        all_refs.append({"kind": "input_cell", "ref": None,
                         "concept": str(cash["concept"]), "period_end": end,
                         "accession": str(accessions[0])})
        all_refs.extend(dict(x) for x in operands)
    if len(signs) != 1:
        return None
    unique: list[dict[str, Any]] = []
    for ref in all_refs:
        if ref not in unique:
            unique.append(ref)
    return {"value": (sum(samples, Decimal(0)) / Decimal(len(samples))).quantize(
                rate_quant, ROUND_HALF_UP),
            "count": len(samples), "first_period": str(used[0]["period_end"]),
            "last_period": str(used[-1]["period_end"]), "refs": unique}
