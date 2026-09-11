"""Pure annual projection for one immutable company forecast model.

The quarterly model is the executable company statement DAG.  A workbook is
only one view of it, so fiscal-year diluted shares and EPS cannot first come
into existence as spreadsheet formulas.  This module derives a closed,
self-hashed projection from the exact model, model inputs, statement
structure/replay, and fiscal-calendar authority.  Reports and workbook
exports consume the same projection.
"""

from __future__ import annotations

import json
import re
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping, Sequence

from .company_financial_statement_structure import (
    aggregate_fiscal_year,
    annual_diluted_eps,
    day_weighted_annual_shares,
)
from .model_forecast_driver import (
    STRUCTURED_SCHEMA_VERSION,
    build_structure_drivers,
    structure_historical_values,
    validate_forecast_model,
)
from .store import canonical_json, content_hash


SCHEMA_VERSION = "company-model-annual-projection-0.1"
_HEX64 = re.compile(r"[0-9a-f]{64}")


class AnnualProjectionError(ValueError):
    """The supplied authorities cannot produce this annual projection."""


def calendar_binding_from_annual_filing(
    filing: Mapping[str, Any], *, company_ref: str,
) -> dict[str, Any]:
    """Freeze the fiscal calendar declared by one exact annual filing row."""

    if (filing.get("company_ref") != company_ref or filing.get("form") != "10-K"
            or not isinstance(filing.get("ingest_id"), str)
            or not isinstance(filing.get("content_hash"), str)):
        raise AnnualProjectionError("annual filing does not bind this company calendar")
    try:
        report_date = date.fromisoformat(str(filing.get("report_date"))).isoformat()
    except ValueError as exc:
        raise AnnualProjectionError("annual filing report date is invalid") from exc
    base = {
        "calendar_ref": filing["ingest_id"],
        "source_hash": filing["content_hash"],
        "as_of": report_date,
        "fiscal_year_end_month": int(report_date[5:7]),
    }
    return {**base, "content_hash": content_hash(base)}


def validate_calendar_binding(value: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if value is None:
        return None
    binding = dict(value)
    allowed = {
        "calendar_ref", "source_hash", "as_of",
        "fiscal_year_end_month", "content_hash",
    }
    if set(binding) != allowed:
        raise AnnualProjectionError("fiscal calendar binding has an invalid closed shape")
    asserted = binding.pop("content_hash")
    if not isinstance(asserted, str) or asserted != content_hash(binding):
        raise AnnualProjectionError("fiscal calendar binding content hash is invalid")
    month = binding.get("fiscal_year_end_month")
    if isinstance(month, bool) or not isinstance(month, int) or not 1 <= month <= 12:
        raise AnnualProjectionError(
            "fiscal_year_end_month must be an integer from 1 to 12")
    for field in ("calendar_ref", "source_hash", "as_of"):
        if not isinstance(binding.get(field), str) or not binding[field]:
            raise AnnualProjectionError(f"fiscal calendar {field} is invalid")
    if _HEX64.fullmatch(binding["source_hash"]) is None:
        raise AnnualProjectionError("fiscal calendar source_hash is invalid")
    return {**binding, "content_hash": asserted}


def fiscal_groups(
    periods: Sequence[str], binding: Mapping[str, Any] | None,
    historical: set[str],
) -> list[tuple[str, list[str]]]:
    """Group exact quarter ends under the bound fiscal year-end month."""

    if binding is None:
        return []
    groups: list[tuple[str, list[str]]] = []
    pending: list[str] = []
    for raw in periods:
        try:
            period = date.fromisoformat(str(raw)).isoformat()
        except ValueError as exc:
            raise AnnualProjectionError("annual projection period is not an ISO date") from exc
        pending.append(period)
        if int(period[5:7]) == binding["fiscal_year_end_month"]:
            kinds = {"A" if item in historical else "E" for item in pending}
            suffix = next(iter(kinds)) if len(kinds) == 1 else "A/E"
            groups.append((f"FY{period[:4]}{suffix}", pending))
            pending = []
    if pending:
        kinds = {"A" if item in historical else "E" for item in pending}
        suffix = next(iter(kinds)) if len(kinds) == 1 else "A/E"
        groups.append((f"FY{pending[-1][:4]}{suffix} (partial)", pending))
    return groups


def _line_definition_ref(
    structure: Mapping[str, Any], line: Mapping[str, Any], concept: Any,
) -> str:
    definition = {
        "structure_ref": structure.get("structure_ref"),
        "structure_hash": structure.get("content_hash"),
        "line_ref": line.get("ref"), "role": line.get("role"),
        "concept": concept, "unit": line.get("unit"),
        "annual_semantics": line.get("annual_semantics"),
    }
    return f"statement-line-definition:{content_hash(definition)}"


def _structure_facts(
    inputs: Mapping[str, Any], structure: Mapping[str, Any],
    line: Mapping[str, Any], group: Sequence[str], label: str,
    calendar_binding: Mapping[str, Any],
    formula: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Bind raw typed durations to one fiscal group and one structure line."""

    concept = line.get("concept") if line.get("kind") == "filed" else None
    if concept is None and formula is not None:
        concept = formula.get("tie_out_concept")
    source = next(
        (item for item in (inputs.get("filed_lines") or [])
         if item.get("concept") == concept), None,
    )
    if not isinstance(source, Mapping) or len(group) != 4:
        return []
    definition_ref = _line_definition_ref(structure, line, concept)
    fiscal_year = f"{calendar_binding['calendar_ref']}:{label}"
    calendar = calendar_binding["content_hash"]
    selected: list[dict[str, Any]] = []
    for raw in source.get("duration_facts") or []:
        if not isinstance(raw, Mapping):
            continue
        try:
            start = date.fromisoformat(str(raw.get("period_start")))
            end = date.fromisoformat(str(raw.get("period_end")))
        except ValueError:
            continue
        unit = str(raw.get("unit") or "").casefold()
        if unit != str(line.get("unit") or "").casefold():
            continue
        period_kind = raw.get("period_kind")
        if period_kind == "quarter" and str(raw.get("period_end")) in group:
            normalized_kind = "quarter"
        elif str(raw.get("period_end")) == group[-1] and 290 < (end - start).days <= 380:
            normalized_kind = "annual"
        else:
            continue
        selected.append({
            "fiscal_year": fiscal_year, "period_kind": normalized_kind,
            "period_start": start.isoformat(), "period_end": end.isoformat(),
            "value": str(raw.get("value")), "unit": unit,
            "calendar": calendar, "definition_ref": definition_ref,
            "source_accessions": list(raw.get("source_accessions") or []),
            "source_forms": list(raw.get("source_forms") or []),
        })
    return selected


def _historical_eps(
    inputs: Mapping[str, Any], structure: Mapping[str, Any],
    label: str, group: Sequence[str], calendar: Mapping[str, Any],
) -> dict[str, Any]:
    lines = {str(item["ref"]): item for item in (structure.get("lines") or [])}
    formulas = {str(item["output_ref"]): item
                for item in (structure.get("formulas") or [])}
    by_role = {str(item["role"]): item for item in lines.values()}
    numerator = by_role.get("diluted_eps_numerator")
    shares = by_role.get("diluted_weighted_average_shares")
    eps = by_role.get("diluted_eps")
    if not all(isinstance(item, Mapping) for item in (numerator, shares, eps)):
        return {"status": "unavailable", "value": None,
                "reason": "the structure does not define diluted EPS authority"}
    fiscal_year = f"{calendar['calendar_ref']}:{label}"
    numerator_cells = _structure_facts(
        inputs, structure, numerator, group, label, calendar,
        formulas.get(str(numerator["ref"])),
    )
    share_cells = _structure_facts(
        inputs, structure, shares, group, label, calendar,
        formulas.get(str(shares["ref"])),
    )
    eps_cells = _structure_facts(
        inputs, structure, eps, group, label, calendar,
        formulas.get(str(eps["ref"])),
    )
    outcome = annual_diluted_eps(
        diluted_eps_numerator_cells=numerator_cells,
        diluted_weighted_share_cells=share_cells,
        diluted_eps_cells=eps_cells, fiscal_year=fiscal_year,
    )
    return {
        **outcome,
        "direct_numerator": aggregate_fiscal_year(
            numerator_cells, semantic="direct_annual", fiscal_year=fiscal_year),
        "direct_shares": aggregate_fiscal_year(
            share_cells, semantic="direct_annual", fiscal_year=fiscal_year),
    }


def _projected_quarter_cells(
    model: Mapping[str, Any], line: Mapping[str, Any],
    result: Mapping[str, Any], group: Sequence[str], *,
    calendar: Mapping[str, Any], definition_ref: str,
    historical_values: Mapping[str, Mapping[str, Decimal]],
    historical_refs: Mapping[str, Mapping[str, Sequence[Mapping[str, Any]]]],
    historical_starts: Mapping[str, str],
) -> list[dict[str, Any]]:
    """Combine exact filed quarters and immutable forecast cells."""

    line_ref = str(line["ref"])
    by_end: dict[str, dict[str, Any]] = {}
    for end, value in (historical_values.get(line_ref) or {}).items():
        start = historical_starts.get(str(end))
        if start is None:
            continue
        by_end[str(end)] = {
            "period_start": start, "period_end": str(end), "value": str(value),
            "unit": str(line.get("unit") or "").casefold(),
            "input_cell_refs": [dict(item) for item in
                                (historical_refs.get(line_ref, {}).get(str(end)) or [])],
        }
    live: dict[str, Mapping[str, Any]] = {}
    for cell in result.get("cells") or []:
        if cell.get("superseded_by"):
            continue
        period = cell.get("period")
        if (cell.get("status") == "computed" and isinstance(period, Mapping)
                and period.get("kind") == "quarter"):
            live[str(period.get("end"))] = cell
    for end, cell in live.items():
        by_end[end] = {
            "period_start": cell["period"].get("start"),
            "period_end": end, "value": cell.get("value"),
            "unit": str(line.get("unit") or "").casefold(),
            "model_cell_ref": cell.get("ref"),
            **({"input_cell_refs": [dict(item) for item in
                                     (cell.get("input_cell_refs") or [])]}
               if cell.get("kind") == "actual" else {}),
        }
    return [{
        **by_end[end], "period_kind": "quarter",
        "calendar": calendar["content_hash"],
        "definition_ref": definition_ref,
    } for end in group if end in by_end]


def _forecast_outcomes(
    model: Mapping[str, Any], structure: Mapping[str, Any],
    label: str, group: Sequence[str], calendar: Mapping[str, Any],
    *, historical_values: Mapping[str, Mapping[str, Decimal]],
    historical_refs: Mapping[str, Mapping[str, Sequence[Mapping[str, Any]]]],
    historical_starts: Mapping[str, str],
) -> tuple[dict[str, Any], dict[str, Any]]:
    lines = {str(item["ref"]): item for item in (structure.get("lines") or [])}
    by_role = {str(item["role"]): item for item in lines.values()}
    results = {str(item.get("role")): item for item in (model.get("results") or [])}
    shares_line = by_role.get("diluted_weighted_average_shares")
    numerator_line = by_role.get("diluted_eps_numerator")
    shares_result = results.get("diluted_weighted_average_shares")
    numerator_result = results.get("diluted_eps_numerator")
    unavailable = {"status": "unavailable", "value": None,
                   "reason": "the structure does not define annual diluted EPS inputs"}
    if not all(isinstance(item, Mapping) for item in (
            shares_line, numerator_line, shares_result, numerator_result)):
        return unavailable, unavailable
    replay = model.get("financial_statement_structure_replay") or {}
    share_replay = next(
        (item for item in (replay.get("forecast_methods") or [])
         if item.get("line_ref") == shares_line.get("ref")), None,
    )
    if (shares_line.get("annual_forecast_method") != "day_weighted_quarters"
            or not isinstance(share_replay, Mapping)
            or share_replay.get("annual_status") != "validated"):
        reason = {"status": "unavailable", "value": None,
                  "reason": "annual diluted shares lack a validated forecast method"}
        return reason, reason
    if len(group) != 4:
        reason = {"status": "unavailable", "value": None,
                  "reason": "an annual forecast needs four fiscal quarters"}
        return reason, reason
    fiscal_year = f"{calendar['calendar_ref']}:{label}"
    share_definition = _line_definition_ref(
        structure, shares_line, shares_line.get("concept"))
    share_cells = _projected_quarter_cells(
        model, shares_line, shares_result, group, calendar=calendar,
        definition_ref=share_definition, historical_values=historical_values,
        historical_refs=historical_refs, historical_starts=historical_starts,
    )
    for cell in share_cells:
        cell["fiscal_year"] = fiscal_year
    shares = day_weighted_annual_shares(
        share_cells, required_calendar=calendar["content_hash"],
        required_definition_ref=share_definition,
    )
    if numerator_line.get("annual_semantics") != "sum_quarters":
        numerator = {"status": "unavailable", "value": None,
                     "reason": "annual diluted-EPS numerator is not a four-quarter flow"}
    else:
        numerator_definition = _line_definition_ref(
            structure, numerator_line, numerator_line.get("concept"))
        numerator_cells = _projected_quarter_cells(
            model, numerator_line, numerator_result, group, calendar=calendar,
            definition_ref=numerator_definition, historical_values=historical_values,
            historical_refs=historical_refs, historical_starts=historical_starts,
        )
        for cell in numerator_cells:
            cell["fiscal_year"] = fiscal_year
        numerator = aggregate_fiscal_year(
            numerator_cells, semantic="sum_quarters", fiscal_year=fiscal_year)
    if numerator.get("status") != "computed" or shares.get("status") != "computed":
        reason = (numerator.get("reason") if numerator.get("status") != "computed"
                  else shares.get("reason"))
        return shares, {"status": "unavailable", "value": None, "reason": reason}
    try:
        denominator = Decimal(str(shares["value"]))
        income = Decimal(str(numerator["value"]))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise AnnualProjectionError("annual forecast values are invalid decimals") from exc
    if denominator <= 0:
        return shares, {"status": "unavailable", "value": None,
                        "reason": "annual diluted weighted shares are not positive"}
    income_unit = str(numerator.get("unit") or "").casefold()
    if len(income_unit) != 3 or not income_unit.isalpha():
        return shares, {"status": "unavailable", "value": None,
                        "reason": "annual diluted-EPS numerator is not a currency amount"}
    return shares, {
        "status": "computed", "value": str(income / denominator),
        "unit": f"{income_unit}_per_share",
        "numerator": numerator, "denominator": shares,
    }


def build_annual_projection(
    *, model: Mapping[str, Any], inputs: Mapping[str, Any],
    calendar_binding: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Derive the immutable annual diluted-EPS projection for one model."""

    model_wire = dict(model)
    model_wire.pop("status", None)
    try:
        held = validate_forecast_model(model_wire)
    except Exception as exc:
        raise AnnualProjectionError("forecast model authority is invalid") from exc
    if held.get("schema_version") != STRUCTURED_SCHEMA_VERSION:
        raise AnnualProjectionError("annual projection requires a structured forecast model")
    exact_inputs_hash = content_hash(json.loads(canonical_json(inputs)))
    if (held.get("inputs_hash") != exact_inputs_hash
            or inputs.get("company_ref") != held.get("company_ref")):
        raise AnnualProjectionError("forecast model does not bind these model inputs")
    calendar = validate_calendar_binding(calendar_binding)
    if calendar is None:
        raise AnnualProjectionError("annual projection requires a bound fiscal calendar")
    structure = held["financial_statement_structure"]
    replay = held["financial_statement_structure_replay"]
    binding = held["forecast_structure_binding"]
    periods = list(dict.fromkeys([
        *held["history_periods"],
        *[item["end"] for item in held["realised_periods"] + held["forecast_periods"]],
    ]))
    groups = fiscal_groups(periods, calendar, set(held["history_periods"]))
    drivers = build_structure_drivers(inputs, structure)
    historical_values, historical_refs = structure_historical_values(
        drivers, structure)
    historical_starts: dict[str, str] = {}
    for driver in drivers:
        for cell in driver.get("history") or []:
            if cell.get("period_start") and cell.get("period_end"):
                prior = historical_starts.setdefault(
                    str(cell["period_end"]), str(cell["period_start"]))
                if prior != str(cell["period_start"]):
                    raise AnnualProjectionError(
                        "filed structure lines disagree on a quarter's fiscal window")
    rows: list[dict[str, Any]] = []
    for label, group in groups:
        kind = ("partial" if label.endswith("(partial)") else
                "mixed" if label.endswith("A/E") else
                "historical" if label.endswith("A") else "forecast")
        row: dict[str, Any] = {
            "label": label, "kind": kind, "quarter_ends": list(group),
            "historical_eps": None, "forecast_shares": None,
            "forecast_eps": None,
        }
        if kind == "historical":
            row["historical_eps"] = _historical_eps(
                inputs, structure, label, group, calendar)
        elif kind in {"forecast", "mixed"}:
            shares, eps = _forecast_outcomes(
                held, structure, label, group, calendar,
                historical_values=historical_values,
                historical_refs=historical_refs,
                historical_starts=historical_starts,
            )
            row["forecast_shares"] = shares
            row["forecast_eps"] = eps
        rows.append(row)
    body = {
        "schema_version": SCHEMA_VERSION,
        "projection_ref": (
            f"annual-projection:{held['id']}:{calendar['content_hash']}"),
        "company_ref": held["company_ref"],
        "model_version_ref": held["id"],
        "model_version_hash": held["content_hash"],
        "spec_ref": held["spec_ref"], "spec_hash": held["spec_hash"],
        "inputs_hash": exact_inputs_hash,
        "structure_ref": structure["structure_ref"],
        "structure_hash": structure["content_hash"],
        "structure_replay_hash": content_hash(replay),
        "forecast_structure_binding_hash": binding["content_hash"],
        "calendar_binding": calendar,
        "periods": rows,
    }
    return {**body, "content_hash": content_hash(body)}


def validate_annual_projection(
    value: Mapping[str, Any], *, model: Mapping[str, Any],
    inputs: Mapping[str, Any], calendar_binding: Mapping[str, Any],
) -> dict[str, Any]:
    exact = build_annual_projection(
        model=model, inputs=inputs, calendar_binding=calendar_binding)
    if dict(value) != exact:
        raise AnnualProjectionError("annual projection differs from its exact authorities")
    return exact


def validate_projection_record(
    value: Mapping[str, Any], *, model: Mapping[str, Any],
) -> dict[str, Any]:
    """Validate a persisted projection's immutable model-side bindings."""

    wire = dict(value)
    expected = {
        "schema_version", "projection_ref", "company_ref", "model_version_ref",
        "model_version_hash", "spec_ref", "spec_hash", "inputs_hash",
        "structure_ref", "structure_hash", "structure_replay_hash",
        "forecast_structure_binding_hash", "calendar_binding", "periods",
        "content_hash",
    }
    asserted = wire.pop("content_hash", None)
    structure = model.get("financial_statement_structure") or {}
    replay = model.get("financial_statement_structure_replay") or {}
    binding = model.get("forecast_structure_binding") or {}
    if (
        set(value) != expected or wire.get("schema_version") != SCHEMA_VERSION
        or not isinstance(asserted, str) or asserted != content_hash(wire)
        or wire.get("company_ref") != model.get("company_ref")
        or wire.get("model_version_ref") != model.get("id")
        or wire.get("model_version_hash") != model.get("content_hash")
        or wire.get("spec_ref") != model.get("spec_ref")
        or wire.get("spec_hash") != model.get("spec_hash")
        or wire.get("inputs_hash") != model.get("inputs_hash")
        or wire.get("structure_ref") != structure.get("structure_ref")
        or wire.get("structure_hash") != structure.get("content_hash")
        or wire.get("structure_replay_hash") != content_hash(replay)
        or wire.get("forecast_structure_binding_hash") != binding.get("content_hash")
        or validate_calendar_binding(wire.get("calendar_binding"))
            != wire.get("calendar_binding")
        or not isinstance(wire.get("periods"), list)
    ):
        raise AnnualProjectionError("annual projection record authority differs")
    return {**wire, "content_hash": asserted}


__all__ = [
    "AnnualProjectionError", "SCHEMA_VERSION", "build_annual_projection",
    "calendar_binding_from_annual_filing", "fiscal_groups",
    "validate_annual_projection", "validate_calendar_binding",
    "validate_projection_record",
]
