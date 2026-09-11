from __future__ import annotations

import copy
from decimal import Decimal
from pathlib import Path
import tempfile
import unittest

from dalton_core.company_financial_statement_structure import (
    forecast_structure_binding,
    validate_financial_statement_structure,
)
from dalton_core.model_forecast_driver import (
    ForecastModelAuthority,
    build_structured_forecast_model,
    build_structure_drivers,
    compute_structure_results,
    default_structure_assumptions,
    forecast_periods,
    revenue_anchor,
)
from dalton_core.store import DaltonStore
from tests.test_company_financial_statement_structure import (
    company_spec,
    financial_inputs,
    proposal,
)


def cells(results):
    return {
        (row["role"], cell["period"]["end"]): cell
        for row in results for cell in row["cells"]
    }


class FinancialStructureForecastConsumerTests(unittest.TestCase):
    def authority(self):
        inputs = financial_inputs()
        candidate = proposal(inputs)
        for line in candidate["lines"]:
            if line["ref"] in {"interest-income", "interest-expense"}:
                line.update(forecast_method="share_of_line", forecast_base_ref="revenue")
            elif line["ref"] == "tax":
                line.update(forecast_method="share_of_line", forecast_base_ref="pretax")
            elif line["ref"] == "nci":
                line.update(forecast_method="share_of_line", forecast_base_ref="net")
            elif line["ref"] == "eps-numerator":
                line.update(forecast_method="share_of_line", forecast_base_ref="parent")
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

    def test_v03_model_freezes_structure_replay_and_binding(self):
        inputs, structure = self.authority()
        # Recreate the public replay from the exact proposal authority.
        candidate = proposal(inputs)
        for line in candidate["lines"]:
            original = next(item for item in structure["lines"] if item["ref"] == line["ref"])
            line["forecast_method"] = original["forecast_method"]
            line["forecast_base_ref"] = original["forecast_base_ref"]
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


if __name__ == "__main__":
    unittest.main()
