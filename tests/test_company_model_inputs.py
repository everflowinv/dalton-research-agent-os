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

from dalton_core.company_financial_statement_structure import (
    financial_input_authority,
    forecast_structure_binding,
    materialize_financial_statement_structure,
)
from dalton_core.company_model_inputs import (
    AMBIGUOUS,
    ESTIMATED,
    FILED,
    INCOMPLETE,
    NOT_FOUND,
    SHARED,
    ModelInputError,
    build_model_inputs,
)
from dalton_core.store import content_hash

ACN = "company:sec-cik:0001467373"


class FakeMissions:
    """A statements ledger of exactly the lines a test cares about."""

    def __init__(self, lines=None):
        self.lines = list(lines or [])

    def statement_series_lines(self, company_ref, concept, statement=None):
        return [row for row in self.lines if row["concept"] == concept]

    def statement_filings(self, company_ref=None):
        if not self.lines:
            return []
        return [{"ingest_id": "ingest:test", "company_ref": ACN,
                 "entity_name": "Accenture plc", "accession": "0001467373-26-000032",
                 "report_date": "2026-05-31", "form": "10-Q", "line_count": len(self.lines)}]

    def statement_lines(self, ingest_id, statement=None):
        return [row for row in self.lines
                if statement is None or row["statement"] == statement]


def _line(concept, start, end, value, *, statement="income",
          filed="2026-07-01", accession="0001467373-26-000032", unit="usd",
          level=1):
    return {
        "concept": concept, "statement": statement, "label": concept.split(":")[-1],
        "period_start": start, "period_end": end, "value": value, "unit": unit,
        "is_breakdown": False, "dimension_axis": None, "level": level,
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
        self.assertEqual(table["schema_version"], "0.2")
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
        self.assertNotIn("duration_facts", line)

    def test_built_inputs_materialize_the_persisted_statement_definition(self):
        spec = {
            **_spec(),
            "revenue_anchor_concept": "us-gaap:Revenues",
            "decided_by": "automation:test",
            "financial_statement_structure": {
                "schema_version": "0.1",
                "lines": [
                    {
                        "ref": "revenue", "role": "revenue", "label": "Revenue",
                        "kind": "filed", "concept": "us-gaap:Revenues",
                        "statement": "income", "unit": "usd",
                        "period_kind": "duration", "annual_semantics": "sum_quarters",
                        "forecast_method": "quarterly_growth", "forecast_base_ref": None,
                    },
                    {
                        "ref": "delivery", "role": "cost_of_revenue",
                        "label": "Cost of services", "kind": "filed",
                        "concept": "us-gaap:CostOfGoodsAndServicesSold",
                        "statement": "income", "unit": "usd",
                        "period_kind": "duration", "annual_semantics": "sum_quarters",
                        "forecast_method": "share_of_line",
                        "forecast_base_ref": "revenue",
                    },
                    {
                        "ref": "operating", "role": "operating_income",
                        "label": "Operating income", "kind": "derived",
                        "concept": None, "statement": "income", "unit": "usd",
                        "period_kind": "duration", "annual_semantics": "sum_quarters",
                        "forecast_method": "formula", "forecast_base_ref": None,
                    },
                    {
                        "ref": "pretax", "role": "pretax_income", "label": "Pretax",
                        "kind": "derived", "concept": None, "statement": "income",
                        "unit": "usd", "period_kind": "duration",
                        "annual_semantics": "sum_quarters", "forecast_method": "formula",
                        "forecast_base_ref": None,
                    },
                    {
                        "ref": "tax", "role": "income_tax_expense", "label": "Tax",
                        "kind": "filed", "concept": "us-gaap:IncomeTaxExpenseBenefit",
                        "statement": "income", "unit": "usd",
                        "period_kind": "duration", "annual_semantics": "sum_quarters",
                        "forecast_method": "unavailable", "forecast_base_ref": None,
                    },
                    {
                        "ref": "net", "role": "net_income", "label": "Net income",
                        "kind": "derived", "concept": None, "statement": "income",
                        "unit": "usd", "period_kind": "duration",
                        "annual_semantics": "sum_quarters", "forecast_method": "formula",
                        "forecast_base_ref": None,
                    },
                ],
                "formulas": [{
                    "output_ref": "operating", "operator": "sum",
                    "terms": [
                        {"line_ref": "revenue", "coefficient": "1"},
                        {"line_ref": "delivery", "coefficient": "-1"},
                    ],
                    "tie_out_concept": "us-gaap:OperatingIncomeLoss",
                    "evidence_refs": ["0001467373-26-000032"],
                }, {
                    "output_ref": "pretax", "operator": "sum",
                    "terms": [{"line_ref": "operating", "coefficient": "1"}],
                    "tie_out_concept": "us-gaap:IncomeBeforeTax",
                    "evidence_refs": ["0001467373-26-000032"],
                }, {
                    "output_ref": "net", "operator": "sum",
                    "terms": [
                        {"line_ref": "pretax", "coefficient": "1"},
                        {"line_ref": "tax", "coefficient": "-1"},
                    ],
                    "tie_out_concept": "us-gaap:NetIncomeLoss",
                    "evidence_refs": ["0001467373-26-000032"],
                }],
            },
        }
        spec["content_hash"] = content_hash(spec)
        ledger = FakeMissions(self.ledger().lines + [
            _line("us-gaap:OperatingIncomeLoss",
                  "2026-03-01", "2026-05-31", "6718144000"),
            _line("us-gaap:IncomeBeforeTax",
                  "2026-03-01", "2026-05-31", "6718144000"),
            _line("us-gaap:IncomeTaxExpenseBenefit",
                  "2026-03-01", "2026-05-31", "1000000000"),
            _line("us-gaap:NetIncomeLoss",
                  "2026-03-01", "2026-05-31", "5718144000"),
        ])
        table = build_model_inputs(ledger, spec)
        self.assertEqual(table["schema_version"], "0.3")
        # This concept is used only as a formula tie-out. It must still be in
        # the exact current financial authority, without becoming a model row.
        self.assertIn("us-gaap:OperatingIncomeLoss",
                      {line["concept"] for line in table["filed_lines"]})
        self.assertNotIn("operating", {row["ref"] for row in table["rows"]})
        revenue = next(line for line in table["filed_lines"]
                       if line["concept"] == "us-gaap:Revenues")
        self.assertEqual(
            [(item["period_end"], item["period_kind"])
             for item in revenue["duration_facts"]],
            [("2026-02-28", "quarter"), ("2026-05-31", "quarter")],
        )
        structure, replay = materialize_financial_statement_structure(spec, table)
        binding = forecast_structure_binding(structure, replay, table)
        self.assertEqual(binding["financial_input_hash"],
                         financial_input_authority(table)["content_hash"])
        self.assertEqual(
            {item["line_ref"]: item["status"] for item in replay["forecast_methods"]},
            {"delivery": "validated", "operating": "validated",
             "net": "validated", "pretax": "validated",
             "revenue": "validated", "tax": "unavailable"},
        )

    def test_cash_input_refuses_ambiguous_frozen_operating_cash_concepts(self):
        periods = (("2025-01-01", "2025-03-31"),
                   ("2025-04-01", "2025-06-30"))
        cash = [
            _line(concept, start, end, "100", statement="cash")
            for concept in (
                "us-gaap:NetCashProvidedByUsedInOperatingActivities",
                "us-gaap:NetCashProvidedByUsedInOperatingActivitiesContinuingOperations",
            )
            for start, end in periods
        ]
        cash += [_line("us-gaap:PaymentsToAcquirePropertyPlantAndEquipment",
                       start, end, "10", statement="cash") for start, end in periods]
        spec = {**_spec(), "forecast_statements": [
            {"statement": "cash", "importance": "required"}]}
        table = build_model_inputs(FakeMissions(self.ledger().lines + cash), spec)
        ocf = next(item for item in table["cash_flow_inputs"]
                   if item["role"] == "operating_cash_flow")
        self.assertEqual(ocf["status"], AMBIGUOUS)
        self.assertIn("2 frozen filed concepts", ocf["reason"])

    def test_cash_input_keeps_missing_quarter_and_wrong_sign_as_gaps(self):
        ocf = "us-gaap:NetCashProvidedByUsedInOperatingActivities"
        capex = "us-gaap:PaymentsToAcquirePropertyPlantAndEquipment"
        cash = [
            _line(ocf, "2025-01-01", "2025-03-31", "100", statement="cash"),
            _line(ocf, "2025-07-01", "2025-09-30", "120", statement="cash"),
            _line(capex, "2025-01-01", "2025-03-31", "-10", statement="cash"),
        ]
        spec = {**_spec(), "forecast_statements": [
            {"statement": "cash", "importance": "required"}]}
        table = build_model_inputs(FakeMissions(self.ledger().lines + cash), spec)
        by_role = {item["role"]: item for item in table["cash_flow_inputs"]}
        self.assertEqual(by_role["operating_cash_flow"]["status"], INCOMPLETE)
        self.assertTrue(by_role["operating_cash_flow"]["gaps"])

    def test_v04_cash_input_uses_exact_company_selected_concepts(self):
        periods = (("2025-01-01", "2025-03-31"),
                   ("2025-04-01", "2025-06-30"),
                   ("2025-07-01", "2025-09-30"),
                   ("2025-10-01", "2025-12-31"))
        selected = {
            "operating_cash_flow": "acme:CashGeneratedFromOperations",
            "capital_expenditure": "acme:PurchasesOfEquipment",
        }
        cash = [
            _line(concept, start, end, "100" if role == "operating_cash_flow" else "10",
                  statement="cash")
            for role, concept in selected.items() for start, end in periods
        ]
        spec = {
            **_spec(), "schema_version": "0.4",
            "forecast_statements": [{"statement": "cash", "importance": "required"}],
            "cash_flow_companion": {
                "schema_version": "0.1",
                "lines": [
                    {"role": role, "concept": concept,
                     "forecast_method": "share_of_line",
                     "forecast_base_ref": "revenue", "because": "Company selected."}
                    for role, concept in selected.items()
                ],
                "formula": {"output_ref": "free_cash_flow", "operator": "sum",
                            "terms": []},
            },
        }
        table = build_model_inputs(FakeMissions(self.ledger().lines + cash), spec)
        self.assertEqual(
            {item["role"]: item.get("concept") for item in table["cash_flow_inputs"]},
            selected,
        )

    def test_v04_cash_input_rejects_negative_selected_capex(self):
        periods = (("2025-01-01", "2025-03-31"),
                   ("2025-04-01", "2025-06-30"),
                   ("2025-07-01", "2025-09-30"),
                   ("2025-10-01", "2025-12-31"))
        cash = [
            _line("acme:CashGeneratedFromOperations", start, end, "100", statement="cash")
            for start, end in periods
        ] + [
            _line("acme:PurchasesOfEquipment", start, end, "-10", statement="cash")
            for start, end in periods
        ]
        spec = {
            **_spec(), "schema_version": "0.4",
            "forecast_statements": [{"statement": "cash", "importance": "required"}],
            "cash_flow_companion": {
                "schema_version": "0.1",
                "lines": [
                    {"role": "operating_cash_flow",
                     "concept": "acme:CashGeneratedFromOperations"},
                    {"role": "capital_expenditure",
                     "concept": "acme:PurchasesOfEquipment"},
                ],
            },
        }
        table = build_model_inputs(FakeMissions(self.ledger().lines + cash), spec)
        capex = next(item for item in table["cash_flow_inputs"]
                     if item["role"] == "capital_expenditure")
        self.assertEqual(capex["status"], NOT_FOUND)
        self.assertIn("outflow sign convention", capex["reason"])

    def test_v04_cash_input_refuses_a_known_concept_under_the_other_role(self):
        spec = {
            **_spec(), "schema_version": "0.4",
            "forecast_statements": [{"statement": "cash", "importance": "required"}],
            "cash_flow_companion": {
                "schema_version": "0.1",
                "lines": [
                    {"role": "operating_cash_flow",
                     "concept": "us-gaap:PaymentsToAcquirePropertyPlantAndEquipment"},
                    {"role": "capital_expenditure",
                     "concept": "us-gaap:NetCashProvidedByUsedInOperatingActivities"},
                ],
            },
        }
        with self.assertRaisesRegex(ModelInputError, "known capital_expenditure"):
            build_model_inputs(self.ledger(), spec)

    def test_v04_cash_input_refuses_duplicate_end_and_overlapping_windows(self):
        concept = "acme:CashGeneratedFromOperations"
        periods = [
            ("2025-01-01", "2025-03-31"), ("2025-04-01", "2025-06-30"),
            ("2025-04-02", "2025-06-30"), ("2025-07-01", "2025-09-30"),
            ("2025-10-01", "2025-12-31"),
        ]
        cash = [_line(concept, start, end, "100", statement="cash")
                for start, end in periods]
        spec = {
            **_spec(), "schema_version": "0.4",
            "forecast_statements": [{"statement": "cash", "importance": "required"}],
            "cash_flow_companion": {
                "schema_version": "0.1",
                "lines": [
                    {"role": "operating_cash_flow", "concept": concept},
                    {"role": "capital_expenditure", "concept": None},
                ],
            },
        }
        table = build_model_inputs(FakeMissions(self.ledger().lines + cash), spec)
        operating = next(item for item in table["cash_flow_inputs"]
                         if item["role"] == "operating_cash_flow")
        self.assertEqual(operating["status"], NOT_FOUND)
        self.assertIn("duplicate or overlapping", operating["reason"])

    def test_cash_input_rejects_wrong_statement_dimensions_and_mixed_units(self):
        ocf = "us-gaap:NetCashProvidedByUsedInOperatingActivities"
        capex = "us-gaap:PaymentsToAcquirePropertyPlantAndEquipment"
        rows = [
            _line(ocf, "2025-01-01", "2025-03-31", "100", statement="income"),
            _line(capex, "2025-01-01", "2025-03-31", "10", statement="cash"),
            _line(capex, "2025-04-01", "2025-06-30", "12", statement="cash",
                  unit="eur"),
        ]
        dimensional = _line(
            ocf, "2025-04-01", "2025-06-30", "110", statement="cash")
        dimensional["dimension_axis"] = "segment"
        rows.append(dimensional)
        spec = {**_spec(), "forecast_statements": [
            {"statement": "cash", "importance": "required"}]}
        table = build_model_inputs(FakeMissions(self.ledger().lines + rows), spec)
        by_role = {item["role"]: item for item in table["cash_flow_inputs"]}
        self.assertEqual(by_role["operating_cash_flow"]["status"], NOT_FOUND)
        self.assertIn("cash statement", by_role["operating_cash_flow"]["reason"])
        self.assertEqual(by_role["capital_expenditure"]["status"], NOT_FOUND)
        self.assertIn("single-unit", by_role["capital_expenditure"]["reason"])

    def test_one_quarter_or_nonderivable_annual_pair_is_incomplete(self):
        ocf = "us-gaap:NetCashProvidedByUsedInOperatingActivities"
        capex = "us-gaap:PaymentsToAcquirePropertyPlantAndEquipment"
        rows = [
            _line(concept, "2025-01-01", end, value, statement="cash")
            for concept, values in ((ocf, ("100", "500")), (capex, ("10", "50")))
            for end, value in zip(("2025-03-31", "2025-12-31"), values)
        ]
        spec = {**_spec(), "forecast_statements": [
            {"statement": "cash", "importance": "required"}]}
        table = build_model_inputs(FakeMissions(self.ledger().lines + rows), spec)
        self.assertTrue(all(item["status"] == INCOMPLETE
                            for item in table["cash_flow_inputs"]))
        self.assertTrue(all("four consecutive quarters" in item["reason"]
                            for item in table["cash_flow_inputs"]))

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

    def test_a_filed_line_no_model_row_uses_is_named(self):
        # Live: IBM's specification bound every revenue driver to nothing filed
        # -- adoption, price mix and rate mix genuinely are not in GAAP -- so
        # the model had no filed top line at all while the filings report
        # us-gaap:Revenues plainly. No single row was wrong; the omission
        # existed only between them, which a reviewer cannot see and a derived
        # list can.
        ledger = FakeMissions([
            _line("us-gaap:Revenues", "2026-03-01", "2026-05-31", "18718144000"),
            _line("us-gaap:GrossProfit", "2026-03-01", "2026-05-31", "6000000000"),
            _line("us-gaap:CostOfGoodsAndServicesSold",
                  "2026-03-01", "2026-05-31", "12000000000"),
        ])
        spec = _spec(drivers=[{
            "ref": "billable-capacity", "label": "Billable capacity",
            "kind": "volume", "basis_concept": None, "unit": "headcount",
            "because": "Capacity binds delivery revenue.",
        }])
        unused = build_model_inputs(ledger, spec)["readiness"][
            "filed_income_lines_no_row_uses"]
        concepts = [item["concept"] for item in unused]
        self.assertIn("us-gaap:Revenues", concepts)
        self.assertIn("us-gaap:GrossProfit", concepts)
        # The one a row does draw on is not listed as unused.
        self.assertNotIn("us-gaap:CostOfGoodsAndServicesSold", concepts)

    def test_per_share_lines_are_not_offered_as_model_rows(self):
        # Earnings per share and share counts are filed on the income statement
        # and are not model rows. The data says so without a list of names:
        # they carry a different unit from the rest of the statement.
        ledger = FakeMissions([
            _line("us-gaap:Revenues", "2026-03-01", "2026-05-31", "18718144000"),
            _line("us-gaap:GrossProfit", "2026-03-01", "2026-05-31", "6000000000"),
            _line("us-gaap:EarningsPerShareBasic", "2026-03-01", "2026-05-31",
                  "3.03", unit="usdPerShare"),
            _line("us-gaap:WeightedAverageNumberOfSharesOutstandingBasic",
                  "2026-03-01", "2026-05-31", "620000000", unit="shares"),
        ])
        unused = build_model_inputs(
            ledger, _spec(drivers=[], expenses=[
                {"ref": "cost", "label": "Cost", "basis_concept": None,
                 "behaviour": "fixed", "driver_ref": None, "because": "x"}],
            ))["readiness"]["filed_income_lines_no_row_uses"]
        concepts = [item["concept"] for item in unused]
        self.assertIn("us-gaap:Revenues", concepts)
        self.assertNotIn("us-gaap:EarningsPerShareBasic", concepts)
        self.assertNotIn("us-gaap:WeightedAverageNumberOfSharesOutstandingBasic",
                         concepts)

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
