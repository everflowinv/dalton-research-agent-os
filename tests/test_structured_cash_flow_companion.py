from __future__ import annotations

import copy
from decimal import Decimal
from pathlib import Path
import tempfile
import unittest

from dalton_core.company_financial_statement_structure import (
    forecast_structure_binding,
    materialize_financial_statement_structure,
)
from dalton_core.company_model_annual_projection import build_annual_projection
from dalton_core.company_model_forecast import model_digest
from dalton_core.company_model_report import render_forecast_model
from dalton_core.forecast_sensitivity import build_projection, recompute
from dalton_core.model_forecast_driver import (
    STRUCTURED_CASH_SCHEMA_VERSION,
    ForecastModelAuthority,
    ForecastModelUnavailable,
    ForecastModelValidationError,
    actualize_model,
    build_cash_flow_companion_drivers,
    build_structured_forecast_model,
    compute_cash_flow_companion_results,
    structure_formula_hash,
)
from dalton_core.store import DaltonStore, content_hash
from tests.test_company_financial_statement_structure import (
    ACCESSION,
    company_spec,
    financial_inputs,
)
from tests.test_financial_structure_forecast_consumer import forecastable_proposal


QUARTERS = (
    ("2025-01-01", "2025-03-31"),
    ("2025-04-01", "2025-06-30"),
    ("2025-07-01", "2025-09-30"),
    ("2025-10-01", "2025-12-31"),
)


def cash_input(role: str, concept: str, values: tuple[str, ...], *, unit: str = "usd") -> dict:
    return {
        "role": role, "status": "filed", "concept": concept,
        "statement": "cash", "unit": unit,
        "series": {
            "quarters": [
                {
                    "period_start": start, "period_end": end, "value": value,
                    "unit": unit, "basis": "reported",
                    "source_accessions": [ACCESSION], "source_forms": ["10-Q"],
                }
                for (start, end), value in zip(QUARTERS, values)
            ],
            "instants": [],
        },
        "gaps": [],
    }


def authorities(*, cash: bool = True, unit: str = "usd"):
    inputs = financial_inputs()
    inputs["schema_version"] = "0.3"
    if unit != "usd":
        for line in inputs["filed_lines"]:
            for cell in line["cells"].values():
                if cell["unit"] == "usd":
                    cell["unit"] = unit
                elif cell["unit"] == "usd_per_share":
                    cell["unit"] = f"{unit}_per_share"
    inputs["cash_flow_inputs"] = (
        [
            cash_input(
                "operating_cash_flow",
                "us-gaap:NetCashProvidedByUsedInOperatingActivities",
                ("100", "110", "120", "130"),
                unit=unit,
            ),
            cash_input(
                "capital_expenditure",
                "us-gaap:PaymentsToAcquirePropertyPlantAndEquipment",
                ("20", "22", "24", "26"),
                unit=unit,
            ),
        ]
        if cash else []
    )
    candidate = forecastable_proposal(inputs)
    if unit != "usd":
        for line in candidate["lines"]:
            if line["unit"] == "usd":
                line["unit"] = unit
            elif line["unit"] == "usd_per_share":
                line["unit"] = f"{unit}_per_share"
    spec = {
        **company_spec(),
        "schema_version": "0.4", "decided_by": "automation:test",
        "forecast_statements": [
            {"statement": "income", "importance": "required"},
            {"statement": "balance", "importance": "supporting"},
            {"statement": "cash", "importance": "required"},
        ],
        "financial_statement_structure": candidate,
        "cash_flow_companion": {
            "schema_version": "0.1",
            "lines": [
                {
                    "role": "operating_cash_flow",
                    "concept": "us-gaap:NetCashProvidedByUsedInOperatingActivities",
                    "forecast_method": "share_of_line",
                    "forecast_base_ref": "revenue",
                    "because": "Operating cash conversion is forecast against company revenue.",
                },
                {
                    "role": "capital_expenditure",
                    "concept": "us-gaap:PaymentsToAcquirePropertyPlantAndEquipment",
                    "forecast_method": "share_of_line",
                    "forecast_base_ref": "revenue",
                    "because": "Capital intensity is forecast against company revenue.",
                },
            ],
            "formula": {
                "output_ref": "free_cash_flow", "operator": "sum",
                "terms": [
                    {"role": "operating_cash_flow", "coefficient": "1"},
                    {"role": "capital_expenditure", "coefficient": "-1"},
                ],
            },
        },
    }
    structure, replay = materialize_financial_statement_structure(spec, inputs)
    binding = forecast_structure_binding(structure, replay, inputs)
    return inputs, spec, structure, replay, binding


class StructuredCashFlowCompanionTests(unittest.TestCase):
    def build(self, *, cash: bool = True):
        inputs, spec, structure, replay, binding = authorities(cash=cash)
        body = build_structured_forecast_model(
            spec, inputs, structure=structure, replay=replay, binding=binding)
        return inputs, spec, structure, replay, binding, body

    def publish(self, body):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        store = DaltonStore(str(Path(temporary.name) / "core.sqlite"))
        self.addCleanup(store.close)
        authority = ForecastModelAuthority(store)
        return authority, authority.publish(body)

    def test_v04_keeps_exact_selected_cash_sources_and_forecast_results(self):
        inputs, _spec, _structure, _replay, _binding, body = self.build()
        authority, held = self.publish(body)
        self.assertEqual(held["schema_version"], STRUCTURED_CASH_SCHEMA_VERSION)
        companion = held["cash_flow_companion"]
        self.assertEqual(
            companion["financial_input_hash"],
            content_hash({"cash_flow_inputs": inputs["cash_flow_inputs"]}),
        )
        for line, source in zip(companion["lines"], inputs["cash_flow_inputs"]):
            self.assertEqual(line["concept"], source["concept"])
            self.assertEqual(line["source"], source)
            self.assertEqual(line["source_hash"], content_hash(source))
        results = {item["ref"]: item for item in held["results"]}
        self.assertEqual(
            set(results) & {
                "result:operating_cash_flow", "result:capital_expenditure",
                "result:free_cash_flow",
            },
            {"result:operating_cash_flow", "result:capital_expenditure",
             "result:free_cash_flow"},
        )
        end = held["forecast_periods"][0]["end"]
        values = {
            ref: Decimal(next(cell for cell in result["cells"]
                              if cell["period"]["end"] == end)["value"])
            for ref, result in results.items() if ref in {
                "result:operating_cash_flow", "result:capital_expenditure",
                "result:free_cash_flow",
            }
        }
        self.assertEqual(
            values["result:free_cash_flow"],
            values["result:operating_cash_flow"]
            - values["result:capital_expenditure"],
        )
        reread = authority.model(held["id"])
        self.assertEqual(reread["content_hash"], held["content_hash"])

    def test_missing_cash_source_is_explicitly_unavailable_not_zero(self):
        _inputs, _spec, _structure, _replay, _binding, body = self.build(cash=False)
        _authority, held = self.publish(body)
        for ref in (
            "result:operating_cash_flow", "result:capital_expenditure",
            "result:free_cash_flow",
        ):
            result = next(item for item in held["results"] if item["ref"] == ref)
            self.assertEqual(result["status"], "unavailable")
            self.assertTrue(all(cell["value"] is None for cell in result["cells"]))

    def test_incomplete_cash_source_keeps_exact_filed_history_but_does_not_forecast(self):
        inputs, spec, _structure, _replay, _binding = authorities()
        source = inputs["cash_flow_inputs"][0]
        source["status"] = "incomplete_quarter_series"
        source["series"]["quarters"].pop()
        source["gaps"] = ["2025-12-31"]
        source["reason"] = (
            "filed cumulative series does not provide four consecutive quarters")
        structure, replay = materialize_financial_statement_structure(spec, inputs)
        binding = forecast_structure_binding(structure, replay, inputs)
        body = build_structured_forecast_model(
            spec, inputs, structure=structure, replay=replay, binding=binding)
        _authority, held = self.publish(body)
        companion = held["cash_flow_companion"]
        ocf_line = companion["lines"][0]
        self.assertEqual(ocf_line["status"], "incomplete")
        self.assertEqual(ocf_line["source"], source)
        self.assertEqual(ocf_line["source_hash"], content_hash(source))
        ocf_driver = next(item for item in held["drivers"]
                          if item.get("role") == "operating_cash_flow")
        self.assertEqual(len(ocf_driver["history"]), 3)
        ocf_result = next(item for item in held["results"]
                          if item["ref"] == "result:operating_cash_flow")
        self.assertEqual(ocf_result["status"], "unavailable")
        self.assertTrue(all(cell["value"] is None for cell in ocf_result["cells"]))

        calendar = {
            "calendar_ref": "statement-filing:test", "source_hash": "a" * 64,
            "as_of": "2025-12-31", "fiscal_year_end_month": 12,
        }
        calendar["content_hash"] = content_hash(calendar)
        annual = build_annual_projection(
            model=held, inputs=inputs, calendar_binding=calendar)
        annual_ocf = next(row for row in annual["periods"]
                          if row["label"] == "FY2025A")["line_outcomes"][
                              "result:operating_cash_flow"]
        self.assertEqual(annual_ocf["status"], "unavailable")
        self.assertEqual(len(annual_ocf["source_periods"]), 3)

    def test_duplicate_cash_quarter_end_cannot_enter_the_companion(self):
        inputs, spec, structure, replay, binding = authorities()
        duplicate = copy.deepcopy(
            inputs["cash_flow_inputs"][0]["series"]["quarters"][1])
        duplicate["period_start"] = "2025-04-02"
        duplicate["value"] = "999"
        inputs["cash_flow_inputs"][0]["series"]["quarters"].append(duplicate)
        structure, replay = materialize_financial_statement_structure(spec, inputs)
        binding = forecast_structure_binding(structure, replay, inputs)
        with self.assertRaisesRegex(
                ForecastModelUnavailable, "duplicate or overlapping quarters"):
            build_structured_forecast_model(
                spec, inputs, structure=structure, replay=replay, binding=binding)

    def test_non_usd_cash_companion_keeps_bound_company_currency(self):
        _inputs, _spec, _structure, _replay, _binding = authorities(unit="eur")
        body = build_structured_forecast_model(
            _spec, _inputs, structure=_structure, replay=_replay, binding=_binding)
        _authority, held = self.publish(body)
        self.assertEqual(held["currency"], "EUR")
        self.assertEqual(held["cash_flow_companion"]["reporting_unit"], "eur")
        self.assertEqual(
            {item["unit"] for item in held["results"]
             if item["ref"] in {"result:operating_cash_flow",
                                 "result:capital_expenditure", "result:free_cash_flow"}},
            {"eur"},
        )

    def test_negative_forecast_capex_is_unavailable_and_propagates_to_fcf(self):
        _inputs, _spec, _structure, _replay, _binding, body = self.build()
        companion = body["cash_flow_companion"]
        cash_drivers = build_cash_flow_companion_drivers(_inputs, companion)
        income_results = [copy.deepcopy(item) for item in body["results"]
                          if not item["ref"].startswith("result:operating_cash_flow")
                          and item["ref"] not in {
                              "result:capital_expenditure", "result:free_cash_flow"}]
        base_ref = companion["lines"][1]["forecast_base_result_ref"]
        base = next(item for item in income_results if item["ref"] == base_ref)
        base["cells"][0]["value"] = "-100"
        outcomes = compute_cash_flow_companion_results(
            cash_drivers, body["assumptions"], body["forecast_periods"],
            companion, income_results)
        capex = next(item for item in outcomes
                     if item["ref"] == "result:capital_expenditure")
        fcf = next(item for item in outcomes if item["ref"] == "result:free_cash_flow")
        self.assertEqual(capex["cells"][0]["status"], "unavailable")
        self.assertEqual(fcf["cells"][0]["status"], "unavailable")

    def test_old_v03_record_remains_replayable_without_new_fields(self):
        _inputs, _spec, structure, _replay, binding, body = self.build(cash=False)
        old = copy.deepcopy(body)
        old["schema_version"] = "0.3"
        old.pop("cash_flow_companion")
        old["formula_hash"] = structure_formula_hash(structure, binding)
        old["results"] = [item for item in old["results"]
                          if item["ref"] not in {
                              "result:operating_cash_flow",
                              "result:capital_expenditure", "result:free_cash_flow",
                          }]
        authority, held = self.publish(old)
        self.assertEqual(held["schema_version"], "0.3")
        self.assertNotIn("cash_flow_companion", held)
        self.assertEqual(authority.model(held["id"]), {
            key: value for key, value in held.items() if key != "status"
        })

    def test_digest_binds_cash_source_even_when_income_authority_is_same(self):
        inputs, spec, *_ = authorities()
        changed = copy.deepcopy(inputs)
        changed["cash_flow_inputs"][0]["series"]["quarters"][-1]["value"] = "131"
        self.assertNotEqual(model_digest(spec, inputs), model_digest(spec, changed))

    def test_sensitivity_report_and_annual_projection_consume_same_cash_results(self):
        inputs, _spec, _structure, _replay, _binding, body = self.build()
        _authority, held = self.publish(body)
        sensitivity = build_projection(held)
        self.assertTrue(sensitivity["drivers"])
        cash_ref = "concept:us-gaap:NetCashProvidedByUsedInOperatingActivities"
        live_assumption = next(item for item in held["assumptions"]
                               if item["driver_ref"] == cash_ref)
        recomputed, replaced = recompute(
            held, cash_ref, Decimal(live_assumption["value"]) + Decimal("0.01"))
        self.assertTrue(replaced)
        self.assertTrue(all(
            cell["status"] == "computed"
            for cell in next(item for item in recomputed
                             if item["ref"] == "result:free_cash_flow")["cells"]
        ))
        calendar = {
            "calendar_ref": "statement-filing:test", "source_hash": "a" * 64,
            "as_of": "2025-12-31", "fiscal_year_end_month": 12,
        }
        calendar["content_hash"] = content_hash(calendar)
        annual = build_annual_projection(
            model=held, inputs=inputs, calendar_binding=calendar)
        historical = next(row for row in annual["periods"] if row["label"] == "FY2025A")
        self.assertEqual(
            historical["line_outcomes"]["result:free_cash_flow"]["value"], "368")
        report = render_forecast_model(held, annual_projection=annual)
        self.assertIn("Free cash flow", report)

    def test_annual_cash_cells_keep_every_accession_and_derived_operand(self):
        inputs, spec, _structure, _replay, _binding = authorities()
        source = inputs["cash_flow_inputs"][0]["series"]["quarters"][1]
        source["basis"] = "derived_from_cumulative"
        source["source_accessions"] = [ACCESSION, "0000000000-25-000001"]
        source["derived_from"] = [
            {"period_start": "2025-01-01", "period_end": "2025-03-31",
             "value": "100", "unit": "usd", "accession": ACCESSION,
             "form": "10-Q"},
            {"period_start": "2025-01-01", "period_end": "2025-06-30",
             "value": "210", "unit": "usd",
             "accession": "0000000000-25-000001", "form": "10-Q"},
        ]
        structure, replay = materialize_financial_statement_structure(spec, inputs)
        binding = forecast_structure_binding(structure, replay, inputs)
        body = build_structured_forecast_model(
            spec, inputs, structure=structure, replay=replay, binding=binding)
        _authority, held = self.publish(body)
        calendar = {"calendar_ref": "statement-filing:test", "source_hash": "a" * 64,
                    "as_of": "2025-12-31", "fiscal_year_end_month": 12}
        calendar["content_hash"] = content_hash(calendar)
        annual = build_annual_projection(
            model=held, inputs=inputs, calendar_binding=calendar)
        ocf = next(row for row in annual["periods"] if row["label"] == "FY2025A")[
            "line_outcomes"]["result:operating_cash_flow"]
        first = ocf["source_periods"][1]
        self.assertEqual(
            {item["accession"] for item in first["input_cell_refs"]},
            {ACCESSION, "0000000000-25-000001"},
        )
        self.assertEqual(len(first["derived_from"]), 2)

    def test_cumulative_cash_operand_units_are_casefolded_but_not_cross_currency(self):
        inputs, spec, _structure, _replay, _binding = authorities(unit="eur")
        for source in inputs["cash_flow_inputs"]:
            source["unit"] = "EUR"
            for quarter in source["series"]["quarters"]:
                quarter["unit"] = "EUR"
        source = inputs["cash_flow_inputs"][0]["series"]["quarters"][1]
        source["basis"] = "derived_from_cumulative"
        source["source_accessions"] = [ACCESSION, "0000000000-25-000001"]
        source["source_forms"] = ["10-Q"]
        source["derived_from"] = [
            {"period_start": "2025-01-01", "period_end": "2025-03-31",
             "value": "100", "unit": "EUR", "accession": ACCESSION,
             "form": "10-Q"},
            {"period_start": "2025-01-01", "period_end": "2025-06-30",
             "value": "210", "unit": "EUR",
             "accession": "0000000000-25-000001", "form": "10-Q"},
        ]
        structure, replay = materialize_financial_statement_structure(spec, inputs)
        binding = forecast_structure_binding(structure, replay, inputs)
        body = build_structured_forecast_model(
            spec, inputs, structure=structure, replay=replay, binding=binding)
        _authority, held = self.publish(body)
        driver = next(item for item in held["drivers"]
                      if item.get("role") == "operating_cash_flow")
        derived = driver["history"][1]["derived_from"]
        self.assertEqual({item["unit"] for item in derived}, {"eur"})

        wrong = copy.deepcopy(body)
        cash_driver = next(item for item in wrong["drivers"]
                           if item.get("role") == "operating_cash_flow")
        cash_driver["history"][1]["derived_from"][1]["unit"] = "USD"
        with self.assertRaisesRegex(
                ForecastModelValidationError, "derived arithmetic does not replay"):
            self.publish(wrong)

    def test_annual_fcf_refuses_cash_lines_with_different_quarter_windows(self):
        inputs, spec, _structure, _replay, _binding = authorities()
        inputs["cash_flow_inputs"][1]["series"]["quarters"][0][
            "period_start"] = "2025-01-02"
        structure, replay = materialize_financial_statement_structure(spec, inputs)
        binding = forecast_structure_binding(structure, replay, inputs)
        body = build_structured_forecast_model(
            spec, inputs, structure=structure, replay=replay, binding=binding)
        _authority, held = self.publish(body)
        calendar = {"calendar_ref": "statement-filing:test", "source_hash": "a" * 64,
                    "as_of": "2025-12-31", "fiscal_year_end_month": 12}
        calendar["content_hash"] = content_hash(calendar)
        annual = build_annual_projection(
            model=held, inputs=inputs, calendar_binding=calendar)
        fcf = next(row for row in annual["periods"] if row["label"] == "FY2025A")[
            "line_outcomes"]["result:free_cash_flow"]
        self.assertEqual(fcf["status"], "unavailable")

    def test_actualization_records_cash_actual_and_replays_future_companion(self):
        inputs, spec, _structure, _replay, _binding, body = self.build()
        authority, prior = self.publish(body)
        current = copy.deepcopy(inputs)
        current["periods"].append("2026-03-31")
        actuals = {
            "revenue": 1400, "cost": 840, "opex": 280, "operating": 280,
            "interest_income": 10, "interest_expense": 20, "pretax": 270,
            "tax": 60, "net": 210, "nci": 5, "parent": 205,
            "eps_numerator": 205, "shares": 100, "eps": "2.05",
        }
        for line in current["filed_lines"]:
            line["cells"]["2026-03-31"] = {
                "period_start": "2026-01-01", "value": str(actuals[line["concept"]]),
                "unit": next(iter(line["cells"].values()))["unit"],
                "basis": "reported", "source_accessions": [ACCESSION],
            }
        for source, value in zip(current["cash_flow_inputs"], ("140", "28")):
            source["series"]["quarters"].append({
                "period_start": "2026-01-01", "period_end": "2026-03-31",
                "value": value, "unit": "usd", "basis": "reported",
                "source_accessions": [ACCESSION], "source_forms": ["10-Q"],
            })
        structure, replay = materialize_financial_statement_structure(spec, current)
        binding = forecast_structure_binding(structure, replay, current)
        updated = actualize_model(
            prior, current, structure=structure, replay=replay, binding=binding)
        self.assertIsNotNone(updated)
        held = authority.publish(updated)
        fcf = next(item for item in held["results"]
                   if item["ref"] == "result:free_cash_flow")
        actual = next(cell for cell in fcf["cells"]
                      if cell["period"]["end"] == "2026-03-31"
                      and cell["kind"] == "actual")
        self.assertEqual(actual["value"], "112.00000000")
        next_end = held["forecast_periods"][0]["end"]
        self.assertEqual(
            next(cell for cell in fcf["cells"] if cell["period"]["end"] == next_end)[
                "status"],
            "computed",
        )


if __name__ == "__main__":
    unittest.main()
