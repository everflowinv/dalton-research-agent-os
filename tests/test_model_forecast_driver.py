"""P13-M2: drivers, assumptions, results, and the things this layer refuses to do.

The arithmetic here is deliberately hand-computable -- revenue growing ten
percent a quarter, cost at eighty percent of it -- so that a test failing means
the model changed rather than that the fixture drifted. The expected numbers
below were worked out on paper and are written out in full.

What the rest of the file is about is the refusals, because they are the part
that makes the numbers worth anything: a driver whose concept nobody filed gets
no assumption at all, two concepts claiming the revenue role are refused rather
than chosen between, a filed line the specification splits keeps its total and
loses its split, and automation may never write an assumption that says a human
decided it.
"""

from __future__ import annotations

import json
import sqlite3
import unittest
from decimal import Decimal

from dalton_core.company_model_inputs import build_model_inputs
from dalton_core.company_model_report import render_forecast_model
from dalton_core.model_forecast import DRIVER_FORMULA_HASH, DRIVER_FORMULA_REF
from dalton_core.model_forecast_driver import (
    CHANGE_REASONS,
    CONCEPT_ROLES,
    GENERATOR_REF,
    MAX_REALISED_PERIODS,
    SOURCE_VERSION_KEY,
    AssumptionRefused,
    ForecastModelAuthority,
    ForecastModelUnavailable,
    ForecastModelValidationError,
    actualize_model,
    build_drivers,
    build_forecast_model,
    company_slug,
    default_assumptions,
    draft_assumptions,
    forecast_periods,
    ForecastModelConflict,
    model_readiness,
    replay_cell,
    revenue_anchor,
    revise_assumptions,
    validate_forecast_model,
)
from dalton_core import model_forecast_driver
from dalton_core.store import DaltonStore, canonical_json
from tests.test_company_model_inputs import ACN, FakeMissions, _line

REVENUE_CONCEPT = "us-gaap:Revenues"
COST_CONCEPT = "us-gaap:CostOfGoodsAndServicesSold"
SGA_CONCEPT = "us-gaap:SellingGeneralAndAdministrativeExpense"
TAX_CONCEPT = "us-gaap:IncomeTaxExpenseBenefit"

# Four consecutive fiscal quarters, Accenture's calendar.
QUARTERS = (
    ("2025-06-01", "2025-08-31"),
    ("2025-09-01", "2025-11-30"),
    ("2025-12-01", "2026-02-28"),
    ("2026-03-01", "2026-05-31"),
)
# Revenue grows exactly ten percent a quarter; cost is exactly eighty percent
# of revenue, SG&A exactly ten percent, tax exactly a quarter of what is left.
SERIES = {
    REVENUE_CONCEPT: ("1000000000", "1100000000", "1210000000", "1331000000"),
    COST_CONCEPT: ("800000000", "880000000", "968000000", "1064800000"),
    SGA_CONCEPT: ("100000000", "110000000", "121000000", "133100000"),
    TAX_CONCEPT: ("25000000", "27500000", "30250000", "33275000"),
}


def ledger(series=None):
    series = SERIES if series is None else series
    lines = []
    for concept, values in series.items():
        for (start, end), value in zip(QUARTERS, values):
            lines.append(_line(concept, start, end, value))
    return FakeMissions(lines)


def spec(*, drivers=None, expenses=None, cash="not_material", quarters=4,
         revenue_anchor_concept=None):
    return {
        "spec_id": "company-model-spec:test", "company_ref": ACN,
        "state_hash": "a" * 64, "content_hash": "b" * 64,
        **({"revenue_anchor_concept": revenue_anchor_concept}
           if revenue_anchor_concept else {}),
        "revenue_drivers": drivers if drivers is not None else [{
            "ref": "top-line", "label": "Client work", "kind": "mix",
            "basis_concept": REVENUE_CONCEPT, "unit": "USD",
            "because": "The blend moves the total.",
        }],
        "expense_lines": expenses if expenses is not None else [
            {"ref": "delivery", "label": "Cost of services",
             "basis_concept": COST_CONCEPT, "behaviour": "variable_with_revenue",
             "driver_ref": None, "because": "Delivery cost follows revenue."},
            {"ref": "sga", "label": "Selling, general and administrative",
             "basis_concept": SGA_CONCEPT, "behaviour": "semi_variable",
             "driver_ref": None, "because": "Sales cost scales with the book."},
            {"ref": "tax", "label": "Income tax", "basis_concept": TAX_CONCEPT,
             "behaviour": "variable_with_revenue", "driver_ref": None,
             "because": "The rate is stable and the mix is not moving."},
        ],
        "operating_metrics": [],
        "forecast_statements": [
            {"statement": "income", "importance": "required", "because": "The question."},
            {"statement": "balance", "importance": "supporting", "because": "Capital light."},
            {"statement": "cash", "importance": cash, "because": "Judgement."},
        ],
        "horizon": {"historical_quarters": 12, "forecast_quarters": quarters,
                    "because": "The cycle."},
    }


def model(missions=None, specification=None, **kwargs):
    missions = ledger() if missions is None else missions
    specification = spec() if specification is None else specification
    table = build_model_inputs(missions, specification)
    return build_forecast_model(specification, table, **kwargs)


def cells_of(record, ref):
    result = next(item for item in record["results"] if item["ref"] == ref)
    return {cell["period"]["end"]: cell for cell in result["cells"]}


class DriverTests(unittest.TestCase):
    def test_a_driver_is_keyed_by_the_concept_and_carries_its_filed_cells(self):
        drivers = build_drivers(build_model_inputs(ledger(), spec()))
        top = next(item for item in drivers if item["concept"] == REVENUE_CONCEPT)
        self.assertEqual(top["ref"], f"concept:{REVENUE_CONCEPT}")
        self.assertEqual(top["kind"], "revenue")
        self.assertEqual(top["role"], "revenue")
        self.assertEqual(top["spec_rows"], ["top-line"])
        self.assertEqual([cell["period_end"] for cell in top["history"]],
                         [end for _, end in QUARTERS])
        cell = top["history"][-1]
        self.assertEqual(cell["value"], "1331000000")
        self.assertEqual(cell["accessions"], ["0001467373-26-000032"])

    def test_several_rows_on_one_filed_line_keep_the_total_and_lose_the_split(self):
        drivers = build_drivers(build_model_inputs(ledger(), spec(expenses=[
            {"ref": "delivery-staff", "label": "Delivery staff",
             "basis_concept": COST_CONCEPT, "behaviour": "variable_with_headcount",
             "driver_ref": None, "because": "Payroll follows the billable base."},
            {"ref": "subcontractors", "label": "Subcontractors",
             "basis_concept": COST_CONCEPT, "behaviour": "variable_with_revenue",
             "driver_ref": None, "because": "Bought-in delivery flexes."},
        ])))
        cost = next(item for item in drivers if item["concept"] == COST_CONCEPT)
        self.assertEqual(cost["status"], "share_of_filed")
        self.assertEqual(cost["spec_rows"], ["delivery-staff", "subcontractors"])
        self.assertIn("the split is not filed", cost["note"])
        # One driver, not two copies of one series.
        self.assertEqual(sum(1 for item in drivers if item["concept"] == COST_CONCEPT), 1)

    def test_a_row_with_no_filed_counterpart_is_a_driver_with_no_history(self):
        drivers = build_drivers(build_model_inputs(ledger(), spec(drivers=[
            {"ref": "top-line", "label": "Client work", "kind": "mix",
             "basis_concept": REVENUE_CONCEPT, "unit": "USD", "because": "x"},
            {"ref": "billable-heads", "label": "Billable headcount", "kind": "volume",
             "basis_concept": None, "unit": "headcount", "because": "Capacity binds."},
        ])))
        heads = next(item for item in drivers if item["ref"] == "row:billable-heads")
        self.assertEqual(heads["history"], [])
        self.assertEqual(heads["status"], "estimated")
        self.assertIsNone(heads["role"])

    def test_a_cash_flow_line_filed_on_the_income_statement_holds_no_role(self):
        # The one arithmetic error worth a guard of its own: depreciation is an
        # income-statement line for a filer whose cost of revenue excludes it,
        # and a cash-flow line for everyone else. Reading the cash-flow one as
        # an operating expense subtracts it a second time from a cost line that
        # already contains it, and the model looks entirely reasonable.
        concept = "us-gaap:NetCashProvidedByUsedInOperatingActivities"
        missions = FakeMissions(ledger().lines + [
            _line(concept, start, end, "1", statement="income")
            for start, end in QUARTERS])
        drivers = build_drivers(build_model_inputs(missions, spec(expenses=[
            {"ref": "ocf", "label": "Operating cash flow", "basis_concept": concept,
             "behaviour": "fixed", "driver_ref": None, "because": "Cash matters."}])))
        driver = next(item for item in drivers if item["concept"] == concept)
        self.assertEqual(driver["statement"], "income")
        self.assertIsNone(driver["role"])

    def test_a_concept_with_no_frozen_role_says_nothing_is_computed_from_it(self):
        drivers = build_drivers(build_model_inputs(
            FakeMissions([_line("us-gaap:OperatingExpenses", start, end, "1")
                          for start, end in QUARTERS]
                         + [_line(REVENUE_CONCEPT, start, end, value)
                            for (start, end), value
                            in zip(QUARTERS, SERIES[REVENUE_CONCEPT])]),
            spec(expenses=[{"ref": "opex", "label": "Operating expenses",
                            "basis_concept": "us-gaap:OperatingExpenses",
                            "behaviour": "fixed", "driver_ref": None,
                            "because": "It is what the filing shows."}])))
        opex = next(item for item in drivers
                    if item["concept"] == "us-gaap:OperatingExpenses")
        self.assertIsNone(opex["role"])
        self.assertIn("no frozen model role", opex["note"])
        # And the table is a whitelist of components, so a subtotal is not in it.
        self.assertNotIn("us-gaap:OperatingExpenses", CONCEPT_ROLES)
        self.assertNotIn("us-gaap:CostsAndExpenses", CONCEPT_ROLES)

    def test_two_concepts_claiming_the_revenue_role_are_refused_not_chosen(self):
        other = "us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax"
        missions = ledger({**SERIES, other: ("1", "2", "3", "4")})
        specification = spec(drivers=[
            {"ref": "a", "label": "Revenues", "kind": "mix",
             "basis_concept": REVENUE_CONCEPT, "unit": "USD", "because": "x"},
            {"ref": "b", "label": "Contract revenue", "kind": "mix",
             "basis_concept": other, "unit": "USD", "because": "y"},
        ])
        drivers = build_drivers(build_model_inputs(missions, specification))
        with self.assertRaises(ForecastModelUnavailable) as caught:
            revenue_anchor(drivers)
        self.assertIn("more than one filed concept", str(caught.exception))

    def test_a_company_with_no_filed_revenue_cannot_be_modelled(self):
        missions = FakeMissions([_line(COST_CONCEPT, start, end, "1")
                                 for start, end in QUARTERS])
        with self.assertRaises(ForecastModelUnavailable) as caught:
            model(missions, spec(drivers=[{
                "ref": "heads", "label": "Billable headcount", "kind": "volume",
                "basis_concept": None, "unit": "headcount", "because": "Capacity."}]))
        self.assertIn("no revenue driver", str(caught.exception))

    def test_a_filed_anchor_starts_the_model_without_masquerading_as_a_driver(self):
        specification = spec(
            revenue_anchor_concept=REVENUE_CONCEPT,
            drivers=[{
                "ref": "heads", "label": "Billable heads", "kind": "volume",
                "basis_concept": None, "unit": "headcount",
                "because": "Capacity moves delivered revenue.",
            }],
        )
        record = model(ledger(), specification)
        anchor = revenue_anchor(record["drivers"])
        self.assertEqual(anchor["concept"], REVENUE_CONCEPT)
        economic = next(item for item in record["drivers"] if item["ref"] == "row:heads")
        self.assertIsNone(economic["concept"])
        self.assertIn("result:revenue", {
            item["ref"] for item in record["results"]
            if any(cell.get("status") == "computed" for cell in item["cells"])
        })

    def test_the_forecast_columns_follow_the_last_filed_quarter(self):
        drivers = build_drivers(build_model_inputs(ledger(), spec()))
        periods = forecast_periods(revenue_anchor(drivers), 4)
        self.assertEqual([item["end"] for item in periods],
                         ["2026-08-31", "2026-11-30", "2027-02-28", "2027-05-31"])
        self.assertEqual(periods[0]["start"], "2026-06-01")
        self.assertEqual(periods[0]["kind"], "quarter")


class AssumptionTests(unittest.TestCase):
    def test_the_default_growth_is_the_trailing_average_and_names_its_quarters(self):
        record = model()
        growth = next(item for item in record["assumptions"]
                      if item["driver_ref"] == f"concept:{REVENUE_CONCEPT}")
        self.assertEqual(growth["measure"], "quarterly_growth")
        self.assertEqual(Decimal(growth["value"]), Decimal("0.1"))
        self.assertEqual(growth["kind"], "estimate")
        self.assertIn("2026-05-31", growth["because"])
        self.assertIn("carried forward unchanged", growth["because"])
        self.assertEqual(growth["provenance"]["rule_ref"], GENERATOR_REF)
        # Every rate is defended by the cells it was computed from.
        self.assertTrue(growth["refs"])
        self.assertEqual({ref["concept"] for ref in growth["refs"]}, {REVENUE_CONCEPT})
        self.assertTrue(all(ref["accession"] for ref in growth["refs"]))

    def test_an_expense_is_a_share_of_revenue_and_tax_a_share_of_operating_income(self):
        record = model()
        by_driver = {}
        for item in record["assumptions"]:
            by_driver.setdefault(item["driver_ref"], item)
        cost = by_driver[f"concept:{COST_CONCEPT}"]
        self.assertEqual(cost["measure"], "revenue_share")
        self.assertEqual(Decimal(cost["value"]), Decimal("0.8"))
        tax = by_driver[f"concept:{TAX_CONCEPT}"]
        self.assertEqual(tax["measure"], "operating_income_share")
        self.assertEqual(Decimal(tax["value"]), Decimal("0.25"))
        self.assertIn("share of operating income", tax["because"])

    def test_there_is_one_assumption_per_driver_per_forecast_quarter(self):
        record = model(specification=spec(quarters=6))
        self.assertEqual(len(record["forecast_periods"]), 6)
        for driver in (REVENUE_CONCEPT, COST_CONCEPT, SGA_CONCEPT, TAX_CONCEPT):
            rows = [item for item in record["assumptions"]
                    if item["driver_ref"] == f"concept:{driver}"]
            self.assertEqual(len(rows), 6, driver)
            self.assertEqual(len({item["period"]["end"] for item in rows}), 6)

    def test_a_hole_in_the_history_is_not_treated_as_a_consecutive_quarter(self):
        # A company whose history comes from 10-Qs is missing every fourth
        # quarter. Reading across the hole would put a year of growth into a
        # one-quarter rate: 1000 -> 1500 is not fifty percent in a quarter.
        missions = FakeMissions([
            _line(REVENUE_CONCEPT, "2025-06-01", "2025-08-31", "1000000000"),
            _line(REVENUE_CONCEPT, "2025-09-01", "2025-11-30", "1100000000"),
            # 2025-12-01..2026-02-28 never filed: the fourth quarter.
            _line(REVENUE_CONCEPT, "2026-03-01", "2026-05-31", "1500000000"),
        ])
        drivers = build_drivers(build_model_inputs(missions, spec(expenses=[])))
        periods = forecast_periods(revenue_anchor(drivers), 2)
        assumptions = default_assumptions(drivers, periods)
        self.assertEqual(Decimal(assumptions[0]["value"]), Decimal("0.1"))

    def test_a_balance_sheet_line_gets_no_assumption_because_it_is_not_a_flow(self):
        missions = FakeMissions(
            [_line(REVENUE_CONCEPT, start, end, value)
             for (start, end), value in zip(QUARTERS, SERIES[REVENUE_CONCEPT])]
            + [_line("us-gaap:Goodwill", None, end, "5000000000",
                     statement="balance") for _, end in QUARTERS])
        record = model(missions, spec(expenses=[{
            "ref": "goodwill", "label": "Goodwill", "basis_concept": "us-gaap:Goodwill",
            "behaviour": "fixed", "driver_ref": None, "because": "Acquisitive."}]))
        self.assertEqual(
            [item["driver_ref"] for item in record["assumptions"]],
            [f"concept:{REVENUE_CONCEPT}"] * len(record["forecast_periods"]))

    def test_a_drafted_assumption_that_cites_an_unfiled_quarter_is_refused(self):
        drivers = build_drivers(build_model_inputs(ledger(), spec()))
        periods = forecast_periods(revenue_anchor(drivers), 2)

        def drafter(_drivers, _periods):
            return [{
                "driver_ref": f"concept:{REVENUE_CONCEPT}", "period": dict(_periods[0]),
                "measure": "quarterly_growth", "value": "0.2", "unit": "ratio",
                "kind": "estimate", "because": "Bookings accelerated.",
                "refs": [{"kind": "input_cell", "ref": None,
                          "concept": REVENUE_CONCEPT, "period_end": "2019-05-31",
                          "accession": "0001467373-19-000001"}],
            }]

        with self.assertRaises(AssumptionRefused) as caught:
            draft_assumptions(drivers, periods, drafter=drafter,
                              decided_by="automation:drafter")
        self.assertIn("no filed cell", str(caught.exception))

    def test_a_drafted_assumption_may_never_claim_a_human_decided_it(self):
        drivers = build_drivers(build_model_inputs(ledger(), spec()))
        periods = forecast_periods(revenue_anchor(drivers), 1)
        with self.assertRaises(AssumptionRefused):
            draft_assumptions(
                drivers, periods, decided_by="automation:drafter",
                drafter=lambda _d, _p: [{
                    "driver_ref": f"concept:{REVENUE_CONCEPT}", "period": dict(_p[0]),
                    "measure": "quarterly_growth", "value": "0.2", "unit": "ratio",
                    "kind": "human", "because": "The analyst said so.", "refs": []}])


class ResultTests(unittest.TestCase):
    """The arithmetic, worked out on paper.

    Revenue 1,331.0m growing 10% a quarter; cost 80% of revenue; SG&A 10%; tax
    25% of operating income.

        revenue          1,331.0 * 1.1        = 1,464.1
        cost of revenue  1,464.1 * 0.8        = 1,171.28
        gross profit     1,464.1 - 1,171.28   =   292.82
        SG&A             1,464.1 * 0.1        =   146.41
        operating income   292.82 - 146.41    =   146.41
        income tax         146.41 * 0.25      =    36.6025
        net income         146.41 - 36.6025   =   109.8075
    """

    def setUp(self) -> None:
        self.record = model()

    def expect(self, ref, period, value):
        cell = cells_of(self.record, ref)[period]
        self.assertEqual(cell["status"], "computed", cell.get("reason"))
        self.assertEqual(Decimal(cell["value"]), Decimal(value))

    def test_the_income_chain_is_the_arithmetic_of_the_assumptions(self):
        self.expect("result:revenue", "2026-08-31", "1464100000")
        self.expect("result:cost_of_revenue", "2026-08-31", "1171280000")
        self.expect("result:gross_profit", "2026-08-31", "292820000")
        self.expect(f"result:operating_expense:{SGA_CONCEPT}", "2026-08-31", "146410000")
        self.expect("result:operating_income", "2026-08-31", "146410000")
        self.expect("result:income_tax_expense", "2026-08-31", "36602500")
        self.expect("result:net_income", "2026-08-31", "109807500")

    def test_the_second_quarter_compounds_on_the_first(self):
        self.expect("result:revenue", "2026-11-30", "1610510000")
        self.expect("result:net_income", "2026-11-30", "120788250")

    def test_every_result_cell_names_what_it_was_computed_from(self):
        first = cells_of(self.record, "result:revenue")["2026-08-31"]
        self.assertEqual(first["assumption_refs"],
                         [f"assumption:concept:{REVENUE_CONCEPT}@2026-08-31:estimate"])
        # The first quarter rests on the last filed quarter; the second rests
        # on the first, and says so rather than looking like a filed figure.
        self.assertEqual(first["input_cell_refs"][0]["period_end"], "2026-05-31")
        self.assertEqual(first["result_refs"], [])
        second = cells_of(self.record, "result:revenue")["2026-11-30"]
        self.assertEqual(second["input_cell_refs"], [])
        self.assertEqual(second["result_refs"],
                         [{"ref": "result:revenue", "period_end": "2026-08-31"}])
        gross = cells_of(self.record, "result:gross_profit")["2026-08-31"]
        self.assertEqual([item["ref"] for item in gross["result_refs"]],
                         ["result:revenue", "result:cost_of_revenue"])

    def test_a_missing_concept_leaves_the_result_unavailable_not_invented(self):
        record = model(specification=spec(expenses=[
            {"ref": "delivery", "label": "Cost of services",
             "basis_concept": "us-gaap:CostOfRevenue",  # in the role table, not filed
             "behaviour": "variable_with_revenue", "driver_ref": None,
             "because": "Delivery cost follows revenue."}]))
        cost = next(item for item in record["results"]
                    if item["ref"] == "result:cost_of_revenue")
        self.assertEqual(cost["status"], "unavailable")
        self.assertIn("no filed cost-of-revenue concept", cost["reason"])
        for cell in cost["cells"]:
            self.assertIsNone(cell["value"])
        gross = next(item for item in record["results"]
                     if item["ref"] == "result:gross_profit")
        self.assertEqual(gross["status"], "unavailable")
        # Revenue still stands: one missing line does not void the ones that
        # do not depend on it.
        revenue = next(item for item in record["results"]
                       if item["ref"] == "result:revenue")
        self.assertEqual(revenue["status"], "computed")

    def test_the_cash_statement_is_forecast_only_when_the_specification_asks(self):
        quiet = next(item for item in model()["results"]
                     if item["ref"] == "result:free_cash_flow")
        self.assertEqual(quiet["status"], "unavailable")
        self.assertIn("not_material", quiet["reason"])
        asked = next(item for item in model(specification=spec(cash="required"))["results"]
                     if item["ref"] == "result:free_cash_flow")
        self.assertIn("operating-cash-flow", asked["reason"])

    def test_free_cash_flow_is_operating_cash_less_capex(self):
        ocf = "us-gaap:NetCashProvidedByUsedInOperatingActivities"
        capex = "us-gaap:PaymentsToAcquirePropertyPlantAndEquipment"
        missions = FakeMissions(ledger().lines + [
            _line(concept, start, end, value, statement="cash")
            for concept, values in (
                (ocf, ("200000000", "220000000", "242000000", "266200000")),
                (capex, ("50000000", "55000000", "60500000", "66550000")))
            for (start, end), value in zip(QUARTERS, values)
        ])
        record = model(missions, spec(cash="required", expenses=[
            {"ref": "delivery", "label": "Cost of services",
             "basis_concept": COST_CONCEPT, "behaviour": "variable_with_revenue",
             "driver_ref": None, "because": "Delivery cost follows revenue."},
            {"ref": "ocf", "label": "Operating cash flow", "basis_concept": ocf,
             "behaviour": "variable_with_revenue", "driver_ref": None,
             "because": "Cash conversion is the thesis."},
            {"ref": "capex", "label": "Capital expenditure", "basis_concept": capex,
             "behaviour": "semi_variable", "driver_ref": None,
             "because": "Capital light, but not free."},
        ]))
        # 20% and 5% of 1,464.1m.
        fcf = cells_of(record, "result:free_cash_flow")["2026-08-31"]
        self.assertEqual(Decimal(fcf["value"]), Decimal("219615000"))

    def test_readiness_counts_rather_than_scores(self):
        readiness = model_readiness(self.record)
        self.assertEqual(readiness["drivers"], 4)
        self.assertEqual(readiness["drivers_with_assumptions"], 4)
        self.assertEqual(readiness["forecast_quarters"], 4)
        self.assertIn("result:revenue", readiness["results_computed"])
        self.assertEqual(readiness["assumption_kinds"]["human"], 0)
        self.assertNotIn("score", readiness)


class VersionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = DaltonStore(":memory:")
        self.addCleanup(self.store.close)
        self.authority = ForecastModelAuthority(self.store)

    def test_recomputing_the_same_model_produces_the_same_bytes(self):
        first, second = model(), model()
        self.assertEqual(canonical_json(first), canonical_json(second))

    def test_an_unchanged_model_is_a_duplicate_not_a_second_version(self):
        stored = self.authority.publish(model())
        self.assertEqual(stored["status"], "fresh")
        self.assertEqual(stored["version"], 1)
        self.assertIsNone(stored["prior_version_ref"])
        again = self.authority.publish(model())
        self.assertEqual(again["status"], "duplicate")
        self.assertEqual(again["id"], stored["id"])
        self.assertEqual(len(self.authority.versions(ACN)), 1)

    def test_a_changed_assumption_is_a_new_version_that_names_the_old_one(self):
        first = self.authority.publish(model())
        second = self.authority.publish(revise_assumptions(
            first,
            [{"driver": f"concept:{REVENUE_CONCEPT}", "period": "2026-08-31",
              "value": "0.03",
              "because": "Bookings rolled over; growth halves from here.",
              "refs": [{"kind": "human_decision", "ref": "human:analyst",
                        "concept": None, "period_end": None, "accession": None}]}],
            change_reason="human_revision", actor_ref="human:analyst",
            evidence_refs=[{"kind": "human_decision", "ref": "human:analyst",
                            "concept": None, "period_end": None,
                            "accession": None}]))
        self.assertEqual(second["status"], "fresh")
        self.assertEqual(second["version"], 2)
        self.assertEqual(second["prior_version_ref"], first["id"])
        self.assertEqual(second["change_reason"], "human_revision")
        self.assertEqual(self.authority.latest(ACN)["id"], second["id"])
        # The old version is still readable, unchanged.
        self.assertEqual(self.authority.model(first["id"])["content_hash"],
                         first["content_hash"])

    def test_automation_may_not_publish_an_assumption_a_human_would_have_written(self):
        body = model()
        held = body["assumptions"][0]
        body["assumptions"][0] = {
            **held, "kind": "human",
            "ref": held["ref"].replace(":estimate", ":human"),
            "provenance": {"rule_ref": None, "work_order_ref": None,
                           "decided_by": "human:analyst"}}
        with self.assertRaises(ForecastModelValidationError) as caught:
            self.authority.publish(body)
        self.assertIn("may not write a human assumption", str(caught.exception))

    def test_a_human_assumption_must_name_the_person(self):
        body = model()
        body["actor_ref"] = "human:analyst"
        held = body["assumptions"][0]
        body["assumptions"][0] = {
            **held, "kind": "human",
            "ref": held["ref"].replace(":estimate", ":human")}
        with self.assertRaises(ForecastModelValidationError):
            self.authority.publish(body)

    def test_an_actual_that_cites_no_filing_is_not_an_actual(self):
        body = model()
        held = body["assumptions"][0]
        body["assumptions"][0] = {
            **held, "kind": "actual", "refs": [],
            "ref": held["ref"].replace(":estimate", ":actual"),
            "because": "it was carried forward, honest"}
        with self.assertRaises(ForecastModelValidationError) as caught:
            self.authority.publish(body)
        self.assertIn("cite the filing", str(caught.exception))

    def test_a_version_that_names_no_evidence_is_refused(self):
        body = model()
        body["evidence_refs"] = []
        with self.assertRaises(ForecastModelValidationError) as caught:
            self.authority.publish(body)
        self.assertIn("name the evidence", str(caught.exception))

    def test_the_record_is_immutable_and_unauthorized_writes_are_refused(self):
        stored = self.authority.publish(model())
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.connection.execute(
                "UPDATE forecast_model_versions SET company_ref='x' WHERE version_id=?",
                (stored["id"],))
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.connection.execute(
                "DELETE FROM forecast_model_versions WHERE version_id=?", (stored["id"],))
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.connection.execute(
                "INSERT INTO forecast_model_versions(version_id,model_ref,version_number,"
                "prior_version_id,company_ref,spec_ref,inputs_hash,body_hash,record_json,"
                "content_hash,actor_ref,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                ("forecast-model-version:x:1", "forecast-model:x", 1, None, "x",
                 "spec", "a" * 64, "b" * 64, "{}", "c" * 64, "automation:x", "now"))

    def test_a_record_whose_hash_does_not_replay_is_not_a_record(self):
        stored = self.authority.publish(model())
        tampered = dict(stored)
        tampered.pop("status")
        tampered["results"] = []
        with self.assertRaises(ForecastModelValidationError):
            validate_forecast_model(tampered)

    def test_the_stored_record_binds_the_specification_and_the_inputs(self):
        stored = self.authority.publish(model())
        self.assertEqual(stored["spec_ref"], "company-model-spec:test")
        self.assertEqual(stored["spec_hash"], "b" * 64)
        self.assertEqual(stored["formula_ref"], "formula:driver-model:1")
        self.assertEqual(stored["generator_ref"], GENERATOR_REF)
        self.assertTrue(stored["id"].startswith("forecast-model-version:"))
        row = self.store.connection.execute(
            "SELECT record_json FROM forecast_model_versions WHERE version_id=?",
            (stored["id"],)).fetchone()
        self.assertEqual(json.loads(row["record_json"])["content_hash"],
                         stored["content_hash"])


class RenderTests(unittest.TestCase):
    def test_the_view_shows_history_beside_the_forecast_and_says_why(self):
        store = DaltonStore(":memory:")
        self.addCleanup(store.close)
        record = ForecastModelAuthority(store).publish(model())
        text = render_forecast_model(record, entity_name="Accenture plc")
        self.assertIn("DRIVER MODEL  Accenture plc", text)
        self.assertIn("ASSUMPTIONS", text)
        self.assertIn("10.00%", text)
        self.assertIn("carried forward unchanged", text)
        self.assertIn("1,464.1", text)
        # An unavailable result prints its reason where its number would be.
        self.assertIn("unavailable: the specification marks the cash flow statement",
                      text)
        self.assertIn(" |", text)


def filed_quarter(missions, end="2026-08-31", start="2026-06-01", *, values=None,
                  accession="0001467373-26-000099"):
    """The next quarter arrives in a filing."""

    values = values or {
        REVENUE_CONCEPT: "1500000000", COST_CONCEPT: "1200000000",
        SGA_CONCEPT: "150000000", TAX_CONCEPT: "37500000",
    }
    lines = list(missions.lines)
    for concept, value in values.items():
        lines.append(_line(concept, start, end, value, accession=accession))
    return FakeMissions(lines)


class ActualisationTests(unittest.TestCase):
    """What happens when a quarter this model estimated is finally filed.

    The estimate is not deleted, not corrected and not moved. It stays where it
    is with the value it had, marked as superseded by the actual that answered
    it -- because the whole point of writing a forecast down is being able to
    find out later how wrong it was, and a model that overwrote its estimates
    with the outcome would score itself perfect every quarter.

    And nothing ahead of the filed quarter moves. Whether a print changes the
    view of next year is a judgement with a decision word on it; it arrives as
    a revision, from whoever made it.
    """

    def setUp(self) -> None:
        self.store = DaltonStore(":memory:")
        self.addCleanup(self.store.close)
        self.authority = ForecastModelAuthority(self.store)
        self.first = self.authority.publish(model())
        self.table = build_model_inputs(filed_quarter(ledger()), spec())

    def actualized(self):
        body = actualize_model(self.first, self.table)
        return self.authority.publish(body)

    def test_nothing_to_actualise_until_a_forecast_quarter_is_filed(self):
        self.assertIsNone(actualize_model(
            self.first, build_model_inputs(ledger(), spec())))

    def test_the_estimate_keeps_its_value_and_says_what_answered_it(self):
        second = self.actualized()
        cells = [cell for cell in cells_all(second, "result:revenue")
                 if cell["period"]["end"] == "2026-08-31"]
        estimate = next(item for item in cells if item["kind"] == "estimate")
        actual = next(item for item in cells if item["kind"] == "actual")
        self.assertEqual(Decimal(estimate["value"]), Decimal("1464100000"))
        self.assertEqual(estimate["superseded_by"], actual["ref"])
        self.assertEqual(Decimal(actual["value"]), Decimal("1500000000"))
        self.assertEqual(actual["input_cell_refs"][0]["accession"],
                         "0001467373-26-000099")
        self.assertEqual(second["change_reason"], "filing_actual")
        self.assertEqual([ref["accession"] for ref in second["evidence_refs"]],
                         ["0001467373-26-000099"] * len(second["evidence_refs"]))

    def test_the_quarters_still_ahead_are_not_touched(self):
        second = self.actualized()
        before = cells_of(self.first, "result:revenue")
        after = cells_of(second, "result:revenue")
        for end in ("2026-11-30", "2027-02-28", "2027-05-31"):
            self.assertEqual(after[end]["value"], before[end]["value"], end)
            self.assertEqual(after[end]["kind"], "estimate")
        self.assertEqual([item["end"] for item in second["forecast_periods"]],
                         ["2026-11-30", "2027-02-28", "2027-05-31"])
        self.assertEqual([item["end"] for item in second["realised_periods"]],
                         ["2026-08-31"])

    def test_a_derived_line_is_actual_only_when_its_components_are(self):
        second = self.actualized()
        gross = [cell for cell in cells_all(second, "result:gross_profit")
                 if cell["period"]["end"] == "2026-08-31"]
        actual = next(item for item in gross if item["kind"] == "actual")
        # 1,500.0 filed revenue less 1,200.0 filed cost.
        self.assertEqual(Decimal(actual["value"]), Decimal("300000000"))
        self.assertEqual([item["ref"] for item in actual["result_refs"]],
                         ["result:revenue", "result:cost_of_revenue"])
        self.assertEqual(actual["assumption_refs"], [])

    def test_the_realised_rate_is_recorded_beside_the_rate_we_assumed(self):
        second = self.actualized()
        rows = [item for item in second["assumptions"]
                if item["driver_ref"] == f"concept:{REVENUE_CONCEPT}"
                and item["period"]["end"] == "2026-08-31"]
        estimate = next(item for item in rows if item["kind"] == "estimate")
        actual = next(item for item in rows if item["kind"] == "actual")
        self.assertEqual(Decimal(estimate["value"]), Decimal("0.1"))
        # (1,500.0 - 1,331.0) / 1,331.0
        self.assertEqual(actual["value"], "0.126972201352")
        self.assertEqual(estimate["superseded_by"], actual["ref"])
        self.assertTrue(any(ref["accession"] == "0001467373-26-000099"
                            for ref in actual["refs"]))

    def test_a_second_filing_of_the_same_quarter_is_a_duplicate(self):
        self.actualized()
        latest = self.authority.latest(ACN)
        self.assertIsNone(actualize_model(latest, self.table))

    def test_what_we_estimated_and_what_happened_can_both_be_replayed(self):
        self.actualized()
        history = replay_cell(self.authority.versions(ACN), "result:revenue",
                              "2026-08-31")
        self.assertEqual(
            [(item["version"], item["kind"], item["value"]) for item in history],
            [(1, "estimate", "1464100000.00000000"),
             (2, "estimate", "1464100000.00000000"),
             (2, "actual", "1500000000.00000000")])
        self.assertIsNone(history[0]["superseded_by"])
        self.assertIsNotNone(history[1]["superseded_by"])


class RevisionTests(unittest.TestCase):
    """Moving an assumption is something a caller decides, with evidence.

    Nothing in this layer revises a forecast on its own. The owner's example is
    the shape of it: a company signs a large contract mid-quarter, someone
    decides that means the coming quarters are better, and the Claim that
    reports the contract is the evidence. The arithmetic follows; the judgement
    does not.
    """

    def setUp(self) -> None:
        self.store = DaltonStore(":memory:")
        self.addCleanup(self.store.close)
        self.authority = ForecastModelAuthority(self.store)
        self.first = self.authority.publish(model())
        self.claim = {"kind": "claim", "ref": "claim-version:contract-win",
                      "concept": None, "period_end": None, "accession": None}

    def revise(self, **kwargs):
        params = {
            "changes": [{
                "driver": f"concept:{REVENUE_CONCEPT}", "period": "2026-08-31",
                "value": "0.20", "because": "They signed a 500m contract in September.",
                "refs": [self.claim],
            }],
            "change_reason": "driver_event",
            "evidence_refs": [self.claim],
            "actor_ref": "automation:coverage-mission",
            "decision": "update_estimates",
        }
        params.update(kwargs)
        return revise_assumptions(self.first, params.pop("changes"), **params)

    def test_a_revision_moves_the_assumption_and_everything_that_rests_on_it(self):
        second = self.authority.publish(self.revise())
        self.assertEqual(second["version"], 2)
        self.assertEqual(second["change_reason"], "driver_event")
        self.assertEqual(second["decision"], "update_estimates")
        self.assertEqual(second["evidence_refs"][0]["ref"], "claim-version:contract-win")
        # 1,331.0 * 1.2, and the cost line follows it at eighty percent.
        self.assertEqual(Decimal(cells_of(second, "result:revenue")["2026-08-31"]["value"]),
                         Decimal("1597200000"))
        self.assertEqual(
            Decimal(cells_of(second, "result:cost_of_revenue")["2026-08-31"]["value"]),
            Decimal("1277760000"))
        assumption = next(
            item for item in second["assumptions"]
            if item["driver_ref"] == f"concept:{REVENUE_CONCEPT}"
            and item["period"]["end"] == "2026-08-31")
        self.assertEqual(Decimal(assumption["value"]), Decimal("0.2"))
        self.assertEqual(assumption["refs"], [self.claim])
        self.assertIn("500m contract", assumption["because"])

    def test_a_revision_that_cites_nothing_is_refused(self):
        with self.assertRaises(ForecastModelValidationError) as caught:
            self.revise(evidence_refs=[])
        self.assertIn("cite the evidence", str(caught.exception))

    def test_an_event_revision_must_say_what_was_decided(self):
        with self.assertRaises(ForecastModelValidationError) as caught:
            self.revise(decision=None)
        self.assertIn("what was decided", str(caught.exception))

    def test_a_quarter_the_filings_already_answered_is_not_revisable(self):
        table = build_model_inputs(filed_quarter(ledger()), spec())
        second = self.authority.publish(actualize_model(self.first, table))
        with self.assertRaises(ForecastModelValidationError) as caught:
            revise_assumptions(
                second, [{"driver": f"concept:{REVENUE_CONCEPT}",
                          "period": "2026-08-31", "value": "0.2",
                          "because": "Hindsight.", "refs": [self.claim]}],
                change_reason="human_revision", evidence_refs=[self.claim],
                actor_ref="human:analyst")
        self.assertIn("already answered", str(caught.exception))

    def test_a_human_revision_is_a_human_assumption(self):
        body = revise_assumptions(
            self.first, [{"driver": f"concept:{REVENUE_CONCEPT}",
                          "period": "2026-11-30", "value": "0.03",
                          "because": "Bookings rolled over; growth halves from here.",
                          "refs": [self.claim]}],
            change_reason="human_revision", evidence_refs=[self.claim],
            actor_ref="human:analyst")
        stored = self.authority.publish(body)
        assumption = next(
            item for item in stored["assumptions"]
            if item["period"]["end"] == "2026-11-30"
            and item["driver_ref"] == f"concept:{REVENUE_CONCEPT}")
        self.assertEqual(assumption["kind"], "human")
        self.assertEqual(assumption["provenance"]["decided_by"], "human:analyst")
        # The quarter before it is untouched: a revision revises what it names.
        self.assertEqual(cells_of(stored, "result:revenue")["2026-08-31"]["value"],
                         cells_of(self.first, "result:revenue")["2026-08-31"]["value"])

    def test_a_revision_that_changes_nothing_it_names_is_refused(self):
        with self.assertRaises(ForecastModelValidationError):
            self.revise(changes=[])
        with self.assertRaises(ForecastModelValidationError):
            self.revise(changes=[{"driver": "concept:nothing", "period": "2026-08-31",
                                  "value": "0.2", "because": "x", "refs": [self.claim]}])


def cells_all(record, ref):
    result = next(item for item in record["results"] if item["ref"] == ref)
    return list(result["cells"])


class FrozenContractTests(unittest.TestCase):
    """The things a later version of this module may not quietly change.

    A formula hash is what a stored line binds; a change to the semantics text
    that nobody noticed would make every line already written unreadable, and
    the failure would show up as "derived line must use a frozen formula" long
    after the change. So the hash is written down here in full.
    """

    def test_the_driver_formula_is_frozen(self):
        self.assertEqual(DRIVER_FORMULA_REF, "formula:driver-model:1")
        self.assertEqual(
            DRIVER_FORMULA_HASH,
            "908b13eede153e0fe64717249f01f24ac008a7d7fe8b412c0b6a1425b368fa25")

    def test_the_change_reasons_are_the_owner_s_five(self):
        self.assertEqual(CHANGE_REASONS, (
            "filing_actual", "driver_event", "assumption_review",
            "evidence_thicker", "human_revision"))

    def test_a_company_is_named_by_its_whole_ref(self):
        # Not the last colon-separated segment. Live already holds
        # company:sec-cik:001688568 beside company:sec-cik:0001467373 -- one of
        # them is missing a digit -- and two companies whose refs ended in the
        # same segment would write into each other's version chain.
        self.assertEqual(company_slug(ACN), "ff98fb9b6accc2576b2bfe8fa89a38d5")
        self.assertNotEqual(company_slug("company:sec-cik:1"),
                            company_slug("company:other:1"))
        self.assertEqual(len(company_slug(ACN)), 32)


class LostUpdateTests(unittest.TestCase):
    """Two callers, one chain, and the version each of them started from.

    The scenario is ordinary: something revises the first forecast quarter,
    and a caller still holding the version before that revises the second one.
    Publishing the second body would append a version that silently reverts the
    first revision -- and it would carry the second caller's ``change_reason``,
    so the record would say ``assumption_review`` for a change nobody made.
    """

    def setUp(self) -> None:
        self.store = DaltonStore(":memory:")
        self.addCleanup(self.store.close)
        self.authority = ForecastModelAuthority(self.store)
        self.first = self.authority.publish(model())
        self.claim = {"kind": "claim", "ref": "claim-version:x", "concept": None,
                      "period_end": None, "accession": None}

    def revision(self, prior, period, value, reason="assumption_review"):
        return revise_assumptions(
            prior, [{"driver": f"concept:{REVENUE_CONCEPT}", "period": period,
                     "value": value, "because": "A view was taken.",
                     "refs": [self.claim]}],
            change_reason=reason, evidence_refs=[self.claim],
            actor_ref="human:analyst")

    def test_a_body_computed_from_a_version_that_has_moved_is_refused(self):
        stale = self.revision(self.first, "2026-11-30", "0.05")
        self.authority.publish(self.revision(self.first, "2026-08-31", "0.20"))
        with self.assertRaises(ForecastModelConflict) as caught:
            self.authority.publish(stale)
        self.assertIn("computed from", str(caught.exception))
        # And the revision that did land is still what the chain says.
        latest = self.authority.latest(ACN)
        self.assertEqual(latest["version"], 2)
        self.assertEqual(
            Decimal(cells_of(latest, "result:revenue")["2026-08-31"]["value"]),
            Decimal("1597200000"))

    def test_recomputing_against_the_new_head_lands(self):
        second = self.authority.publish(self.revision(self.first, "2026-08-31", "0.20"))
        third = self.authority.publish(self.revision(second, "2026-11-30", "0.05"))
        self.assertEqual(third["version"], 3)
        # Both revisions are in it: the first quarter kept its 20%.
        self.assertEqual(
            Decimal(cells_of(third, "result:revenue")["2026-08-31"]["value"]),
            Decimal("1597200000"))
        self.assertEqual(
            Decimal(cells_of(third, "result:revenue")["2026-11-30"]["value"]),
            Decimal("1677060000"))

    def test_a_first_model_against_a_company_that_already_has_one_is_refused(self):
        self.authority.publish(self.revision(self.first, "2026-08-31", "0.20"))
        body = model(specification=spec(quarters=6))
        self.assertIsNone(body[SOURCE_VERSION_KEY])
        with self.assertRaises(ForecastModelConflict):
            self.authority.publish(body)

    def test_an_actualisation_of_a_version_that_has_moved_is_refused(self):
        table = build_model_inputs(filed_quarter(ledger()), spec())
        stale = actualize_model(self.first, table)
        self.authority.publish(self.revision(self.first, "2026-11-30", "0.05"))
        with self.assertRaises(ForecastModelConflict):
            self.authority.publish(stale)


class AgedOutRealisedTests(unittest.TestCase):
    """A quarter that falls off the realised list is still a quarter that happened."""

    def test_a_revision_does_not_delete_settled_quarters_it_no_longer_lists(self):
        store = DaltonStore(":memory:")
        self.addCleanup(store.close)
        authority = ForecastModelAuthority(store)
        record = authority.publish(model())
        table = build_model_inputs(filed_quarter(ledger()), spec())
        record = authority.publish(actualize_model(record, table))
        self.assertEqual([item["end"] for item in record["realised_periods"]],
                         ["2026-08-31"])
        # The cap is one, so the next filing pushes this quarter off the list.
        original = model_forecast_driver.MAX_REALISED_PERIODS
        model_forecast_driver.MAX_REALISED_PERIODS = 1
        self.addCleanup(setattr, model_forecast_driver,
                        "MAX_REALISED_PERIODS", original)
        second = filed_quarter(
            filed_quarter(ledger()), end="2026-11-30", start="2026-09-01",
            values={REVENUE_CONCEPT: "1600000000", COST_CONCEPT: "1280000000",
                    SGA_CONCEPT: "160000000", TAX_CONCEPT: "40000000"},
            accession="0001467373-26-000100")
        record = authority.publish(
            actualize_model(record, build_model_inputs(second, spec())))
        self.assertEqual([item["end"] for item in record["realised_periods"]],
                         ["2026-11-30"])
        revised = authority.publish(revise_assumptions(
            record, [{"driver": f"concept:{REVENUE_CONCEPT}", "period": "2027-02-28",
                      "value": "0.05", "because": "A view was taken.",
                      # Below every quarter this company has filed, so P17b's
                      # band invariant wants the sentence saying why.
                      "outside_band": {
                          "reason": "the renewal cohort behind the filed range "
                                    "does not repeat in this quarter"},
                      "refs": [{"kind": "claim", "ref": "claim-version:x",
                                "concept": None, "period_end": None,
                                "accession": None}]}],
            change_reason="assumption_review",
            evidence_refs=[{"kind": "claim", "ref": "claim-version:x",
                            "concept": None, "period_end": None,
                            "accession": None}],
            actor_ref="human:analyst"))
        cells = {(cell["period"]["end"], cell["kind"]): cell
                 for cell in cells_all(revised, "result:revenue")}
        # Both settled quarters survive, estimate and actual, even though only
        # the later one is still on the realised list.
        self.assertEqual(Decimal(cells[("2026-08-31", "actual")]["value"]),
                         Decimal("1500000000"))
        self.assertEqual(Decimal(cells[("2026-08-31", "estimate")]["value"]),
                         Decimal("1464100000"))
        self.assertTrue(cells[("2026-08-31", "estimate")]["superseded_by"])
        self.assertEqual(Decimal(cells[("2026-11-30", "actual")]["value"]),
                         Decimal("1600000000"))


class SignFlipTests(unittest.TestCase):
    """An average share of a base that changed sign is not a rate.

    A company that lost money in one quarter and made money in the next has a
    tax rate of minus something and plus something; their mean is an artefact
    of how far apart the loss and the profit were. The quarters come back
    unassumed and the lines that needed them say so.
    """

    def test_a_base_that_changes_sign_gets_no_share_assumption(self):
        losing = dict(SERIES)
        # Cost above revenue in the first two quarters: operating income is
        # negative, then positive.
        losing[COST_CONCEPT] = ("1100000000", "1210000000", "968000000", "1064800000")
        record = model(ledger(losing))
        self.assertEqual(
            [item for item in record["assumptions"]
             if item["driver_ref"] == f"concept:{TAX_CONCEPT}"], [])
        tax = next(item for item in record["results"]
                   if item["ref"] == "result:income_tax_expense")
        self.assertEqual(tax["status"], "unavailable")
        self.assertIn("no usable rate", tax["reason"])
        net = next(item for item in record["results"]
                   if item["ref"] == "result:net_income")
        self.assertEqual(net["status"], "unavailable")
        self.assertIn("income tax is not available", net["reason"])
        # Everything above the sign flip still stands.
        self.assertEqual(next(item for item in record["results"]
                              if item["ref"] == "result:operating_income")["status"],
                         "computed")


class BecauseTests(unittest.TestCase):
    def test_the_window_names_both_of_its_ends(self):
        growth = next(item for item in model()["assumptions"]
                      if item["driver_ref"] == f"concept:{REVENUE_CONCEPT}")
        # Three changes across four filed quarters: the window starts at the
        # first of them, not at the second.
        self.assertIn("between 2025-08-31 and 2026-05-31", growth["because"])
        self.assertIn("3 quarter-on-quarter changes", growth["because"])

    def test_the_growth_assumption_admits_it_is_blind_to_seasonality(self):
        # A reader of the record has to see this where the number is, not in a
        # report they may never open. Accenture's fiscal fourth quarter is
        # never in a 10-Q, so an average of the quarters that are will be wrong
        # for it by however unlike them it is.
        growth = next(item for item in model()["assumptions"]
                      if item["driver_ref"] == f"concept:{REVENUE_CONCEPT}")
        self.assertIn("no seasonality", growth["because"])


if __name__ == "__main__":
    unittest.main()
