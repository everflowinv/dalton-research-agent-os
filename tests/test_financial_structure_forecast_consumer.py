from __future__ import annotations

import copy
from decimal import Decimal
from pathlib import Path
import tempfile
import unittest

from dalton_core.company_financial_statement_structure import (
    SCHEMA_VERSION as STRUCTURE_SCHEMA_VERSION,
    forecast_structure_binding,
    materialize_financial_statement_structure,
    replay_historical_structure,
    validate_financial_statement_structure,
)
from dalton_core.model_forecast_driver import (
    ForecastModelAuthority,
    ForecastModelUnavailable,
    _structure_result_refs,
    actualize_model,
    build_structured_forecast_model,
    build_structure_drivers,
    compute_structure_results,
    default_structure_assumptions,
    forecast_periods,
    revenue_anchor,
    revise_assumptions,
)
from dalton_core.forecast_sensitivity import build_projection, measure_series, recompute
from dalton_core.company_model_forecast import publish_forecast_lines
from dalton_core.company_model_annual_projection import (
    AnnualProjectionError,
    build_annual_projection,
    validate_annual_projection,
    validate_calendar_binding,
)
from dalton_core.company_model_report import render_forecast_model
from dalton_core.model_forecast import (
    ModelForecastAuthority,
    STRUCTURED_DRIVER_FORMULA_HASH,
    STRUCTURED_DRIVER_FORMULA_REF,
)
from dalton_core.fund_xlsx_export import (
    _display_unit, _number_format, export_fund_workbook,
)
from dalton_core.store import DaltonStore, content_hash
from tests.test_company_financial_statement_structure import (
    ACCESSION,
    company_spec,
    financial_inputs,
    proposal,
)


def cells(results):
    return {
        (row["role"], cell["period"]["end"]): cell
        for row in results for cell in row["cells"]
    }


def forecastable_proposal(inputs):
    """Fixture topology with an explicitly derived diluted-EPS numerator."""

    candidate = proposal(inputs)
    for line in candidate["lines"]:
        if line["ref"] in {"interest-income", "interest-expense"}:
            line.update(forecast_method="share_of_line", forecast_base_ref="revenue")
        elif line["ref"] == "tax":
            line.update(forecast_method="share_of_line", forecast_base_ref="pretax")
        elif line["ref"] == "nci":
            line.update(forecast_method="share_of_line", forecast_base_ref="net")
        elif line["ref"] == "eps-numerator":
            line.update(
                kind="derived", concept=None, forecast_method="formula",
                forecast_base_ref=None,
            )
    candidate["formulas"].insert(-1, {
        "output_ref": "eps-numerator", "operator": "sum",
        "terms": [{"line_ref": "parent", "coefficient": "1"}],
        "tie_out_concept": "eps_numerator", "evidence_refs": [ACCESSION],
    })
    return candidate


def annual_authority_inputs():
    inputs = financial_inputs()
    inputs["schema_version"] = "0.3"
    annual = {
        "period_start": "2025-01-01", "period_end": "2025-12-31",
        "period_kind": "cumulative", "source_accessions": [ACCESSION],
        "source_forms": ["10-K"],
    }
    facts = {
        "eps_numerator": {**annual, "value": "670", "unit": "usd"},
        "shares": {**annual, "value": "100", "unit": "shares"},
        "eps": {**annual, "value": "6.70", "unit": "usd_per_share"},
    }
    for line in inputs["filed_lines"]:
        quarter_facts = [
            {"period_start": cell["period_start"], "period_end": end,
             "period_kind": "quarter", "value": cell["value"],
             "unit": cell["unit"], "source_accessions": [ACCESSION],
             "source_forms": ["10-Q"]}
            for end, cell in line["cells"].items()
        ]
        line["duration_facts"] = quarter_facts + (
            [facts[line["concept"]]] if line["concept"] in facts else []
        )
        line["ambiguous_periods"] = []
    return inputs


def annual_forecastable_proposal(inputs):
    candidate = forecastable_proposal(inputs)
    candidate["schema_version"] = STRUCTURE_SCHEMA_VERSION
    for line in candidate["lines"]:
        line["annual_forecast_method"] = None
        if line["role"] == "diluted_weighted_average_shares":
            line["forecast_method"] = "quarterly_growth"
            line["annual_forecast_method"] = "day_weighted_quarters"
    return candidate


class FinancialStructureForecastConsumerTests(unittest.TestCase):
    def authority(self, inputs=None):
        inputs = financial_inputs() if inputs is None else inputs
        candidate = forecastable_proposal(inputs)
        structure, replay = validate_financial_statement_structure(
            candidate, company_spec(), inputs)
        self.assertTrue(replay["ready_for_forecast"])
        return inputs, structure

    def compute(self, structure, inputs):
        drivers = build_structure_drivers(inputs, structure)
        periods = forecast_periods(revenue_anchor(drivers), 2)
        assumptions = default_structure_assumptions(drivers, periods, structure)
        return compute_structure_results(drivers, assumptions, periods, structure)

    def test_company_formula_controls_nonoperating_sign_and_tax_base(self):
        inputs, structure = self.authority()
        result = self.compute(structure, inputs)
        by_cell = cells(result)
        end = sorted(end for role, end in by_cell if role == "pretax_income")[0]
        operating = Decimal(by_cell[("operating_income", end)]["value"])
        interest_income = Decimal(by_cell[("interest_income", end)]["value"])
        interest_expense = Decimal(by_cell[("interest_expense", end)]["value"])
        pretax = Decimal(by_cell[("pretax_income", end)]["value"])
        tax = Decimal(by_cell[("income_tax_expense", end)]["value"])
        self.assertEqual(pretax, operating + interest_income - interest_expense)
        # The tax assumption explicitly names pretax as its company base.
        tax_result = next(row for row in result if row["role"] == "income_tax_expense")
        self.assertEqual(tax_result["cells"][0]["result_refs"], [
            {"ref": "result:pretax_income", "period_end": end},
        ])
        self.assertGreater(tax, 0)

        # A second company's validated DAG may classify the same magnitude as
        # an expense. The consumer follows the coefficient; it does not infer
        # the sign from a label or role.
        expense_structure = copy.deepcopy(structure)
        pretax_formula = next(item for item in expense_structure["formulas"]
                              if item["output_ref"] == "pretax")
        income_term = next(item for item in pretax_formula["terms"]
                           if item["line_ref"] == "interest-income")
        income_term["coefficient"] = "-1"
        expense = self.compute(expense_structure, inputs)
        expense_cell = cells(expense)[("pretax_income", end)]
        self.assertEqual(
            Decimal(expense_cell["value"]),
            operating - interest_income - interest_expense,
        )

    def test_missing_leaf_assumption_propagates_unavailable_without_zero_fill(self):
        inputs = financial_inputs()
        structure, replay = validate_financial_statement_structure(
            proposal(inputs), company_spec(), inputs)
        self.assertTrue(replay["ready_for_forecast"])
        results = self.compute(structure, inputs)
        by_cell = cells(results)
        end = sorted(end for role, end in by_cell if role == "operating_income")[0]
        self.assertEqual(by_cell[("operating_income", end)]["status"], "computed")
        self.assertEqual(by_cell[("interest_income", end)]["status"], "unavailable")
        pretax = by_cell[("pretax_income", end)]
        self.assertEqual(pretax["status"], "unavailable")
        self.assertIsNone(pretax["value"])
        self.assertIn("interest-income", pretax["reason"])
        self.assertEqual(by_cell[("net_income", end)]["status"], "unavailable")
        self.assertEqual(by_cell[("diluted_eps", end)]["status"], "unavailable")

    def test_growth_does_not_jump_a_missing_or_stale_quarter(self):
        inputs, structure = self.authority()
        drivers = build_structure_drivers(inputs, structure)
        periods = forecast_periods(revenue_anchor(drivers), 2)
        assumptions = default_structure_assumptions(drivers, periods, structure)
        revenue = next(item for item in drivers if item["structure_line_ref"] == "revenue")
        assumptions = [item for item in assumptions if not (
            item["driver_ref"] == revenue["ref"]
            and item["period"]["end"] == periods[0]["end"])]
        output = cells(compute_structure_results(
            drivers, assumptions, periods, structure))
        self.assertEqual(output[("revenue", periods[0]["end"])]["status"], "unavailable")
        self.assertIn("adjacent prior quarter",
                      output[("revenue", periods[1]["end"])]["reason"])

        stale_structure = copy.deepcopy(structure)
        next(item for item in stale_structure["lines"]
             if item["ref"] == "interest-income")["forecast_method"] = "quarterly_growth"
        next(item for item in stale_structure["lines"]
             if item["ref"] == "interest-income")["forecast_base_ref"] = None
        stale = build_structure_drivers(inputs, stale_structure)
        interest = next(item for item in stale
                        if item["structure_line_ref"] == "interest-income")
        interest["history"] = interest["history"][:-1]
        stale_assumptions = default_structure_assumptions(
            stale, periods, stale_structure)
        stale_output = cells(compute_structure_results(
            stale, stale_assumptions, periods, stale_structure))
        self.assertEqual(
            stale_output[("interest_income", periods[0]["end"])]["status"],
            "unavailable")
        self.assertIn("adjacent prior quarter",
                      stale_output[("interest_income", periods[0]["end"])]["reason"])

    def test_nonpositive_forecast_shares_make_eps_unavailable(self):
        inputs, structure = self.authority()
        changed = copy.deepcopy(structure)
        shares_line = next(item for item in changed["lines"] if item["ref"] == "shares")
        shares_line["forecast_method"] = "quarterly_growth"
        drivers = build_structure_drivers(inputs, changed)
        shares_driver = next(item for item in drivers
                             if item["structure_line_ref"] == "shares")
        periods = forecast_periods(revenue_anchor(drivers), 1)
        assumptions = default_structure_assumptions(drivers, periods, changed)
        for item in assumptions:
            if item["driver_ref"] == shares_driver["ref"]:
                item["value"] = "-2"
        output = cells(compute_structure_results(drivers, assumptions, periods, changed))
        end = periods[0]["end"]
        self.assertEqual(
            output[("diluted_weighted_average_shares", end)]["status"], "unavailable")
        self.assertIn("not positive",
                      output[("diluted_weighted_average_shares", end)]["reason"])
        self.assertEqual(output[("diluted_eps", end)]["status"], "unavailable")

    def test_v03_model_freezes_structure_replay_and_binding(self):
        inputs, structure = self.authority()
        candidate = forecastable_proposal(inputs)
        structure, replay = validate_financial_statement_structure(
            candidate, company_spec(), inputs)
        binding = forecast_structure_binding(structure, replay, inputs)
        body = build_structured_forecast_model(
            company_spec(), inputs, structure=structure, replay=replay, binding=binding)
        with tempfile.TemporaryDirectory() as temporary:
            store = DaltonStore(str(Path(temporary) / "core.sqlite"))
            self.addCleanup(store.close)
            record = ForecastModelAuthority(store).publish(body)
        self.assertEqual(record["schema_version"], "0.3")
        self.assertEqual(record["forecast_structure_binding"], binding)
        self.assertEqual(record["financial_statement_structure"], structure)

    def test_v03_actualization_replays_exact_dag_and_eps_inputs(self):
        inputs, structure = self.authority()
        candidate = forecastable_proposal(inputs)
        spec = {**company_spec(), "decided_by": "automation:test",
                "financial_statement_structure": {
                    "schema_version": "0.1", "lines": candidate["lines"],
                    "formulas": candidate["formulas"],
                }}
        structure, replay = materialize_financial_statement_structure(spec, inputs)
        binding = forecast_structure_binding(structure, replay, inputs)
        body = build_structured_forecast_model(
            spec, inputs, structure=structure, replay=replay, binding=binding)
        with tempfile.TemporaryDirectory() as temporary:
            store = DaltonStore(str(Path(temporary) / "core.sqlite"))
            self.addCleanup(store.close)
            authority = ForecastModelAuthority(store)
            prior = authority.publish(body)

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
                    "period_start": "2026-01-01",
                    "value": str(actuals[line["concept"]]),
                    "unit": next(iter(line["cells"].values()))["unit"],
                    "basis": "reported", "source_accessions": ["0000000000-26-000002"],
                }
            current_structure, current_replay = materialize_financial_statement_structure(
                spec, current)
            current_binding = forecast_structure_binding(
                current_structure, current_replay, current)
            self.assertNotEqual(current_structure["structure_ref"],
                                structure["structure_ref"])
            updated = actualize_model(
                prior, current, structure=current_structure, replay=current_replay,
                binding=current_binding)
            self.assertIsNotNone(updated)
            record = authority.publish(updated)

        by_cell = cells(record["results"])
        for role, value in (("operating_income", "280.00000000"),
                            ("pretax_income", "270.00000000"),
                            ("parent_net_income", "205.00000000"),
                            ("diluted_eps", "2.05000000")):
            self.assertEqual(by_cell[(role, "2026-03-31")]["value"], value)
            self.assertEqual(by_cell[(role, "2026-03-31")]["kind"], "actual")
        eps = by_cell[("diluted_eps", "2026-03-31")]
        self.assertEqual(eps["result_refs"], [
            {"ref": "result:diluted_eps_numerator", "period_end": "2026-03-31"},
            {"ref": "result:diluted_weighted_average_shares",
             "period_end": "2026-03-31"},
        ])
        next_end = record["forecast_periods"][0]["end"]
        revenue = by_cell[("revenue", next_end)]
        revenue_result = next(
            item for item in record["results"] if item["role"] == "revenue")
        growth = next(
            item for item in record["assumptions"]
            if item["driver_ref"] == revenue_result["driver_ref"]
            and item["period"]["end"] == next_end
            and not item.get("superseded_by")
        )
        self.assertEqual(
            Decimal(revenue["value"]),
            (Decimal("1400") * (Decimal(1) + Decimal(growth["value"]))).quantize(
                Decimal("0.00000001")),
        )

    def test_v03_actualization_refuses_changed_formula_topology(self):
        inputs, structure = self.authority()
        candidate = forecastable_proposal(inputs)
        structure, replay = validate_financial_statement_structure(
            candidate, company_spec(), inputs)
        binding = forecast_structure_binding(structure, replay, inputs)
        body = build_structured_forecast_model(
            company_spec(), inputs, structure=structure, replay=replay, binding=binding)
        with tempfile.TemporaryDirectory() as temporary:
            store = DaltonStore(str(Path(temporary) / "core.sqlite"))
            self.addCleanup(store.close)
            prior = ForecastModelAuthority(store).publish(body)
        changed = copy.deepcopy(structure)
        changed["created_by"] = "automation:other-structure-decision"
        changed_body = dict(changed)
        changed_body.pop("content_hash")
        from dalton_core.store import content_hash
        changed["content_hash"] = content_hash(changed_body)
        changed_replay = replay_historical_structure(changed, inputs)
        changed_binding = forecast_structure_binding(changed, changed_replay, inputs)
        with self.assertRaisesRegex(ForecastModelUnavailable, "structure changed"):
            actualize_model(
                prior, inputs, structure=changed, replay=changed_replay,
                binding=changed_binding)

    def test_v03_sensitivity_recomputes_the_frozen_dag_and_exact_share_base(self):
        inputs, structure = self.authority()
        candidate = forecastable_proposal(inputs)
        structure, replay = validate_financial_statement_structure(
            candidate, company_spec(), inputs)
        binding = forecast_structure_binding(structure, replay, inputs)
        body = build_structured_forecast_model(
            company_spec(), inputs, structure=structure, replay=replay, binding=binding)
        with tempfile.TemporaryDirectory() as temporary:
            store = DaltonStore(str(Path(temporary) / "core.sqlite"))
            self.addCleanup(store.close)
            record = ForecastModelAuthority(store).publish(body)

        tax_driver = next(item for item in record["drivers"]
                          if item["structure_line_ref"] == "tax")
        series = measure_series(record, tax_driver["ref"], "share_of_line")
        self.assertEqual(series["status"], "available")
        self.assertEqual(len(series["points"]), 4)
        # The tax line is divided by the structure's pretax line, not revenue
        # or the legacy operating-income shortcut.
        self.assertEqual(Decimal(series["points"][-1]["value"]), Decimal(55) / 250)

        interest_driver = next(item for item in record["drivers"]
                               if item["structure_line_ref"] == "interest-income")
        base_results, _ = recompute(record, None, None)
        moved_results, replaced = recompute(record, interest_driver["ref"], Decimal("0.02"))
        first = record["forecast_periods"][0]["end"]
        base_pretax = Decimal(cells(base_results)[("pretax_income", first)]["value"])
        moved_pretax = Decimal(cells(moved_results)[("pretax_income", first)]["value"])
        self.assertGreater(moved_pretax, base_pretax)
        self.assertEqual(len(replaced), len(record["forecast_periods"]))
        projection = build_projection(record)
        self.assertEqual(projection["formula_hash"], record["formula_hash"])
        self.assertGreaterEqual(len(projection["drivers"]), 3)

    def test_v03_revision_recomputes_same_structure_and_preserves_authority(self):
        inputs, structure = self.authority()
        candidate = forecastable_proposal(inputs)
        structure, replay = validate_financial_statement_structure(
            candidate, company_spec(), inputs)
        binding = forecast_structure_binding(structure, replay, inputs)
        body = build_structured_forecast_model(
            company_spec(), inputs, structure=structure, replay=replay, binding=binding)
        with tempfile.TemporaryDirectory() as temporary:
            store = DaltonStore(str(Path(temporary) / "core.sqlite"))
            self.addCleanup(store.close)
            authority = ForecastModelAuthority(store)
            prior = authority.publish(body)
            tax_driver = next(item for item in prior["drivers"]
                              if item["structure_line_ref"] == "tax")
            end = prior["forecast_periods"][0]["end"]
            revised = revise_assumptions(
                prior, [{"driver": tax_driver["ref"], "period": end,
                         "value": "0.219", "because": "new filed tax guidance",
                         "refs": [{"kind": "filing", "ref": None, "concept": "tax",
                                   "period_end": "2025-12-31",
                                   "accession": ACCESSION}]}],
                change_reason="assumption_review",
                evidence_refs=[{"kind": "filing", "ref": None, "concept": "tax",
                                "period_end": "2025-12-31", "accession": ACCESSION}],
                actor_ref="automation:test")
            record = authority.publish(revised)
        before = Decimal(cells(prior["results"])[("net_income", end)]["value"])
        after = Decimal(cells(record["results"])[("net_income", end)]["value"])
        self.assertLess(after, before)
        self.assertEqual(record["financial_statement_structure"], structure)
        self.assertEqual(record["formula_hash"], prior["formula_hash"])

    def test_v03_published_line_keeps_exact_structure_formula_authority(self):
        inputs, structure = self.authority()
        candidate = forecastable_proposal(inputs)
        structure, replay = validate_financial_statement_structure(
            candidate, company_spec(), inputs)
        binding = forecast_structure_binding(structure, replay, inputs)
        body = build_structured_forecast_model(
            company_spec(), inputs, structure=structure, replay=replay, binding=binding)
        with tempfile.TemporaryDirectory() as temporary:
            store = DaltonStore(str(Path(temporary) / "core.sqlite"))
            self.addCleanup(store.close)
            record = ForecastModelAuthority(store).publish(body)
            lines = ModelForecastAuthority(store)
            published = publish_forecast_lines(lines, record)
            held = lines.line(published[0]["version_ref"])
        self.assertEqual(held["formula_ref"], STRUCTURED_DRIVER_FORMULA_REF)
        self.assertEqual(held["formula_hash"], STRUCTURED_DRIVER_FORMULA_HASH)
        self.assertEqual(held["scenario_version_ref"], record["id"])
        self.assertEqual(held["scenario_version_hash"], record["content_hash"])

    def test_v03_workbook_translates_company_dag_and_refuses_eps_annual_sum(self):
        from openpyxl import load_workbook

        inputs, structure = self.authority()
        candidate = forecastable_proposal(inputs)
        structure, replay = validate_financial_statement_structure(
            candidate, company_spec(), inputs)
        binding = forecast_structure_binding(structure, replay, inputs)
        body = build_structured_forecast_model(
            company_spec(), inputs, structure=structure, replay=replay, binding=binding)
        with tempfile.TemporaryDirectory() as temporary:
            store = DaltonStore(str(Path(temporary) / "core.sqlite"))
            self.addCleanup(store.close)
            record = ForecastModelAuthority(store).publish(body)
            path = Path(temporary) / "structured.xlsx"
            calendar = {
                "calendar_ref": "calendar:test", "source_hash": "c" * 64,
                "as_of": "2025-12-31", "fiscal_year_end_month": 12,
            }
            calendar["content_hash"] = content_hash(calendar)
            export_fund_workbook(
                path, model=record, spec=company_spec(), inputs=inputs,
                calendar_binding=calendar)
            book = load_workbook(path, data_only=False)
        financials = book["Financials"]
        pretax_row = next(
            row for row in range(3, financials.max_row + 1)
            if any(str(financials.cell(row, column).value).startswith("Pretax")
                   for column in range(1, 5))
        )
        formulas = [financials.cell(pretax_row, column).value
                    for column in range(2, financials.max_column + 1)]
        self.assertTrue(any(isinstance(value, str) and "+" in value and "-" in value
                            for value in formulas), formulas)
        eps_row = 3 + next(index for index, result in enumerate(record["results"])
                           if result["role"] == "diluted_eps")
        # No annual EPS is made by summing per-share quarters.
        self.assertIsNone(financials.cell(eps_row, 5).value)
        self.assertIn(",,", _number_format("usd"))
        self.assertEqual(_display_unit("usd"), "USD millions")
        self.assertEqual(_display_unit("eur_per_share"), "EUR per share")

    def test_v03_workbook_uses_exact_direct_annual_eps_authority(self):
        from openpyxl import load_workbook

        inputs = annual_authority_inputs()
        inputs, structure = self.authority(inputs)
        candidate = forecastable_proposal(inputs)
        structure, replay = validate_financial_statement_structure(
            candidate, company_spec(), inputs)
        binding = forecast_structure_binding(structure, replay, inputs)
        body = build_structured_forecast_model(
            company_spec(), inputs, structure=structure, replay=replay, binding=binding)
        with tempfile.TemporaryDirectory() as temporary:
            store = DaltonStore(str(Path(temporary) / "core.sqlite"))
            self.addCleanup(store.close)
            record = ForecastModelAuthority(store).publish(body)
            path = Path(temporary) / "annual-eps.xlsx"
            calendar = {
                "calendar_ref": "calendar:test", "source_hash": "c" * 64,
                "as_of": "2025-12-31", "fiscal_year_end_month": 12,
            }
            calendar["content_hash"] = content_hash(calendar)
            export_fund_workbook(
                path, model=record, spec=company_spec(), inputs=inputs,
                calendar_binding=calendar)
            book = load_workbook(path, data_only=False)
        financials = book["Financials"]
        rows = {result["role"]: 3 + index
                for index, result in enumerate(record["results"])}
        self.assertTrue(
            financials.cell(
                rows["diluted_eps_numerator"], 5).value.startswith("=SUM("))
        self.assertEqual(
            financials.cell(
                rows["diluted_weighted_average_shares"], 5).value, 0.0001)
        self.assertEqual(
            financials.cell(rows["diluted_eps"], 5).value, 6.7,
        )
        self.assertTrue(any(
            "shares millions" in str(financials.cell(
                rows["diluted_weighted_average_shares"], column).value)
            for column in range(1, 5)
        ))
        self.assertTrue(any(
            str(book["Formula Map"].cell(row, 2).value).startswith(
                "annual-projection:")
            and "result:diluted_eps" in str(
                book["Formula Map"].cell(row, 2).value)
            for row in range(5, book["Formula Map"].max_row + 1)
        ))

    def test_v03_workbook_forecasts_annual_shares_only_with_bound_method(self):
        from openpyxl import load_workbook

        inputs = annual_authority_inputs()
        candidate = annual_forecastable_proposal(inputs)
        structure, replay = validate_financial_statement_structure(
            candidate, company_spec(), inputs)
        self.assertTrue(replay["ready_for_forecast"])
        binding = forecast_structure_binding(structure, replay, inputs)
        body = build_structured_forecast_model(
            company_spec(), inputs, structure=structure, replay=replay, binding=binding)
        with tempfile.TemporaryDirectory() as temporary:
            store = DaltonStore(str(Path(temporary) / "core.sqlite"))
            self.addCleanup(store.close)
            record = ForecastModelAuthority(store).publish(body)
            path = Path(temporary) / "forecast-annual-eps.xlsx"
            calendar = {
                "calendar_ref": "calendar:test", "source_hash": "c" * 64,
                "as_of": "2025-12-31", "fiscal_year_end_month": 12,
            }
            calendar["content_hash"] = content_hash(calendar)
            export_fund_workbook(
                path, model=record, spec=company_spec(), inputs=inputs,
                calendar_binding=calendar)
            book = load_workbook(path, data_only=False)
        financials = book["Financials"]
        rows = {result["role"]: 3 + index
                for index, result in enumerate(record["results"])}
        share_formula = financials.cell(
            rows["diluted_weighted_average_shares"], 6).value
        self.assertIsInstance(share_formula, str)
        self.assertIn("*90", share_formula)
        self.assertIn("*92", share_formula)
        self.assertEqual(
            financials.cell(rows["diluted_eps"], 6).value,
            f"='Financials'!F{rows['diluted_eps_numerator']}/"
            f"'Financials'!F{rows['diluted_weighted_average_shares']}",
        )

        no_authority_candidate = annual_forecastable_proposal(inputs)
        next(
            line for line in no_authority_candidate["lines"]
            if line["role"] == "diluted_weighted_average_shares"
        )["annual_forecast_method"] = "unavailable"
        no_authority_structure, no_authority_replay = (
            validate_financial_statement_structure(
                no_authority_candidate, company_spec(), inputs)
        )
        no_authority_binding = forecast_structure_binding(
            no_authority_structure, no_authority_replay, inputs)
        no_authority_body = build_structured_forecast_model(
            company_spec(), inputs, structure=no_authority_structure,
            replay=no_authority_replay, binding=no_authority_binding)
        with tempfile.TemporaryDirectory() as temporary:
            store = DaltonStore(str(Path(temporary) / "core.sqlite"))
            self.addCleanup(store.close)
            no_authority_record = ForecastModelAuthority(store).publish(no_authority_body)
            path = Path(temporary) / "unavailable.xlsx"
            export_fund_workbook(
                path, model=no_authority_record, spec=company_spec(), inputs=inputs,
                calendar_binding=calendar)
            unavailable = load_workbook(path, data_only=False)["Financials"]
        unavailable_rows = {
            result["role"]: 3 + index
            for index, result in enumerate(no_authority_record["results"])
        }
        self.assertIsNone(unavailable.cell(
            unavailable_rows["diluted_weighted_average_shares"], 6).value)
        self.assertIsNone(unavailable.cell(unavailable_rows["diluted_eps"], 6).value)

    def test_annual_projection_is_shared_by_report_and_export(self):
        inputs = annual_authority_inputs()
        candidate = annual_forecastable_proposal(inputs)
        structure, replay = validate_financial_statement_structure(
            candidate, company_spec(), inputs)
        binding = forecast_structure_binding(structure, replay, inputs)
        body = build_structured_forecast_model(
            company_spec(), inputs, structure=structure, replay=replay, binding=binding)
        calendar = {
            "calendar_ref": "calendar:test", "source_hash": "c" * 64,
            "as_of": "2025-12-31", "fiscal_year_end_month": 12,
        }
        calendar["content_hash"] = content_hash(calendar)
        with tempfile.TemporaryDirectory() as temporary:
            store = DaltonStore(str(Path(temporary) / "core.sqlite"))
            self.addCleanup(store.close)
            authority = ForecastModelAuthority(store)
            record = authority.publish(body)
            projection = build_annual_projection(
                model=record, inputs=inputs, calendar_binding=calendar)
            held = projection
            exported = export_fund_workbook(
                Path(temporary) / "projection.xlsx", model=record,
                spec=company_spec(), inputs=inputs,
                calendar_binding=calendar, annual_projection=held)

        self.assertEqual(exported["annual_projection_hash"], held["content_hash"])
        rows = {item["label"]: item for item in held["periods"]}
        self.assertEqual(rows["FY2025A"]["historical_eps"]["value"], "6.7")
        self.assertEqual(rows["FY2026E"]["forecast_shares"]["value"], "100.00000000")
        self.assertEqual(rows["FY2026E"]["forecast_eps"]["value"], "9.4678918595")
        revenue = rows["FY2026E"]["line_outcomes"]["result:revenue"]
        self.assertEqual(revenue["status"], "computed")
        self.assertEqual(len(revenue["source_periods"]), 4)
        self.assertTrue(all(item.get("model_cell_ref")
                            for item in revenue["source_periods"]))
        rendered = render_forecast_model(record, annual_projection=held)
        self.assertIn("ANNUAL STRUCTURED FINANCIALS", rendered)
        self.assertIn("ANNUAL DILUTED EPS", rendered)
        self.assertIn("FY2025A", rendered)
        self.assertIn(held["content_hash"], rendered)

        tampered = copy.deepcopy(held)
        tampered["periods"][1]["forecast_eps"]["value"] = "99"
        with self.assertRaisesRegex(AnnualProjectionError, "differs"):
            validate_annual_projection(
                tampered, model=record, inputs=inputs,
                calendar_binding=calendar)
        identity_tampered = copy.deepcopy(held)
        identity_tampered["projection_ref"] = "annual-projection:forged"
        identity_tampered["content_hash"] = content_hash({
            key: value for key, value in identity_tampered.items()
            if key != "content_hash"
        })
        from dalton_core.company_model_annual_projection import validate_projection_record
        with self.assertRaisesRegex(AnnualProjectionError, "authority differs"):
            validate_projection_record(identity_tampered, model=record)

    def test_annual_projection_combines_filed_and_forecast_quarters(self):
        inputs = annual_authority_inputs()
        candidate = annual_forecastable_proposal(inputs)
        spec = {**company_spec(), "decided_by": "automation:test",
                "financial_statement_structure": candidate}
        structure, replay = materialize_financial_statement_structure(spec, inputs)
        binding = forecast_structure_binding(structure, replay, inputs)
        body = build_structured_forecast_model(
            spec, inputs, structure=structure, replay=replay, binding=binding)
        with tempfile.TemporaryDirectory() as temporary:
            store = DaltonStore(str(Path(temporary) / "core.sqlite"))
            self.addCleanup(store.close)
            authority = ForecastModelAuthority(store)
            prior = authority.publish(body)
            current = copy.deepcopy(inputs)
            current["periods"].append("2026-03-31")
            actuals = {
                "revenue": 1400, "cost": 840, "opex": 280, "operating": 280,
                "interest_income": 10, "interest_expense": 20, "pretax": 270,
                "tax": 60, "net": 210, "nci": 5, "parent": 205,
                "eps_numerator": 205, "shares": 101, "eps": "2.02970297",
            }
            for line in current["filed_lines"]:
                line["cells"]["2026-03-31"] = {
                    "period_start": "2026-01-01",
                    "value": str(actuals[line["concept"]]),
                    "unit": next(iter(line["cells"].values()))["unit"],
                    "basis": "reported", "source_accessions": [
                        "0000000000-26-000002"],
                }
            current_structure, current_replay = materialize_financial_statement_structure(
                spec, current)
            current_binding = forecast_structure_binding(
                current_structure, current_replay, current)
            body = actualize_model(
                prior, current, structure=current_structure,
                replay=current_replay, binding=current_binding)
            self.assertIsNotNone(body)
            record = authority.publish(body)
        calendar = {
            "calendar_ref": "calendar:test", "source_hash": "c" * 64,
            "as_of": "2025-12-31", "fiscal_year_end_month": 12,
        }
        calendar["content_hash"] = content_hash(calendar)
        projection = build_annual_projection(
            model=record, inputs=current, calendar_binding=calendar)
        mixed = next(item for item in projection["periods"]
                     if item["label"] == "FY2026A/E")
        self.assertEqual(mixed["kind"], "mixed")
        self.assertEqual(mixed["forecast_eps"]["status"], "computed")
        source_periods = mixed["forecast_eps"]["numerator"]["source_periods"]
        self.assertEqual(len(source_periods), 4)
        self.assertIn("input_cell_refs", source_periods[0])
        self.assertTrue(all("model_cell_ref" in item for item in source_periods[1:]))
        annual_revenue = mixed["line_outcomes"]["result:revenue"]
        self.assertEqual(annual_revenue["status"], "computed")
        self.assertIn("input_cell_refs", annual_revenue["source_periods"][0])
        self.assertTrue(all("model_cell_ref" in item
                            for item in annual_revenue["source_periods"][1:]))
        with tempfile.TemporaryDirectory() as exported_dir:
            path = Path(exported_dir) / "mixed.xlsx"
            exported = export_fund_workbook(
                path, model=record, spec=spec,
                inputs=current, calendar_binding=calendar,
                annual_projection=projection,
            )
            from openpyxl import load_workbook
            from openpyxl.utils import get_column_letter
            book = load_workbook(path, data_only=False)
            driver = book["Driver"]
        self.assertEqual(exported["annual_projection_hash"], projection["content_hash"])
        # The immutable superseded estimate for the newly actual quarter stays
        # in model authority, but must not become a duplicate spreadsheet row.
        revenue_rows = [
            row for row in range(1, driver.max_row + 1)
            if driver.cell(row, 3).value ==
            "Revenue — Quarterly growth (ratio)"
        ]
        self.assertEqual(len(revenue_rows), 1)
        actual_column = next(
            column for column in range(1, driver.max_column + 1)
            if driver.cell(1, column).value == "1Q26"
        )
        forecast_column = next(
            column for column in range(1, driver.max_column + 1)
            if driver.cell(1, column).value == "2Q26E"
        )
        cost_ratio_row = next(
            row for row in range(1, driver.max_row + 1)
            if driver.cell(row, 3).value == "Cost — Share of line (ratio)"
        )
        refs_by_line = _structure_result_refs(
            record["financial_statement_structure"])
        result_rows = {
            result["ref"]: 3 + index
            for index, result in enumerate(record["results"])
        }
        column_letter = get_column_letter(actual_column)
        self.assertEqual(
            driver.cell(cost_ratio_row, actual_column).value,
            f"='Financials'!{column_letter}{result_rows[refs_by_line['cost']]}/"
            f"'Financials'!{column_letter}{result_rows[refs_by_line['revenue']]}",
        )
        self.assertEqual(
            driver.cell(cost_ratio_row, actual_column).font.color.rgb[-6:],
            "008000",
        )
        self.assertIsInstance(
            driver.cell(cost_ratio_row, forecast_column).value, (int, float),
        )
        self.assertEqual(
            driver.cell(cost_ratio_row, forecast_column).font.color.rgb[-6:],
            "0000FF",
        )

    def test_annual_calendar_requires_iso_date_and_matching_fiscal_month(self):
        for as_of, month in (("not-a-date", 12), ("2025-11-30", 12)):
            with self.subTest(as_of=as_of, month=month):
                calendar = {
                    "calendar_ref": "calendar:test", "source_hash": "c" * 64,
                    "as_of": as_of, "fiscal_year_end_month": month,
                }
                calendar["content_hash"] = content_hash(calendar)
                with self.assertRaises(AnnualProjectionError):
                    validate_calendar_binding(calendar)

if __name__ == "__main__":
    unittest.main()
