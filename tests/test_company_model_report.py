"""P13ao: the view a person reads to judge whether the model is right.

Two properties matter and both are about not flattering the work: the filed
history and the model rows stay separate, because several model rows have no
history of their own and interleaving them would let a reader skim past that;
and rows needing estimates are named rather than counted, because "seven rows
need estimates" reads like progress and a list of names reads like the work.
"""

from __future__ import annotations

import unittest

from dalton_core.company_model_report import render_model_inputs
from tests.test_company_model_inputs import FakeMissions, _line, _spec
from dalton_core.company_model_inputs import build_model_inputs


class RenderTests(unittest.TestCase):
    def table(self, ledger=None, spec=None):
        ledger = ledger or FakeMissions([
            _line("us-gaap:Revenues", "2026-03-01", "2026-05-31", "18718144000"),
            _line("us-gaap:CostOfGoodsAndServicesSold",
                  "2026-03-01", "2026-05-31", "12000000000"),
        ])
        return build_model_inputs(ledger, spec or _spec())

    def test_figures_print_in_millions_without_touching_the_value(self):
        table = self.table()
        text = render_model_inputs(table, entity_name="Accenture plc")
        self.assertIn("Accenture plc", text)
        self.assertIn("18,718.1", text)
        # The table itself still carries what was filed, to the unit.
        line = next(item for item in table["filed_lines"]
                    if item["concept"] == "us-gaap:Revenues")
        self.assertEqual(line["cells"]["2026-05-31"]["value"], "18718144000")

    def test_a_derived_figure_is_marked_where_it_is_shown(self):
        ledger = FakeMissions([
            _line("us-gaap:Revenues", "2026-01-01", "2026-03-31", "100000000"),
            _line("us-gaap:Revenues", "2026-01-01", "2026-06-30", "250000000"),
        ])
        text = render_model_inputs(self.table(ledger, _spec(expenses=[])))
        self.assertIn("150.0*", text)
        self.assertIn("derived from cumulative", text)

    def test_a_split_line_is_marked_and_the_split_is_listed_by_name(self):
        spec = _spec(expenses=[
            {"ref": "delivery-staff", "label": "Delivery staff",
             "basis_concept": "us-gaap:CostOfGoodsAndServicesSold",
             "behaviour": "variable_with_headcount", "driver_ref": None,
             "because": "Payroll follows the billable base."},
            {"ref": "subcontractors", "label": "Subcontractors",
             "basis_concept": "us-gaap:CostOfGoodsAndServicesSold",
             "behaviour": "variable_with_revenue", "driver_ref": None,
             "because": "Bought-in delivery flexes."},
        ])
        text = render_model_inputs(self.table(spec=spec))
        self.assertIn("[split]", text)
        self.assertIn("delivery-staff, subcontractors", text)

    def test_rows_needing_estimates_are_named_not_counted(self):
        spec = _spec(drivers=[{
            "ref": "billable-capacity", "label": "Billable capacity",
            "kind": "volume", "basis_concept": None, "unit": "headcount",
            "because": "Capacity binds delivery revenue.",
        }])
        text = render_model_inputs(self.table(spec=spec))
        self.assertIn("billable-capacity", text)
        self.assertIn("estimated -- no filed counterpart", text)

    def test_the_filed_history_and_the_model_rows_are_separate_blocks(self):
        text = render_model_inputs(self.table())
        self.assertLess(text.index("FILED HISTORY"), text.index("MODEL ROWS"))
        self.assertIn("WHAT IS STILL MISSING", text)

    def test_a_period_a_line_does_not_reach_is_blank_not_zero(self):
        ledger = FakeMissions([
            _line("us-gaap:Revenues", "2026-03-01", "2026-05-31", "18718144000"),
            _line("us-gaap:Revenues", "2025-12-01", "2026-02-28", "18044066000"),
            _line("us-gaap:CostOfGoodsAndServicesSold",
                  "2026-03-01", "2026-05-31", "12000000000"),
        ])
        text = render_model_inputs(self.table(ledger))
        # The cost line has nothing for the earlier quarter, and a missing
        # figure is not a zero -- read by column, because "12,000.0" contains
        # a "0.0" and a substring check would pass for the wrong reason.
        cost_row = next(line for line in text.split("\n")
                        if line.startswith("Cost"))
        cells = [cost_row[46:][index:index + 12].strip()
                 for index in range(0, 24, 12)]
        self.assertEqual(cells, ["--", "12,000.0"])

    def test_a_table_with_nothing_outstanding_says_nothing(self):
        text = render_model_inputs(self.table(spec=_spec(
            drivers=[{"ref": "revenue", "label": "Revenue", "kind": "mix",
                      "basis_concept": "us-gaap:Revenues", "unit": "USD",
                      "because": "It is the top line."}],
            expenses=[{"ref": "cost", "label": "Cost",
                       "basis_concept": "us-gaap:CostOfGoodsAndServicesSold",
                       "behaviour": "variable_with_revenue", "driver_ref": None,
                       "because": "It follows revenue."}])))
        self.assertIn("  nothing", text.split("WHAT IS STILL MISSING")[1])


if __name__ == "__main__":
    unittest.main()
