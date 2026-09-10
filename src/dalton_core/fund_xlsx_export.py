"""Read-only export of one governed forecast into an auditable fund workbook."""
from __future__ import annotations

import argparse
import json
import math
import os
import sqlite3
import tempfile
from collections import defaultdict
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping, Sequence

from .company_model_inputs import build_model_inputs
from .coverage_mission import CoverageMissionAuthority
from .model_forecast_driver import ForecastModelAuthority, validate_forecast_model
from .store import DaltonStore, canonical_json, content_hash
from .valuation_snapshot import ValuationSnapshotAuthority

LAYOUT_VERSION = "fund-xlsx-layout:0.2"


class FundWorkbookExportError(RuntimeError):
    pass


def _xlsx() -> tuple[Any, Any, Any, Any]:
    try:
        from openpyxl import Workbook, load_workbook
        from openpyxl.styles import Font, PatternFill
        return Workbook, load_workbook, Font, PatternFill
    except ImportError as exc:
        raise FundWorkbookExportError(
            "XLSX export needs the optional `prior-models` dependency") from exc


def _number(value: Any) -> float:
    number = Decimal(str(value))
    if not number.is_finite():
        raise FundWorkbookExportError("workbook number must be finite")
    converted = float(number)
    if not math.isfinite(converted):
        raise FundWorkbookExportError("workbook number exceeds Excel numeric range")
    if number and abs((Decimal(str(converted)) - number) / number) > Decimal("1e-15"):
        raise FundWorkbookExportError("workbook number exceeds Excel's reliable precision")
    return converted


def _col(index: int) -> str:
    from openpyxl.utils import get_column_letter
    return get_column_letter(index)


def _style_sheet(ws: Any, font: Any, fill: Any) -> None:
    ws.sheet_view.showGridLines = False
    ws.freeze_panes = "B5"
    ws.column_dimensions["A"].width = 37
    for cell in ws[1]:
        cell.font = font(name="Arial", size=15, bold=True, color="FFFFFF")
        cell.fill = fill("solid", fgColor="17365D")
    for cell in ws[4]:
        cell.font = font(name="Arial", size=10, bold=True, color="FFFFFF")
        cell.fill = fill("solid", fgColor="1F4E78")


def _quarter_label(
    period: str,
    binding: Mapping[str, Any] | None,
    historical: set[str],
) -> str:
    suffix = "A" if period in historical else "E"
    if binding is None:
        return f"{period}{suffix}"
    year = int(period[:4])
    month = int(period[5:7])
    fiscal_end = binding["fiscal_year_end_month"]
    fiscal_year = year if month <= fiscal_end else year + 1
    months_to_end = (fiscal_end - month) % 12
    quarter = 4 - (months_to_end // 3)
    if months_to_end % 3:
        return f"{period}{suffix}"
    return f"Q{quarter} FY{fiscal_year}{suffix}"


def _number_format(unit: str, *, per_share: bool = False) -> str:
    if unit == "ratio":
        return "0.0%;(0.0%);-"
    if per_share:
        return '"$"#,##0.00;[Red]("$"#,##0.00);-'
    if unit == "USD":
        return '"$"#,##0;[Red]("$"#,##0);-'
    return "#,##0;[Red](#,##0);-"


def _formula_for(
    result: Mapping[str, Any], cell: Mapping[str, Any], period: str,
    result_cells: Mapping[tuple[str, str], str], assumption_cells: Mapping[str, str],
    history_cells: Mapping[tuple[str, str], str],
) -> str | None:
    refs = [result_cells.get((r["ref"], r["period_end"]))
            for r in cell.get("result_refs") or []]
    if any(ref is None for ref in refs):
        return None
    assumptions = [assumption_cells.get(ref) for ref in cell.get("assumption_refs") or []]
    if any(ref is None for ref in assumptions):
        return None
    formula = result["formula"]
    if (formula == "revenue[k] = revenue[k-1] * (1 + growth[k])"
            and len(refs) <= 1 and len(assumptions) == 1):
        prior_refs = list(cell.get("result_refs") or [])
        prior = refs[0] if refs else None
        if prior is None and cell.get("input_cell_refs"):
            source = cell["input_cell_refs"][0]
            prior = history_cells.get((source.get("concept"), source.get("period_end")))
        return None if prior is None else f"={prior}*(1+{assumptions[0]})"
    if (formula in {
            "cost_of_revenue[k] = revenue[k] * share[k]",
            "income_tax_expense[k] = operating_income[k] * share[k]",
            } or (result["ref"].startswith("result:operating_expense:")
                  and formula == f"{result['label']}[k] = revenue[k] * share[k]")) \
            and len(refs) == 1 and len(assumptions) == 1:
        return f"={refs[0]}*{assumptions[0]}"
    if (formula in {
            "gross_profit[k] = revenue[k] - cost_of_revenue[k]",
            "operating_income[k] = gross_profit[k] - sum(operating_expense[k])",
            "net_income[k] = operating_income[k] - income_tax[k]",
            } and len(refs) >= 2 and not assumptions):
        return f"={refs[0]}-SUM({','.join(refs[1:])})"
    return None


def _verify_translated_value(
    result: Mapping[str, Any], cell: Mapping[str, Any],
    result_values: Mapping[tuple[str, str], Decimal],
    assumption_values: Mapping[str, Decimal],
    history_values: Mapping[tuple[str, str], Decimal],
) -> None:
    refs = [result_values.get((item["ref"], item["period_end"]))
            for item in cell.get("result_refs") or []]
    assumptions = [assumption_values.get(item)
                   for item in cell.get("assumption_refs") or []]
    formula = result["formula"]
    expected: Decimal | None = None
    if formula == "revenue[k] = revenue[k-1] * (1 + growth[k])" and assumptions:
        prior = refs[0] if refs else None
        if prior is None and cell.get("input_cell_refs"):
            source = cell["input_cell_refs"][0]
            prior = history_values.get((source.get("concept"), source.get("period_end")))
        if prior is not None:
            expected = prior * (Decimal(1) + assumptions[0])
    elif (formula in {
            "cost_of_revenue[k] = revenue[k] * share[k]",
            "income_tax_expense[k] = operating_income[k] * share[k]",
            } or (result["ref"].startswith("result:operating_expense:")
                  and formula == f"{result['label']}[k] = revenue[k] * share[k]")):
        if refs and assumptions:
            expected = refs[0] * assumptions[0]
    elif formula in {
            "gross_profit[k] = revenue[k] - cost_of_revenue[k]",
            "operating_income[k] = gross_profit[k] - sum(operating_expense[k])",
            "net_income[k] = operating_income[k] - income_tax[k]",
            } and refs and all(item is not None for item in refs):
        expected = refs[0] - sum(refs[1:], Decimal(0))
    if expected is None or "value" not in cell:
        return
    actual = Decimal(str(cell["value"]))
    tolerance = max(Decimal("0.000001"), abs(actual) * Decimal("1e-12"))
    if abs(expected - actual) > tolerance:
        raise FundWorkbookExportError(
            f"translated formula disagrees with {cell['ref']}: {expected} != {actual}")


def _calendar_binding(value: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if value is None:
        return None
    binding = dict(value)
    allowed = {"calendar_ref", "source_hash", "as_of",
               "fiscal_year_end_month", "content_hash"}
    if set(binding) != allowed:
        raise FundWorkbookExportError("fiscal calendar binding has an invalid closed shape")
    asserted = binding.pop("content_hash")
    if asserted != content_hash(binding):
        raise FundWorkbookExportError("fiscal calendar binding content hash is invalid")
    month = binding["fiscal_year_end_month"]
    if isinstance(month, bool) or not isinstance(month, int) or not 1 <= month <= 12:
        raise FundWorkbookExportError("fiscal_year_end_month must be an integer from 1 to 12")
    return {**binding, "content_hash": asserted}


def _fiscal_groups(periods: Sequence[str], binding: Mapping[str, Any] | None,
                   historical: set[str]) -> list[tuple[str, list[str]]]:
    if binding is None:
        return []
    groups: list[tuple[str, list[str]]] = []
    pending: list[str] = []
    for period in periods:
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


def _four_quarter_flow_cells(
    by_period: Mapping[str, Mapping[str, Any]], group: Sequence[str]
) -> bool:
    """Prove that a fiscal-year aggregate contains four duration quarters."""

    if len(group) != 4:
        return False
    prior_end: date | None = None
    for period_end in group:
        cell = by_period.get(period_end)
        period = cell.get("period") if isinstance(cell, Mapping) else None
        if not isinstance(period, Mapping):
            return False
        if (
            period.get("kind") != "quarter"
            or period.get("end") != period_end
            or not isinstance(period.get("start"), str)
        ):
            return False
        try:
            start = date.fromisoformat(period["start"])
            end = date.fromisoformat(period_end)
        except ValueError:
            return False
        if start > end or (prior_end is not None and start <= prior_end):
            return False
        prior_end = end
    return True


def _duration_quarter_cell(cell: Mapping[str, Any], period_end: str) -> bool:
    period = cell.get("period")
    if not isinstance(period, Mapping):
        return False
    if (
        period.get("kind") != "quarter"
        or period.get("end") != period_end
        or not isinstance(period.get("start"), str)
    ):
        return False
    try:
        return date.fromisoformat(period["start"]) <= date.fromisoformat(period_end)
    except ValueError:
        return False


def export_fund_workbook(
    output: Path, *, model: Mapping[str, Any], spec: Mapping[str, Any],
    inputs: Mapping[str, Any], valuation: Mapping[str, Any] | None = None,
    valuation_scenario: Mapping[str, Any] | None = None,
    calendar_binding: Mapping[str, Any] | None = None,
    mission_binding: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Export verified authority records without changing any authority."""
    model_wire = dict(model)
    model_wire.pop("status", None)
    model = validate_forecast_model(model_wire)
    if model["spec_ref"] != spec.get("spec_id") or model["spec_hash"] != spec.get("content_hash"):
        raise FundWorkbookExportError("forecast model does not bind this specification")
    if model["inputs_hash"] != content_hash(json.loads(canonical_json(inputs))):
        raise FundWorkbookExportError("forecast model does not bind these model inputs")
    if inputs.get("company_ref") != model["company_ref"]:
        raise FundWorkbookExportError("model inputs belong to another company")
    Workbook, _, Font, PatternFill = _xlsx()
    wb = Workbook()
    wb.remove(wb.active)
    driver = wb.create_sheet("Driver")
    financials = wb.create_sheet("Financials")
    valuation_ws = wb.create_sheet("Valuation")
    sources = wb.create_sheet("Sources")
    manifest = wb.create_sheet("Formula Map")
    periods = list(dict.fromkeys([*model["history_periods"], *[p["end"] for p in model["realised_periods"] + model["forecast_periods"]]]))
    if not periods:
        periods = sorted({c["period"]["end"] for r in model["results"] for c in r["cells"]})
    history = list(model["history_periods"])
    calendar_binding = _calendar_binding(calendar_binding)
    annual_groups = _fiscal_groups(periods, calendar_binding, set(history))
    annual_columns = {
        label: 2 + index for index, (label, _) in enumerate(annual_groups)
    }
    quarter_start = 2 + len(annual_groups) + (1 if annual_groups else 0)
    period_columns = {
        period: quarter_start + index for index, period in enumerate(periods)
    }
    headers = ["Line / role", *[label for label, _ in annual_groups]]
    if annual_groups:
        headers.append(None)
    headers.extend(
        _quarter_label(period, calendar_binding, set(history)) for period in periods
    )
    formula_map: list[dict[str, Any]] = []

    for ws, title in ((driver, "Driver model"), (financials, "Financial statements"),
                      (valuation_ws, "Valuation"), (sources, "Sources and bindings"),
                      (manifest, "Formula map")):
        ws.append([title])
        ws.append([model["company_ref"], f"As of {model['created_at']}"])
        ws.append([])

    for ci, value in enumerate(headers, 1):
        driver.cell(4, ci, value)
    history_cells: dict[tuple[str, str], str] = {}
    history_values: dict[tuple[str, str], Decimal] = {}
    history_flow_periods: set[tuple[str, str]] = set()
    row = 5
    for item in model["drivers"]:
        values = {c["period_end"]: c for c in item.get("history") or []}
        unit_label = item.get("unit") or "number"
        driver.cell(row, 1, f"{item['label']} ({unit_label}) — actual")
        for period in periods:
            ci = period_columns[period]
            value = values.get(period)
            if value:
                driver.cell(row, ci, _number(value["value"]))
                driver.cell(row, ci).font = Font(name="Arial", color="0000FF")
                driver.cell(row, ci).number_format = _number_format(
                    item.get("unit") or "number"
                )
                history_cells[(item.get("concept"), period)] = f"'Driver'!{_col(ci)}{row}"
                history_cells[(item["ref"], period)] = f"'Driver'!{_col(ci)}{row}"
                history_values[(item.get("concept"), period)] = Decimal(str(value["value"]))
                history_values[(item["ref"], period)] = Decimal(str(value["value"]))
                if isinstance(value.get("period_start"), str):
                    history_flow_periods.add((item["ref"], period))
        row += 1
    assumption_cells: dict[str, str] = {}
    assumption_values: dict[str, Decimal] = {}
    driver_labels = {item["ref"]: item["label"] for item in model["drivers"]}
    for assumption in model["assumptions"]:
        label = driver_labels.get(assumption["driver_ref"], assumption["driver_ref"])
        driver.cell(
            row,
            1,
            f"{label} ({assumption['unit']}) — {assumption['measure']} — assumption",
        )
        period = assumption["period"]["end"]
        if period in periods:
            ci = period_columns[period]
            driver.cell(row, ci, _number(assumption["value"]))
            driver.cell(row, ci).font = Font(name="Arial", color="0000FF")
            driver.cell(row, ci).number_format = _number_format(assumption["unit"])
            assumption_cells[assumption["ref"]] = f"'Driver'!{_col(ci)}{row}"
            assumption_values[assumption["ref"]] = Decimal(str(assumption["value"]))
        row += 1

    for ci, value in enumerate(headers, 1):
        financials.cell(4, ci, value)
    result_rows = {item["ref"]: 5 + i for i, item in enumerate(model["results"])}
    result_cells: dict[tuple[str, str], str] = {}
    result_flow_periods: set[tuple[str, str]] = set()
    result_values = {
        (result["ref"], cell["period"]["end"]): Decimal(str(cell["value"]))
        for result in model["results"] for cell in result["cells"]
        if cell.get("status") == "computed" and "value" in cell
    }
    gaps: list[str] = []
    if calendar_binding is None:
        gaps.append("annual columns unavailable: no bound fiscal calendar")
    for result in model["results"]:
        rr = result_rows[result["ref"]]
        role = ("formula output" if result["status"] == "computed" else "unavailable")
        financials.cell(
            rr,
            1,
            f"{result['label']} ({result['unit']}) — {role} — "
            f"{result['role'] or 'calculated'}",
        )
        by_period = {c["period"]["end"]: c for c in result["cells"]}
        for period in periods:
            ci = period_columns[period]
            cell = by_period.get(period)
            formula = None
            model_cell_ref = None
            if period in history and result.get("driver_ref"):
                source = history_cells.get((result["driver_ref"], period))
                if source:
                    formula = f"={source}"
                    model_cell_ref = result["driver_ref"]
                    if (result["driver_ref"], period) in history_flow_periods:
                        result_flow_periods.add((result["ref"], period))
            elif period in history:
                components: list[str] = []
                if result["ref"] == "result:gross_profit":
                    components = [result_cells.get(("result:revenue", period)),
                                  result_cells.get(("result:cost_of_revenue", period))]
                elif result["ref"] == "result:operating_income":
                    expense_refs = [item["ref"] for item in model["results"]
                                    if item["ref"].startswith("result:operating_expense:")]
                    components = [result_cells.get(("result:gross_profit", period)),
                                  *[result_cells.get((ref, period)) for ref in expense_refs]]
                elif result["ref"] == "result:net_income":
                    components = [result_cells.get(("result:operating_income", period)),
                                  result_cells.get(("result:income_tax_expense", period))]
                if components and all(components):
                    formula = f"={components[0]}-SUM({','.join(components[1:])})"
                    model_cell_ref = "historical-derived"
                    component_refs = (
                        ["result:revenue", "result:cost_of_revenue"]
                        if result["ref"] == "result:gross_profit"
                        else ["result:gross_profit", *expense_refs]
                        if result["ref"] == "result:operating_income"
                        else ["result:operating_income", "result:income_tax_expense"]
                    )
                    if all((ref, period) in result_flow_periods
                           for ref in component_refs):
                        result_flow_periods.add((result["ref"], period))
            elif cell is not None and cell["status"] == "computed":
                formula = _formula_for(result, cell, period, result_cells,
                                       assumption_cells, history_cells)
                model_cell_ref = cell["ref"]
                if formula is not None:
                    _verify_translated_value(
                        result, cell, result_values, assumption_values,
                        history_values)
                    if _duration_quarter_cell(cell, period):
                        result_flow_periods.add((result["ref"], period))
            if formula is None:
                if cell is not None:
                    reason = cell.get("reason") if cell["status"] != "computed" else result["formula"]
                    gaps.append(f"{result['ref']} {period}: unavailable or unsupported; {reason}")
                continue
            financials.cell(rr, ci, formula)
            result_cells[(result["ref"], period)] = f"'Financials'!{_col(ci)}{rr}"
            financials.cell(rr, ci).font = Font(
                name="Arial", color="008000" if "'Driver'!" in formula else "000000"
            )
            financials.cell(rr, ci).number_format = _number_format(result["unit"])
            formula_map.append({"cell": f"Financials!{_col(ci)}{rr}",
                                "model_cell_ref": model_cell_ref, "formula": formula,
                                "model_formula": result["formula"]})
        for label, group in annual_groups:
            annual_i = annual_columns[label]
            quarter_cols = [period_columns[p] for p in group
                            if (result["ref"], p) in result_cells]
            target = financials.cell(rr, annual_i)
            flow_proven = all(
                (result["ref"], period) in result_flow_periods for period in group
            )
            if (
                flow_proven
                and len(quarter_cols) == 4
                and result["unit"] != "ratio"
            ):
                target.value = f"=SUM({_col(quarter_cols[0])}{rr}:{_col(quarter_cols[-1])}{rr})"
                target.font = Font(name="Arial", color="000000")
                target.number_format = _number_format(result["unit"])
                result_cells[(result["ref"], label)] = (
                    f"'Financials'!{target.coordinate}"
                )
                formula_map.append({"cell": f"Financials!{target.coordinate}",
                                    "model_cell_ref": None, "formula": target.value,
                                    "model_formula": "annual_from_four_fiscal_quarters"})
            else:
                target.value = None
                target.comment = None
                if result["unit"] == "ratio":
                    reason = (
                        "ratio lacks an explicit frozen numerator/denominator "
                        "formula contract"
                    )
                elif not flow_proven:
                    reason = "four typed duration-quarter cells are not proven"
                else:
                    reason = f"{len(quarter_cols)}/{len(group)} supported quarters"
                gaps.append(f"{result['ref']} {label}: annual unavailable; {reason}")
    for ws in (driver, financials):
        for col in range(2, len(headers) + 1):
            ws.column_dimensions[_col(col)].width = 16
        for cells in ws.iter_rows(min_row=5, min_col=2):
            for cell in cells:
                if cell.number_format == "General":
                    cell.number_format = "#,##0;[Red](#,##0);-"

    for ci, value in enumerate(["Metric", "Value", "Role", "Authority formula / gap"], 1):
        valuation_ws.cell(4, ci, value)
    if valuation_scenario is not None:
        scenario = dict(valuation_scenario)
        allowed = {"scenario_ref", "as_of", "multiple_kind", "multiple",
                   "net_cash", "diluted_shares", "required_return",
                   "year_fraction", "content_hash"}
        if set(scenario) != allowed:
            raise FundWorkbookExportError("valuation scenario has an invalid closed shape")
        asserted = scenario.pop("content_hash")
        if asserted != content_hash(scenario):
            raise FundWorkbookExportError("valuation scenario content hash is invalid")
        if scenario["multiple_kind"] not in {"ev_revenue", "pe_net_income"}:
            raise FundWorkbookExportError("valuation scenario multiple_kind is unsupported")
        for key in ("multiple", "diluted_shares", "required_return", "year_fraction"):
            if _number(scenario[key]) <= 0:
                raise FundWorkbookExportError(f"valuation scenario {key} must be positive")
        rows = [
            ("Selected multiple", _number(scenario["multiple"]), "owner assumption"),
            ("Net cash", _number(scenario["net_cash"]), "owner assumption"),
            ("Diluted shares", _number(scenario["diluted_shares"]), "owner assumption"),
            ("Required return", _number(scenario["required_return"]), "owner assumption"),
            ("Year fraction", _number(scenario["year_fraction"]), "owner assumption"),
        ]
        for offset, (label, value, role) in enumerate(rows, 5):
            valuation_ws.cell(offset, 1, label)
            valuation_ws.cell(offset, 2, value)
            valuation_ws.cell(offset, 3, role)
            valuation_ws.cell(offset, 2).font = Font(name="Arial", color="0000FF")
        valuation_ws["B5"].number_format = "0.0x"
        valuation_ws["B6"].number_format = _number_format("USD")
        valuation_ws["B7"].number_format = "#,##0;[Red](#,##0);-"
        valuation_ws["B8"].number_format = _number_format("ratio")
        valuation_ws["B9"].number_format = "0.0x"
        target_ref = ("result:revenue" if scenario["multiple_kind"] == "ev_revenue"
                      else "result:net_income")
        complete = [(label, group) for label, group in annual_groups
                    if len(group) == 4 and all((target_ref, p) in result_cells for p in group)]
        if complete:
            label, _ = complete[-1]
            annual_col = annual_columns[label]
            model_row = result_rows[target_ref]
            base = f"'Financials'!{_col(annual_col)}{model_row}"
            valuation_ws.append(["Forecast metric", f"={base}", "formula output", target_ref])
            if scenario["multiple_kind"] == "ev_revenue":
                valuation_ws.append(["Equity value", "=B10*B5+B6", "formula output",
                                     "forecast revenue * multiple + net cash"])
            else:
                valuation_ws.append(["Equity value", "=B10*B5", "formula output",
                                     "forecast net income * multiple"])
            valuation_ws.append(["Future target price", "=B11/B7", "formula output",
                                 "equity value / diluted shares"])
            valuation_ws.append(["Discounted target price", "=B12/(1+B8)^B9",
                                 "formula output", "future target / required return"])
            for row in range(10, 14):
                valuation_ws.cell(row, 2).font = Font(
                    name="Arial", color="008000" if row == 10 else "000000"
                )
                valuation_ws.cell(row, 2).number_format = _number_format(
                    "USD", per_share=row in {12, 13}
                )
                formula_map.append({"cell": f"Valuation!B{row}",
                                    "model_cell_ref": target_ref if row == 10 else None,
                                    "formula": valuation_ws.cell(row, 2).value,
                                    "model_formula": valuation_ws.cell(row, 4).value})
        else:
            valuation_ws.append(["Scenario output", None, "unavailable",
                                 "No complete supported fiscal-year forecast"])
    elif valuation:
        for metric in valuation.get("metrics") or []:
            status = metric.get("status")
            valuation_ws.append([metric.get("label") or metric.get("metric"),
                                 _number(metric["value"]) if status == "available" else None,
                                 "derived authority value" if status == "available" else "unavailable",
                                 metric.get("formula") if status == "available" else metric.get("reason")])
    else:
        valuation_ws.append(["Valuation", None, "unavailable", "No bound valuation authority version"])

    source_rows = [
        ["ForecastModel", model["id"], model["content_hash"], model["created_at"]],
        ["CompanyModelSpec", model["spec_ref"], model["spec_hash"], ""],
        ["ModelInput", model["inputs_hash"], model["inputs_hash"], ""],
        ["Formula", model["formula_ref"], model["formula_hash"], ""],
    ]
    if mission_binding:
        source_rows.append(["CoverageMission", mission_binding["ref"],
                            mission_binding["content_hash"],
                            mission_binding["created_at"]])
    if valuation:
        source_rows.append(["ValuationSnapshot", valuation["id"], valuation["content_hash"], valuation["as_of"]])
    if valuation_scenario:
        source_rows.append(["ValuationScenario", valuation_scenario["scenario_ref"],
                            valuation_scenario["content_hash"], valuation_scenario["as_of"]])
    if calendar_binding:
        source_rows.append(["FiscalCalendar", calendar_binding["calendar_ref"],
                            calendar_binding["content_hash"], calendar_binding["as_of"]])
    for ci, value in enumerate(["Authority", "Reference", "SHA-256 / binding", "As of"], 1):
        sources.cell(4, ci, value)
    for values in source_rows:
        sources.append(values)
    formula_digest = content_hash({"layout_version": LAYOUT_VERSION, "formulas": formula_map})
    for ci, value in enumerate(["Cell", "Model cell", "Excel formula", "Internal formula"], 1):
        manifest.cell(4, ci, value)
    for item in formula_map:
        # Formula Map is an audit table. Keep the formula literal instead of
        # executing a second, context-free copy of it on this sheet.
        manifest.append([item["cell"], item["model_cell_ref"],
                         "'" + item["formula"], item["model_formula"]])
    manifest.append([])
    manifest.append(["Layout version", LAYOUT_VERSION])
    manifest.append(["Formula map SHA-256", formula_digest])
    manifest.append(["Gaps", len(gaps)])
    for gap in gaps:
        manifest.append(["Gap", gap])
    for ws in wb.worksheets:
        _style_sheet(ws, Font, PatternFill)
        ws.column_dimensions["A"].width = max(ws.column_dimensions["A"].width or 0, 54)
        ws.sheet_properties.pageSetUpPr.fitToPage = True
        ws.page_setup.orientation = "landscape"
        ws.page_setup.paperSize = ws.PAPERSIZE_TABLOID
        ws.page_setup.fitToWidth = 1
        ws.page_setup.fitToHeight = 0
        ws.print_title_rows = "1:4"
        ws.sheet_properties.pageSetUpPr.autoPageBreaks = False
        if ws in (driver, financials) and annual_groups:
            spacer_col = 2 + len(annual_groups)
            ws.column_dimensions[_col(spacer_col)].width = 3
            ws.freeze_panes = f"{_col(quarter_start)}5"
        for row_cells in ws.iter_rows():
            for cell in row_cells:
                if cell.row not in {1, 4} and cell.font.name != "Arial":
                    cell.font = Font(name="Arial", size=10, color=cell.font.color)
    output = Path(output).expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists():
        raise FundWorkbookExportError("output already exists; refusing to overwrite")
    wb.calculation.fullCalcOnLoad = True
    wb.calculation.forceFullCalc = True
    with tempfile.NamedTemporaryFile(
            prefix=f".{output.name}.", suffix=".tmp", dir=output.parent,
            delete=False) as handle:
        temporary_output = Path(handle.name)
    try:
        os.chmod(temporary_output, 0o600)
        wb.save(temporary_output)
        os.replace(temporary_output, output)
    except BaseException:
        temporary_output.unlink(missing_ok=True)
        raise
    return {"path": str(output), "company_ref": model["company_ref"],
            "formula_count": len(formula_map), "formula_map_hash": formula_digest,
            "gaps": gaps}


def export_company_workbook(
    core_db: Path, company_ref: str, output: Path,
    *, valuation_scenario: Mapping[str, Any] | None = None,
    calendar_binding: Mapping[str, Any] | None = None,
    mission_ref: str | None = None,
) -> dict[str, Any]:
    """Read one exact Core snapshot and export without opening it writable."""
    source_path = Path(core_db).expanduser().resolve()
    if not source_path.is_file():
        raise FundWorkbookExportError("Core database does not exist")
    if Path(output).expanduser().resolve() == source_path:
        raise FundWorkbookExportError("output must not replace the Core database")
    with tempfile.TemporaryDirectory(prefix="dalton-fund-export-") as temp_dir:
        copied = Path(temp_dir) / "core.sqlite"
        source = sqlite3.connect(f"file:{source_path.as_posix()}?mode=ro", uri=True)
        target = sqlite3.connect(copied)
        try:
            source.backup(target)
        finally:
            target.close()
            source.close()
        with DaltonStore(copied) as store:
            models = ForecastModelAuthority(store)
            missions = CoverageMissionAuthority(store)
            if mission_ref is None:
                active = []
                rows = store.connection.execute(
                    "SELECT mission_ref FROM coverage_mission_pointer "
                    "ORDER BY mission_ref").fetchall()
                for row in rows:
                    candidate = missions.active_mission(row["mission_ref"])
                    if company_ref in {
                            member["company_ref"] for member in candidate["universe"]}:
                        active.append(candidate)
                if len(active) != 1:
                    raise FundWorkbookExportError(
                        "company must belong to exactly one active mission; pass mission_ref")
                mission = active[0]
            else:
                mission = missions.active_mission(mission_ref)
                if company_ref not in {
                        member["company_ref"] for member in mission["universe"]}:
                    raise FundWorkbookExportError(
                        "company is outside the requested active mission")
            model = models.latest(company_ref)
            if model is None:
                raise FundWorkbookExportError("company has no current model")
            spec_row = store.connection.execute(
                "SELECT * FROM coverage_mission_company_model_specs WHERE spec_id=?",
                (model["spec_ref"],),
            ).fetchone()
            if spec_row is None:
                raise FundWorkbookExportError(
                    "forecast model's exact specification is unavailable")
            spec = missions._model_spec_row(spec_row)
            if spec["mission_version_ref"] != mission["id"]:
                raise FundWorkbookExportError(
                    "latest forecast model is not bound to the current mission version")
            inputs = build_model_inputs(missions, spec)
            if calendar_binding is None:
                annual_filings = [item for item in missions.statement_filings(company_ref)
                                  if item["form"] == "10-K"]
                if annual_filings:
                    filing = max(
                        annual_filings,
                        key=lambda item: (item["report_date"], item["accession"]))
                    body = {
                        "company_ref": filing["company_ref"], "cik": filing["cik"],
                        "accession": filing["accession"], "form": filing["form"],
                        "line_count": filing["line_count"],
                        "entity_name": filing["entity_name"], "filed": filing["filed"],
                        "report_date": filing["report_date"],
                        "source_record_refs": filing["source_record_refs"],
                        "governance_ref": filing["governance_ref"],
                        "governance_hash": filing["governance_hash"],
                    }
                    if content_hash(body) != filing["content_hash"]:
                        raise FundWorkbookExportError(
                            "annual filing authority hash is invalid")
                    base = {
                        "calendar_ref": filing["ingest_id"],
                        "source_hash": filing["content_hash"],
                        "as_of": filing["report_date"],
                        "fiscal_year_end_month": int(filing["report_date"][5:7]),
                    }
                    calendar_binding = {**base, "content_hash": content_hash(base)}
            valuation = ValuationSnapshotAuthority(store).latest_version(company_ref)
            return export_fund_workbook(
                output, model=model, spec=spec, inputs=inputs,
                valuation=valuation, valuation_scenario=valuation_scenario,
                calendar_binding=calendar_binding,
                mission_binding={"ref": mission["id"],
                                 "content_hash": mission["content_hash"],
                                 "created_at": mission["created_at"]})


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Export a governed company model as XLSX")
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--company-ref", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--valuation-scenario", type=Path)
    parser.add_argument("--calendar-binding", type=Path)
    parser.add_argument("--mission-ref")
    args = parser.parse_args(argv)
    output = args.output.expanduser().resolve()
    protected = {args.state_dir.expanduser().resolve() / "core.sqlite"}
    for candidate in (args.valuation_scenario, args.calendar_binding):
        if candidate:
            protected.add(candidate.expanduser().resolve())
    if output in protected:
        parser.error("output must not replace a database or input configuration")
    scenario = None
    if args.valuation_scenario:
        scenario = json.loads(args.valuation_scenario.read_text(encoding="utf-8"))
    calendar = None
    if args.calendar_binding:
        calendar = json.loads(args.calendar_binding.read_text(encoding="utf-8"))
    result = export_company_workbook(
        args.state_dir / "core.sqlite", args.company_ref, output,
        valuation_scenario=scenario, calendar_binding=calendar,
        mission_ref=args.mission_ref)
    print(canonical_json(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
