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
    STRUCTURED_CASH_SCHEMA_VERSION,
    build_cash_flow_companion_drivers,
    build_structure_drivers,
    is_structured_schema,
    structure_result_refs,
    structure_historical_values,
    validate_forecast_model,
)
from .store import canonical_json, content_hash


SCHEMA_VERSION = "company-model-annual-projection-0.2"
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
    try:
        as_of = date.fromisoformat(binding["as_of"])
    except ValueError as exc:
        raise AnnualProjectionError("fiscal calendar as_of is not an ISO date") from exc
    if as_of.month != month:
        raise AnnualProjectionError(
            "fiscal calendar month differs from its annual filing date")
    return {**binding, "content_hash": asserted}


def verify_statement_filing(
    connection: Any, filing: Mapping[str, Any],
) -> None:
    """Replay one immutable statement filing and all of its stored lines."""

    rows = connection.execute(
        "SELECT * FROM coverage_mission_statement_lines "
        "WHERE ingest_id=? ORDER BY ordinal", (filing["ingest_id"],),
    ).fetchall()
    if len(rows) != int(filing["line_count"]):
        raise AnnualProjectionError("annual filing authority line count is invalid")
    identity = {
        "company_ref": filing["company_ref"], "cik": filing["cik"],
        "accession": filing["accession"], "form": filing["form"],
        "line_count": int(filing["line_count"]),
    }
    ingest_id = f"statement-ingest:{content_hash(identity)[:32]}"
    if filing["ingest_id"] != ingest_id:
        raise AnnualProjectionError("annual filing authority ingest identity is invalid")
    for ordinal, row in enumerate(rows):
        if (row["ingest_id"] != ingest_id or row["ordinal"] != ordinal
                or row["line_id"] != f"{ingest_id}#{ordinal}"):
            raise AnnualProjectionError("annual filing authority line identity is invalid")
    common_fields = (
        "statement", "concept", "label", "level", "parent_concept",
        "is_breakdown", "dimension_axis", "dimension_member", "period_start",
        "period_end", "value", "unit", "balance",
    )
    line_hashes = []
    for fields in (
            common_fields,
            common_fields[:8] + ("dimension_count",) + common_fields[8:]):
        lines = []
        for row in rows:
            line = {field: row[field] for field in fields}
            line["is_breakdown"] = bool(line["is_breakdown"])
            lines.append(line)
        line_hashes.append(content_hash(lines))
    source_refs = filing.get("source_record_refs")
    if source_refs is None:
        try:
            source_refs = json.loads(filing["source_record_refs_json"])
        except (KeyError, TypeError, json.JSONDecodeError) as exc:
            raise AnnualProjectionError(
                "annual filing source records are invalid") from exc
    body = {
        "company_ref": filing["company_ref"], "cik": filing["cik"],
        "accession": filing["accession"], "form": filing["form"],
        "line_count": filing["line_count"], "entity_name": filing["entity_name"],
        "filed": filing["filed"], "report_date": filing["report_date"],
        "source_record_refs": source_refs,
        "governance_ref": filing["governance_ref"],
        "governance_hash": filing["governance_hash"],
    }
    matches = [line_hash for line_hash in line_hashes
               if content_hash({**body, "statement_lines_hash": line_hash})
               == filing["content_hash"]]
    if len(matches) != 1:
        raise AnnualProjectionError("annual filing authority hash is invalid")


def validate_stored_calendar_binding(
    connection: Any, value: Mapping[str, Any], *, company_ref: str,
) -> dict[str, Any]:
    """Bind a calendar to the exact stored 10-K whose lines replay."""

    binding = validate_calendar_binding(value)
    assert binding is not None
    row = connection.execute(
        "SELECT * FROM coverage_mission_statement_filings WHERE ingest_id=?",
        (binding["calendar_ref"],),
    ).fetchone()
    if row is None:
        raise AnnualProjectionError("fiscal calendar annual filing is unavailable")
    filing = dict(row)
    if (filing.get("company_ref") != company_ref or filing.get("form") != "10-K"):
        raise AnnualProjectionError(
            "fiscal calendar is not this company's stored annual filing")
    if (filing.get("content_hash") != binding["source_hash"]
            or filing.get("report_date") != binding["as_of"]):
        raise AnnualProjectionError("fiscal calendar differs from its stored annual filing")
    verify_statement_filing(connection, filing)
    return binding


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


def _line_outcomes(
    model: Mapping[str, Any], inputs: Mapping[str, Any],
    structure: Mapping[str, Any], label: str, group: Sequence[str],
    calendar: Mapping[str, Any], *,
    historical_values: Mapping[str, Mapping[str, Decimal]],
    historical_refs: Mapping[str, Mapping[str, Sequence[Mapping[str, Any]]]],
    historical_starts: Mapping[str, str],
    historical_eps: Mapping[str, Any] | None,
    forecast_shares: Mapping[str, Any] | None,
    forecast_eps: Mapping[str, Any] | None,
) -> dict[str, dict[str, Any]]:
    """Project every structured income result under its declared semantics."""

    result_refs = structure_result_refs(structure)
    results = {str(item["ref"]): item for item in (model.get("results") or [])}
    formulas = {str(item["output_ref"]): item
                for item in (structure.get("formulas") or [])}
    fiscal_year = f"{calendar['calendar_ref']}:{label}"
    projected: dict[str, dict[str, Any]] = {}
    for line in structure.get("lines") or []:
        if line.get("statement") != "income":
            continue
        line_ref = str(line["ref"])
        result_ref = result_refs[line_ref]
        result = results.get(result_ref)
        if not isinstance(result, Mapping):
            raise AnnualProjectionError(
                f"structured income line {line_ref} has no model result")
        role = str(line["role"])
        if role == "diluted_eps":
            outcome = historical_eps if historical_eps is not None else forecast_eps
            outcome = dict(outcome or {
                "status": "unavailable", "value": None,
                "reason": "annual diluted EPS authority is unavailable",
            })
        elif (role == "diluted_weighted_average_shares"
              and forecast_shares is not None):
            outcome = dict(forecast_shares)
        elif line.get("annual_semantics") == "sum_quarters":
            definition = _line_definition_ref(
                structure, line,
                line.get("concept") if line.get("kind") == "filed"
                else (formulas.get(line_ref) or {}).get("tie_out_concept"),
            )
            cells = _projected_quarter_cells(
                model, line, result, group, calendar=calendar,
                definition_ref=definition,
                historical_values=historical_values,
                historical_refs=historical_refs,
                historical_starts=historical_starts,
            )
            for cell in cells:
                cell["fiscal_year"] = fiscal_year
            outcome = aggregate_fiscal_year(
                cells, semantic="sum_quarters", fiscal_year=fiscal_year)
        elif line.get("annual_semantics") == "direct_annual":
            cells = _structure_facts(
                inputs, structure, line, group, label, calendar,
                formulas.get(line_ref),
            )
            outcome = aggregate_fiscal_year(
                cells, semantic="direct_annual", fiscal_year=fiscal_year)
        else:
            outcome = {
                "status": "unavailable", "value": None,
                "reason": (
                    f"annual semantics {line.get('annual_semantics')} "
                    "has no amount aggregation contract"
                ),
            }
        projected[result_ref] = {
            "line_ref": line_ref, "role": role, "label": str(line["label"]),
            **outcome,
        }
    return projected


def _cash_line_outcomes(
    model: Mapping[str, Any], inputs: Mapping[str, Any], label: str,
    group: Sequence[str], calendar: Mapping[str, Any],
) -> dict[str, dict[str, Any]]:
    """Annualize the exact companion quarters without inventing a cash statement."""

    companion = model.get("cash_flow_companion") or {}
    try:
        drivers = build_cash_flow_companion_drivers(inputs, companion)
    except Exception as exc:
        raise AnnualProjectionError("cash-flow companion inputs do not replay") from exc
    results = {str(item.get("ref")): item for item in (model.get("results") or [])}
    by_role = {str(item.get("role")): item for item in drivers}
    fiscal_year = f"{calendar['calendar_ref']}:{label}"
    quarter_cells: dict[str, list[dict[str, Any]]] = {}
    projected: dict[str, dict[str, Any]] = {}
    for line in companion.get("lines") or []:
        role, result_ref = str(line["role"]), str(line["result_ref"])
        driver = by_role.get(role)
        result = results.get(result_ref)
        by_end: dict[str, dict[str, Any]] = {}
        for cell in (driver or {}).get("history") or []:
            if cell.get("period_start") and cell.get("period_end"):
                by_end[str(cell["period_end"])] = {
                    "period_start": str(cell["period_start"]),
                    "period_end": str(cell["period_end"]),
                    "period_kind": "quarter", "value": str(cell["value"]),
                    "unit": str(line.get("unit") or "").casefold(),
                    "calendar": calendar["content_hash"],
                    "definition_ref": f"{companion['content_hash']}:{line['ref']}",
                    "fiscal_year": fiscal_year,
                    "source_accessions": list(cell.get("accessions") or []),
                    "source_forms": list(cell.get("source_forms") or []),
                    "derived_from": [dict(item) for item in
                                     (cell.get("derived_from") or [])],
                    "input_cell_refs": [{
                        "kind": "input_cell", "ref": None,
                        "concept": str(cell["concept"]),
                        "period_end": str(cell["period_end"]),
                        "accession": str(accession),
                    } for accession in (cell.get("accessions") or [])],
                }
        for cell in (result or {}).get("cells") or []:
            period = cell.get("period") or {}
            if (cell.get("status") == "computed" and not cell.get("superseded_by")
                    and period.get("kind") == "quarter"):
                by_end[str(period["end"])] = {
                    "period_start": str(period["start"]),
                    "period_end": str(period["end"]), "period_kind": "quarter",
                    "value": str(cell["value"]),
                    "unit": str(line.get("unit") or "").casefold(),
                    "calendar": calendar["content_hash"],
                    "definition_ref": f"{companion['content_hash']}:{line['ref']}",
                    "fiscal_year": fiscal_year, "model_cell_ref": cell["ref"],
                }
        selected = [by_end[end] for end in group if end in by_end]
        quarter_cells[result_ref] = selected
        outcome = aggregate_fiscal_year(
            selected, semantic="sum_quarters", fiscal_year=fiscal_year)
        if outcome.get("status") == "unavailable" and selected:
            # An incomplete annual result is still backed by real filed/model
            # quarters.  Keep those exact dependencies visible while refusing
            # to turn a partial year into a value.
            outcome = {**outcome, "source_periods": selected}
        projected[result_ref] = {
            "line_ref": str(line["ref"]), "role": role,
            "label": ("Operating cash flow" if role == "operating_cash_flow"
                      else "Capital expenditure"),
            **outcome,
        }
    fcf_cells: list[dict[str, Any]] = []
    ocf = {item["period_end"]: item for item in
           quarter_cells.get("result:operating_cash_flow", [])}
    capex = {item["period_end"]: item for item in
             quarter_cells.get("result:capital_expenditure", [])}
    for end in group:
        if end not in ocf or end not in capex or (
            ocf[end].get("period_start") != capex[end].get("period_start")
            or ocf[end].get("period_end") != capex[end].get("period_end")
            or ocf[end].get("unit") != capex[end].get("unit")
        ):
            continue
        fcf_cells.append({
            "period_start": ocf[end]["period_start"], "period_end": end,
            "period_kind": "quarter",
            "value": str(Decimal(ocf[end]["value"]) - Decimal(capex[end]["value"])),
            "unit": ocf[end]["unit"], "calendar": calendar["content_hash"],
            "definition_ref": f"{companion['content_hash']}:result:free_cash_flow",
            "fiscal_year": fiscal_year,
            "result_refs": [
                {"ref": "result:operating_cash_flow", "period_end": end},
                {"ref": "result:capital_expenditure", "period_end": end},
            ],
        })
    projected["result:free_cash_flow"] = {
        "line_ref": "cash-flow:free-cash-flow", "role": "free_cash_flow",
        "label": "Free cash flow",
        **aggregate_fiscal_year(
            fcf_cells, semantic="sum_quarters", fiscal_year=fiscal_year),
    }
    return projected


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
    if not is_structured_schema(held.get("schema_version")):
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
        row["line_outcomes"] = _line_outcomes(
            held, inputs, structure, label, group, calendar,
            historical_values=historical_values,
            historical_refs=historical_refs,
            historical_starts=historical_starts,
            historical_eps=row["historical_eps"],
            forecast_shares=row["forecast_shares"],
            forecast_eps=row["forecast_eps"],
        )
        if held.get("schema_version") == STRUCTURED_CASH_SCHEMA_VERSION:
            row["line_outcomes"].update(
                _cash_line_outcomes(held, inputs, label, group, calendar))
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
        or wire.get("projection_ref") != (
            f"annual-projection:{model.get('id')}:"
            f"{(wire.get('calendar_binding') or {}).get('content_hash')}")
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
    "validate_projection_record", "validate_stored_calendar_binding",
    "verify_statement_filing",
]
