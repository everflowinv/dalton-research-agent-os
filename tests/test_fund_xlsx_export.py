from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from dalton_core.company_model_inputs import build_model_inputs
from dalton_core.fund_xlsx_export import (
    FundWorkbookExportError,
    _four_quarter_flow_cells,
    export_company_workbook,
    export_fund_workbook,
)
from dalton_core.model_forecast_driver import ForecastModelAuthority
from dalton_core.store import DaltonStore, content_hash
from tests.test_model_forecast_driver import ledger, model, spec


class FundXlsxExportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = DaltonStore(":memory:")
        self.addCleanup(self.store.close)
        self.specification = spec(quarters=8)
        self.inputs = build_model_inputs(ledger(), self.specification)
        self.model = ForecastModelAuthority(self.store).publish(
            model(specification=self.specification))
        self.path = Path(self.temp.name) / "fund-model.xlsx"

    def scenario(self):
        value = {"scenario_ref": "valuation-scenario:test", "as_of": "2026-09-10",
                 "multiple_kind": "ev_revenue", "multiple": "2",
                 "net_cash": "100000000", "diluted_shares": "100000000",
                 "required_return": "0.10", "year_fraction": "1"}
        return {**value, "content_hash": content_hash(value)}

    def calendar(self):
        value = {"calendar_ref": "fiscal-calendar:acn:2026-09-10",
                 "source_hash": "1" * 64, "as_of": "2026-09-10",
                 "fiscal_year_end_month": 8}
        return {**value, "content_hash": content_hash(value)}

    def export(self, *, scenario=None):
        return export_fund_workbook(
            self.path, model=self.model, spec=self.specification,
            inputs=self.inputs, valuation_scenario=scenario,
            calendar_binding=self.calendar())

    def test_real_internal_model_exports_formula_chain_and_exact_bindings(self):
        from openpyxl import load_workbook

        result = self.export()
        book = load_workbook(self.path, data_only=False)
        self.assertEqual(book.sheetnames,
                         ["Driver", "Financials", "Valuation", "Sources", "Formula Map"])
        headers = {cell.value: cell.column for cell in book["Financials"][4]}
        revenue = book["Financials"].cell(5, headers["Q4 FY2026E"])
        self.assertTrue(revenue.value.startswith("='Driver'!J5*(1+"), revenue.value)
        assumption_row = next(
            row for row in range(5, book["Driver"].max_row + 1)
            if "Revenues (ratio) — quarterly_growth — assumption"
            in str(book["Driver"].cell(row, 1).value)
        )
        assumption = book["Driver"].cell(
            assumption_row, headers["Q4 FY2026E"]
        )
        self.assertEqual(assumption.font.color.rgb[-6:], "0000FF")
        self.assertIn(self.model["content_hash"],
                      [cell.value for cell in book["Sources"]["C"]])
        hashes = [book["Formula Map"].cell(row, 2).value
                  for row in range(1, book["Formula Map"].max_row + 1)
                  if book["Formula Map"].cell(row, 1).value == "Formula map SHA-256"]
        self.assertEqual(hashes, [result["formula_map_hash"]])

    def test_complete_year_is_formula_and_partial_year_stays_blank(self):
        from openpyxl import load_workbook

        result = self.export()
        book = load_workbook(self.path, data_only=False)
        sheet = book["Financials"]
        headers = {cell.value: cell.column for cell in sheet[4]}
        self.assertIsNone(sheet.cell(5, headers["FY2025A"]).value)
        self.assertTrue(sheet.cell(5, headers["FY2026A/E"]).value.startswith("=SUM("))
        self.assertTrue(sheet.cell(5, headers["FY2027E"]).value.startswith("=SUM("))
        self.assertTrue(any("FY2025A: annual unavailable" in gap for gap in result["gaps"]))

    def test_annual_sum_requires_four_typed_duration_quarters(self):
        ends = ["2025-11-30", "2026-02-28", "2026-05-31", "2026-08-31"]
        starts = ["2025-09-01", "2025-12-01", "2026-03-01", "2026-06-01"]
        cells = {
            end: {"period": {"start": start, "end": end,
                             "kind": "quarter", "calendar": "company:fiscal"}}
            for start, end in zip(starts, ends)
        }
        self.assertTrue(_four_quarter_flow_cells(cells, ends))
        instant = {key: {"period": dict(value["period"])}
                   for key, value in cells.items()}
        instant[ends[2]]["period"]["start"] = None
        self.assertFalse(_four_quarter_flow_cells(instant, ends))
        wrong_kind = {key: {"period": dict(value["period"])}
                      for key, value in cells.items()}
        wrong_kind[ends[2]]["period"]["kind"] = "instant"
        self.assertFalse(_four_quarter_flow_cells(wrong_kind, ends))

    def test_fund_layout_and_formula_roles_are_explicit(self):
        from openpyxl import load_workbook

        self.export(scenario=self.scenario())
        book = load_workbook(self.path, data_only=False)
        sheet = book["Financials"]
        headers = [cell.value for cell in sheet[4]]
        self.assertEqual(headers[1:5], [
            "FY2025A", "FY2026A/E", "FY2027E", "FY2028E (partial)",
        ])
        self.assertIsNone(headers[5])
        self.assertEqual(headers[6:11], [
            "Q4 FY2025A", "Q1 FY2026A", "Q2 FY2026A",
            "Q3 FY2026A", "Q4 FY2026E",
        ])
        self.assertEqual(sheet["A4"].fill.fgColor.rgb[-6:], "1F4E78")
        self.assertEqual(sheet["A4"].font.color.rgb[-6:], "FFFFFF")
        self.assertEqual(sheet.freeze_panes, "G5")
        self.assertEqual(sheet.column_dimensions["F"].width, 3)

        # Cross-sheet links are green; local quarter and annual formulas are black.
        self.assertEqual(sheet["G5"].font.color.rgb[-6:], "008000")
        self.assertEqual(sheet["K7"].font.color.rgb[-6:], "000000")
        self.assertEqual(sheet["C5"].font.color.rgb[-6:], "000000")
        self.assertIn('"$"', sheet["C5"].number_format)
        self.assertEqual(book["Valuation"]["B8"].number_format,
                         "0.0%;(0.0%);-")
        self.assertIn('"$"', book["Valuation"]["B13"].number_format)

    def test_monetary_display_uses_millions_without_scaling_values_or_formulas(self):
        from openpyxl import load_workbook

        export_fund_workbook(
            self.path,
            model=self.model,
            spec=self.specification,
            inputs=self.inputs,
            calendar_binding=self.calendar(),
            mission_binding={
                "ref": "coverage-mission-version:test:1",
                "content_hash": "1" * 64,
                "created_at": "2026-09-10T00:00:00+00:00",
                "ticker": "ACN",
                "entity_name": None,
            },
        )
        book = load_workbook(self.path, data_only=False)
        financials = book["Financials"]
        headers = {cell.value: cell.column for cell in financials[4]}
        revenue = financials.cell(5, headers["Q4 FY2026E"])
        self.assertIn("(USD millions)", financials["A5"].value)
        self.assertTrue(revenue.value.startswith("='Driver'!J5*(1+"))
        self.assertTrue(revenue.number_format.endswith(",,);-"))
        self.assertEqual(book["Driver"]["G5"].value, 1_000_000_000)
        self.assertEqual(financials["A2"].value, "ACN")
        formula_map = book["Formula Map"]
        scale = next(
            formula_map.cell(row, 2).value
            for row in range(1, formula_map.max_row + 1)
            if formula_map.cell(row, 1).value == "Monetary display scale"
        )
        self.assertEqual(scale, 1_000_000)

    def test_without_calendar_binding_does_not_guess_annual_columns(self):
        from openpyxl import load_workbook

        result = export_fund_workbook(
            self.path, model=self.model, spec=self.specification, inputs=self.inputs)
        book = load_workbook(self.path, data_only=False)
        self.assertFalse(any(str(cell.value).startswith("FY")
                             for cell in book["Financials"][4]))
        self.assertIn("annual columns unavailable: no bound fiscal calendar",
                      result["gaps"])

    def test_tampered_calendar_binding_is_refused(self):
        calendar = self.calendar()
        calendar["fiscal_year_end_month"] = 12
        with self.assertRaisesRegex(FundWorkbookExportError, "calendar binding content hash"):
            export_fund_workbook(
                self.path, model=self.model, spec=self.specification,
                inputs=self.inputs, calendar_binding=calendar)
        self.assertFalse(self.path.exists())

    def test_changed_assumption_recalculates_quarter_annual_and_income_chain(self):
        soffice = shutil.which("soffice")
        if not soffice:
            self.skipTest("LibreOffice is unavailable")
        from openpyxl import load_workbook

        self.export(scenario=self.scenario())
        book = load_workbook(self.path, data_only=False)
        revenue_assumption = next(
            row for row in range(5, book["Driver"].max_row + 1)
            if "Revenues (ratio) — quarterly_growth — assumption"
            in str(book["Driver"].cell(row, 1).value))
        headers = {cell.value: cell.column for cell in book["Driver"][4]}
        book["Driver"].cell(revenue_assumption, headers["Q4 FY2026E"], 0.20)
        changed = Path(self.temp.name) / "changed" / "changed.xlsx"
        changed.parent.mkdir()
        book.save(changed)
        out = Path(self.temp.name) / "recalculated"
        out.mkdir()
        profile = Path(self.temp.name) / "lo-profile"
        subprocess.run([
            soffice, "--headless", f"-env:UserInstallation=file://{profile}",
            "--convert-to", "xlsx", "--outdir", str(out), str(changed),
        ], check=True, capture_output=True, text=True)
        calculated = load_workbook(out / "changed.xlsx", data_only=True)
        headers = {cell.value: cell.column for cell in calculated["Financials"][4]}
        revenue = calculated["Financials"].cell(5, headers["Q4 FY2026E"]).value
        operating_income = calculated["Financials"].cell(9, headers["Q4 FY2026E"]).value
        self.assertAlmostEqual(revenue, 1597200000, places=2)
        self.assertAlmostEqual(operating_income, 159720000, places=2)
        headers = {cell.value: cell.column for cell in calculated["Financials"][4]}
        self.assertIsNotNone(calculated["Financials"].cell(5, headers["FY2027E"]).value)
        self.assertAlmostEqual(calculated["Valuation"]["B13"].value,
                               149.161194909091, places=5)

    def test_tampered_valuation_scenario_is_refused(self):
        scenario = self.scenario()
        scenario["multiple"] = "20"
        with self.assertRaisesRegex(FundWorkbookExportError, "content hash"):
            self.export(scenario=scenario)
        self.assertFalse(self.path.exists())

    def test_wrong_spec_or_input_hash_is_refused_before_file_creation(self):
        wrong_spec = dict(self.specification)
        wrong_spec["content_hash"] = "0" * 64
        with self.assertRaisesRegex(FundWorkbookExportError, "specification"):
            export_fund_workbook(self.path, model=self.model, spec=wrong_spec,
                                 inputs=self.inputs)
        self.assertFalse(self.path.exists())
        wrong_inputs = dict(self.inputs)
        wrong_inputs["company_ref"] = "company:wrong"
        with self.assertRaisesRegex(FundWorkbookExportError, "model inputs"):
            export_fund_workbook(self.path, model=self.model,
                                 spec=self.specification, inputs=wrong_inputs)
        self.assertFalse(self.path.exists())

    def test_existing_output_is_not_overwritten(self):
        self.path.write_bytes(b"original")
        with self.assertRaisesRegex(FundWorkbookExportError, "refusing to overwrite"):
            self.export()
        self.assertEqual(self.path.read_bytes(), b"original")

    def test_core_database_cannot_be_selected_as_output(self):
        core = Path(self.temp.name) / "core.sqlite"
        core.write_bytes(b"sqlite authority bytes")
        with self.assertRaisesRegex(FundWorkbookExportError, "Core database"):
            export_company_workbook(core, self.model["company_ref"], core)
        self.assertEqual(core.read_bytes(), b"sqlite authority bytes")


if __name__ == "__main__":
    unittest.main()
