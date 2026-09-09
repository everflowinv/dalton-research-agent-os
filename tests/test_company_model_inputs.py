"""P13ao: the specification joined to the filings, without inventing a split.

The join is not one to one and that is the whole difficulty. IBM's
specification splits ``us-gaap:CostOfRevenue`` into six lines that behave
differently while the filing reports one figure called "Cost". Handing the same
series back six times would look like data and be an assertion nobody made, so
a filed line several rows draw on is reported as a constraint: here is the
total, here are the rows that must add up to it, and none of them has a value.
"""

from __future__ import annotations

import unittest

from dalton_core.company_model_inputs import (
    AMBIGUOUS,
    ESTIMATED,
    FILED,
    NOT_FOUND,
    SHARED,
    ModelInputError,
    build_model_inputs,
)

ACN = "company:sec-cik:0001467373"


class FakeMissions:
    """A statements ledger of exactly the lines a test cares about."""

    def __init__(self, lines=None):
        self.lines = list(lines or [])

    def statement_series_lines(self, company_ref, concept, statement=None):
        return [row for row in self.lines if row["concept"] == concept]


def _line(concept, start, end, value, *, statement="income",
          filed="2026-07-01", accession="0001467373-26-000032"):
    return {
        "concept": concept, "statement": statement, "label": concept.split(":")[-1],
        "period_start": start, "period_end": end, "value": value, "unit": "USD",
        "is_breakdown": False, "dimension_axis": None,
        "filed": filed, "accession": accession,
    }


def _spec(*, drivers=None, expenses=None, metrics=None, quarters=12):
    return {
        "spec_id": "company-model-spec:test", "company_ref": ACN,
        "state_hash": "a" * 64,
        "revenue_drivers": drivers if drivers is not None else [{
            "ref": "service-mix", "label": "Service mix", "kind": "mix",
            "basis_concept": "us-gaap:Revenues", "unit": "USD",
            "because": "The blend moves the total.",
        }],
        "expense_lines": expenses if expenses is not None else [{
            "ref": "delivery", "label": "Cost of services",
            "basis_concept": "us-gaap:CostOfGoodsAndServicesSold",
            "behaviour": "variable_with_revenue", "driver_ref": None,
            "because": "Delivery cost follows revenue.",
        }],
        "operating_metrics": metrics if metrics is not None else [],
        "horizon": {"historical_quarters": quarters, "forecast_quarters": 8,
                    "because": "The cycle."},
    }


class ModelInputTests(unittest.TestCase):
    def ledger(self):
        return FakeMissions([
            _line("us-gaap:Revenues", "2026-03-01", "2026-05-31", "18718144000"),
            _line("us-gaap:Revenues", "2025-12-01", "2026-02-28", "18044066000"),
            _line("us-gaap:CostOfGoodsAndServicesSold",
                  "2026-03-01", "2026-05-31", "12000000000"),
        ])

    def test_a_row_with_its_own_filed_line_carries_the_history(self):
        table = build_model_inputs(self.ledger(), _spec())
        self.assertEqual(table["periods"], ["2026-02-28", "2026-05-31"])
        row = next(item for item in table["rows"] if item["ref"] == "service-mix")
        self.assertEqual(row["status"], FILED)
        self.assertEqual(row["statement"], "income")
        line = next(item for item in table["filed_lines"]
                    if item["concept"] == "us-gaap:Revenues")
        self.assertEqual(line["cells"]["2026-05-31"]["value"], "18718144000")
        self.assertEqual(line["cells"]["2026-05-31"]["basis"], "reported")
        self.assertEqual(line["cells"]["2026-05-31"]["source_accessions"],
                         ["0001467373-26-000032"])
        self.assertFalse(line["is_split"])

    def test_several_rows_on_one_filed_line_are_a_constraint_not_four_copies(self):
        spec = _spec(expenses=[
            {"ref": "delivery-staff", "label": "Delivery staff",
             "basis_concept": "us-gaap:CostOfGoodsAndServicesSold",
             "behaviour": "variable_with_headcount", "driver_ref": None,
             "because": "Payroll follows the billable base."},
            {"ref": "subcontractors", "label": "Subcontractors",
             "basis_concept": "us-gaap:CostOfGoodsAndServicesSold",
             "behaviour": "variable_with_revenue", "driver_ref": None,
             "because": "Bought-in delivery flexes with volume."},
        ])
        table = build_model_inputs(self.ledger(), spec)
        for ref in ("delivery-staff", "subcontractors"):
            row = next(item for item in table["rows"] if item["ref"] == ref)
            self.assertEqual(row["status"], SHARED)
            self.assertIn("the split is not filed", row["reason"])
        line = next(item for item in table["filed_lines"]
                    if item["concept"] == "us-gaap:CostOfGoodsAndServicesSold")
        self.assertTrue(line["is_split"])
        self.assertEqual(line["drawn_on_by"], ["delivery-staff", "subcontractors"])
        # The total is still there once, because the total is what is known.
        self.assertEqual(line["cells"]["2026-05-31"]["value"], "12000000000")
        self.assertEqual(
            table["readiness"]["filed_lines_needing_a_split"][0]["into"],
            ["delivery-staff", "subcontractors"])

    def test_a_row_with_no_filed_counterpart_says_so_rather_than_going_blank(self):
        spec = _spec(drivers=[{
            "ref": "billable-headcount", "label": "Billable headcount",
            "kind": "volume", "basis_concept": None, "unit": "headcount",
            "because": "Capacity binds delivery revenue.",
        }])
        table = build_model_inputs(self.ledger(), spec)
        row = next(item for item in table["rows"] if item["ref"] == "billable-headcount")
        self.assertEqual(row["status"], ESTIMATED)
        self.assertIn("no filed counterpart", row["reason"])
        self.assertIn("billable-headcount",
                      table["readiness"]["rows_with_no_filed_history"])

    def test_a_concept_the_filings_do_not_have_is_reported_not_silently_empty(self):
        spec = _spec(drivers=[{
            "ref": "phantom", "label": "Phantom", "kind": "volume",
            "basis_concept": "us-gaap:NotFiledHere", "unit": "USD",
            "because": "It was in the projection and is not in the ledger.",
        }])
        table = build_model_inputs(self.ledger(), spec)
        row = next(item for item in table["rows"] if item["ref"] == "phantom")
        self.assertEqual(row["status"], NOT_FOUND)
        self.assertIn("phantom", table["readiness"]["rows_with_no_filed_history"])

    def test_a_concept_in_two_statements_is_flagged_not_preferred(self):
        ledger = FakeMissions([
            _line("us-gaap:NetIncomeLoss", "2026-03-01", "2026-05-31", "1",
                  statement="income"),
            _line("us-gaap:NetIncomeLoss", "2026-03-01", "2026-05-31", "1",
                  statement="cash"),
        ])
        spec = _spec(drivers=[{
            "ref": "net-income", "label": "Net income", "kind": "external",
            "basis_concept": "us-gaap:NetIncomeLoss", "unit": "USD",
            "because": "It appears twice and this must not be resolved by guessing.",
        }], expenses=[])
        table = build_model_inputs(ledger, spec)
        row = next(item for item in table["rows"] if item["ref"] == "net-income")
        self.assertEqual(row["status"], AMBIGUOUS)

    def test_a_balance_sheet_line_is_kept_as_instants_not_dropped(self):
        # Live: IBM's financing receivables came back as an empty row, because
        # columns were taken from durations alone and a balance has none.
        ledger = FakeMissions([
            _line("ibm:FinancingReceivables", None, "2026-06-30", "12000000000",
                  statement="balance"),
            _line("ibm:FinancingReceivables", None, "2026-03-31", "11500000000",
                  statement="balance"),
        ])
        spec = _spec(drivers=[{
            "ref": "financing-book", "label": "Financing book", "kind": "volume",
            "basis_concept": "ibm:FinancingReceivables", "unit": "USD",
            "because": "Financing income comes off the earning portfolio.",
        }], expenses=[])
        table = build_model_inputs(ledger, spec)
        line = table["filed_lines"][0]
        self.assertEqual(line["period_basis"], "instant")
        self.assertEqual(len(line["cells"]), 2)
        self.assertEqual(line["cells"]["2026-06-30"]["value"], "12000000000")
        self.assertIsNone(line["cells"]["2026-06-30"]["period_start"])
        self.assertEqual(table["readiness"]["filed_lines_with_no_values"], [])

    def test_a_filed_line_is_labelled_from_a_total_not_from_a_breakdown(self):
        # Live: Accenture's total revenue printed as "EMEA", because the label
        # was taken from the last row of any kind and the last row was a
        # geography. The figures were the totals and the name was not, which is
        # worse than no label -- a reader has no reason to doubt it.
        ledger = FakeMissions([
            _line("us-gaap:Revenues", "2026-03-01", "2026-05-31", "18718144000"),
            {**_line("us-gaap:Revenues", "2026-03-01", "2026-05-31", "4000000000"),
             "label": "EMEA", "is_breakdown": True,
             "dimension_axis": "srt:StatementGeographicalAxis"},
        ])
        ledger.lines[0]["label"] = "Revenues"
        table = build_model_inputs(ledger, _spec(expenses=[]))
        line = table["filed_lines"][0]
        self.assertEqual(line["label"], "Revenues")
        self.assertEqual(line["cells"]["2026-05-31"]["value"], "18718144000")

    def test_operating_metrics_are_never_pretended_to_be_in_the_statements(self):
        spec = _spec(metrics=[
            {"ref": "new-bookings", "label": "New bookings", "unit": "USD",
             "periodicity": "quarterly", "disclosed": True,
             "because": "The market trades this print."},
            {"ref": "utilisation", "label": "Utilisation", "unit": "percent",
             "periodicity": "quarterly", "disclosed": False,
             "because": "Margin turns on it."},
        ])
        table = build_model_inputs(self.ledger(), spec)
        statuses = {item["ref"]: item for item in table["operating_metrics"]}
        self.assertEqual(statuses["new-bookings"]["status"], ESTIMATED)
        self.assertIn("not in the financial statements",
                      statuses["new-bookings"]["reason"])
        self.assertIn("does not report", statuses["utilisation"]["reason"])

    def test_the_horizon_bounds_the_columns(self):
        ledger = FakeMissions([
            _line("us-gaap:Revenues", "2026-03-01", "2026-05-31", "3"),
            _line("us-gaap:Revenues", "2025-12-01", "2026-02-28", "2"),
            _line("us-gaap:Revenues", "2025-09-01", "2025-11-30", "1"),
        ])
        table = build_model_inputs(ledger, _spec(quarters=2, expenses=[]))
        self.assertEqual(table["periods"], ["2026-02-28", "2026-05-31"])
        line = table["filed_lines"][0]
        self.assertEqual(sorted(line["cells"]), ["2026-02-28", "2026-05-31"])

    def test_a_table_bound_is_enforced_over_the_specification(self):
        quarters = [("01-01", "03-31"), ("04-01", "06-30"),
                    ("07-01", "09-30"), ("10-01", "12-31")]
        ledger = FakeMissions([
            _line("us-gaap:Revenues", f"{year}-{start}", f"{year}-{end}",
                  str(year))
            for year in (2024, 2025, 2026) for start, end in quarters
        ])
        # The specification may ask for twenty; one table stays readable.
        table = build_model_inputs(ledger, _spec(quarters=20, expenses=[]),
                                   max_periods=3)
        self.assertEqual(table["periods"],
                         ["2026-06-30", "2026-09-30", "2026-12-31"])

    def test_a_specification_without_a_company_is_refused(self):
        with self.assertRaises(ModelInputError):
            build_model_inputs(self.ledger(), {**_spec(), "company_ref": ""})

    def test_readiness_counts_rather_than_scores(self):
        table = build_model_inputs(self.ledger(), _spec())
        readiness = table["readiness"]
        self.assertEqual(readiness["rows_by_status"], {FILED: 2})
        self.assertEqual(readiness["period_count"], 2)
        self.assertEqual(readiness["first_period"], "2026-02-28")
        self.assertEqual(readiness["last_period"], "2026-05-31")
        # No single number that a reader could accept in place of the detail.
        self.assertNotIn("score", readiness)
        self.assertNotIn("percent_ready", readiness)

    def test_a_derived_cell_is_counted_as_one(self):
        ledger = FakeMissions([
            _line("us-gaap:Revenues", "2026-01-01", "2026-03-31", "100"),
            _line("us-gaap:Revenues", "2026-01-01", "2026-06-30", "250"),
        ])
        table = build_model_inputs(ledger, _spec(expenses=[]))
        line = table["filed_lines"][0]
        self.assertEqual(line["cells"]["2026-06-30"]["basis"],
                         "derived_from_cumulative")
        self.assertEqual(table["readiness"]["derived_cells"], 1)


if __name__ == "__main__":
    unittest.main()
