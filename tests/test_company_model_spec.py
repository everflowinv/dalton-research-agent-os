"""P13al: the modelling frame is fixed; what fills it is judged and checked.

Two rules carry most of the weight here. A model line may only rest on a
concept the company actually filed -- a near miss is refused, not repaired,
because silently mapping us-gaap:Revenue onto us-gaap:Revenues is how a model
comes to cite a line that does not exist. And the income statement is always
required: whether the balance sheet matters is a judgement about the company,
whether you can forecast revenue and margin is not.
"""

from __future__ import annotations

import copy
import tempfile
import threading
import unittest
from pathlib import Path

from dalton_core.company_model_spec import (
    MAX_FORECAST_QUARTERS,
    MAX_REVENUE_DRIVERS,
    CompanyModelSpecError,
    build_prompt,
    forecast_statements,
    parse_response,
    spec_from_response,
    undisclosed_metrics,
)

ACN = "company:sec-cik:0001467373"
DECIDED_BY = "automation:company-model"

STATE = {
    "company_ref": ACN,
    "ticker": "ACN",
    "entity_name": "Accenture plc",
    "state_hash": "a" * 64,
    "filings": [{"accession": "0001467373-26-000031", "form": "10-Q",
                 "report_date": "2026-06-30"}],
    "concepts": [
        "us-gaap:Revenues",
        "us-gaap:CostOfRevenue",
        "us-gaap:SellingGeneralAndAdministrativeExpense",
        "us-gaap:OperatingIncomeLoss",
        "us-gaap:IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest",
        "us-gaap:IncomeTaxExpenseBenefit",
        "us-gaap:NetIncomeLoss",
        "us-gaap:Assets",
    ],
    "statements": {
        "income": [
            {"concept": "us-gaap:Revenues", "label": "Revenues", "level": 0,
             "parent_concept": None, "is_breakdown": False, "dimension_axis": None,
             "unit": "USD", "period_kind": "duration"},
            {"concept": "us-gaap:CostOfRevenue", "label": "Cost of services",
             "level": 1, "parent_concept": "us-gaap:Revenues",
             "is_breakdown": False, "dimension_axis": None,
             "unit": "USD", "period_kind": "duration"},
            {"concept": "us-gaap:SellingGeneralAndAdministrativeExpense",
             "label": "Sales, general and administrative", "level": 1,
             "parent_concept": "us-gaap:Revenues", "is_breakdown": False,
             "dimension_axis": None, "unit": "USD", "period_kind": "duration"},
            {"concept": "us-gaap:OperatingIncomeLoss", "label": "Operating income",
             "level": 0, "parent_concept": None, "is_breakdown": False,
             "dimension_axis": None, "unit": "USD", "period_kind": "duration"},
            {"concept": "us-gaap:IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest",
             "label": "Income before taxes", "level": 0, "parent_concept": None,
             "is_breakdown": False, "dimension_axis": None,
             "unit": "USD", "period_kind": "duration"},
            {"concept": "us-gaap:IncomeTaxExpenseBenefit", "label": "Income tax",
             "level": 1, "parent_concept": "us-gaap:NetIncomeLoss",
             "is_breakdown": False, "dimension_axis": None,
             "unit": "USD", "period_kind": "duration"},
            {"concept": "us-gaap:NetIncomeLoss", "label": "Net income",
             "level": 0, "parent_concept": None, "is_breakdown": False,
             "dimension_axis": None, "unit": "USD", "period_kind": "duration"},
            {"concept": "us-gaap:Revenues", "label": "Americas", "level": 1,
             "parent_concept": None, "is_breakdown": True,
             "dimension_axis": "srt:StatementGeographicalAxis",
             "unit": "USD", "period_kind": "duration"},
        ],
        "balance": [
            {"concept": "us-gaap:Assets", "label": "Total assets", "level": 0,
             "parent_concept": None, "is_breakdown": False, "dimension_axis": None,
             "unit": "USD", "period_kind": "instant"},
        ],
    },
}


def _statement_structure():
    accession = "0001467373-26-000031"
    def line(ref, role, concept, method, base=None, *, kind="filed"):
        return {
            "ref": ref, "role": role, "label": ref, "kind": kind,
            "concept": concept, "statement": "income", "unit": "usd",
            "period_kind": "duration", "annual_semantics": "sum_quarters",
            "forecast_method": method, "forecast_base_ref": base,
        }
    return {
        "schema_version": "0.1",
        "lines": [
            line("revenue", "revenue", "us-gaap:Revenues", "quarterly_growth"),
            line("cost", "cost_of_revenue", "us-gaap:CostOfRevenue",
                 "share_of_line", "revenue"),
            line("sga", "operating_expense",
                 "us-gaap:SellingGeneralAndAdministrativeExpense",
                 "share_of_line", "revenue"),
            line("operating", "operating_income", None, "formula", kind="derived"),
            line("pretax", "pretax_income",
                 "us-gaap:IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest",
                 "unavailable"),
            line("tax", "income_tax_expense", "us-gaap:IncomeTaxExpenseBenefit",
                 "unavailable"),
            line("net", "net_income", None, "formula", kind="derived"),
        ],
        "formulas": [{
            "output_ref": "operating", "operator": "sum",
            "terms": [
                {"line_ref": "revenue", "coefficient": "1"},
                {"line_ref": "cost", "coefficient": "-1"},
                {"line_ref": "sga", "coefficient": "-1"},
            ],
            "tie_out_concept": "us-gaap:OperatingIncomeLoss",
            "evidence_refs": [accession],
        }, {
            "output_ref": "net", "operator": "sum",
            "terms": [
                {"line_ref": "pretax", "coefficient": "1"},
                {"line_ref": "tax", "coefficient": "-1"},
            ],
            "tie_out_concept": "us-gaap:NetIncomeLoss",
            "evidence_refs": [accession],
        }],
    }


def _spec(**overrides):
    body = {
        "schema_version": "0.4",
        "revenue_anchor_concept": "us-gaap:Revenues",
        "assessment": (
            "Accenture is a people business: revenue is billable heads times "
            "realised rate, and margin turns on utilisation."
        ),
        "revenue_drivers": [
            {
                "ref": "billable-headcount", "label": "Billable headcount",
                "kind": "volume", "basis_concept": None, "unit": "headcount",
                "because": "Capacity is the binding constraint on delivery revenue.",
            },
            {
                "ref": "realised-rate", "label": "Realised rate per head",
                "kind": "price", "basis_concept": "us-gaap:Revenues",
                "unit": "USD",
                "because": "Pricing moves independently of headcount in this cycle.",
            },
        ],
        "expense_lines": [
            {
                "ref": "cost-of-services", "label": "Cost of services",
                "basis_concept": "us-gaap:CostOfRevenue",
                "behaviour": "variable_with_headcount",
                "driver_ref": "billable-headcount",
                "because": "Delivery payroll follows the billable base, not revenue.",
            },
            {
                "ref": "sga", "label": "Sales, general and administrative",
                "basis_concept": "us-gaap:SellingGeneralAndAdministrativeExpense",
                "behaviour": "semi_variable", "driver_ref": None,
                "because": "Partly sales capacity, partly a fixed corporate base.",
            },
        ],
        "forecast_statements": [
            {"statement": "income", "importance": "required",
             "because": "Revenue and margin are the question."},
            {"statement": "balance", "importance": "supporting",
             "because": "Capital light; the balance sheet is working capital and cash."},
            {"statement": "cash", "importance": "required",
             "because": "Free cash flow funds the buyback the thesis rests on."},
        ],
        "operating_metrics": [
            {"ref": "new-bookings", "label": "New bookings", "unit": "USD",
             "periodicity": "quarterly", "disclosed": True,
             "because": "The market trades this print ahead of revenue."},
            {"ref": "utilisation", "label": "Utilisation", "unit": "percent",
             "periodicity": "quarterly", "disclosed": False,
             "because": "Margin turns on it and it has to be inferred from headcount."},
        ],
        "horizon": {
            "historical_quarters": 12, "forecast_quarters": 8,
            "because": "Three years spans the last demand cycle.",
        },
        "financial_statement_structure": _statement_structure(),
        "cash_flow_companion": {
            "schema_version": "0.1",
            "lines": [
                {"role": "operating_cash_flow", "concept": None,
                 "forecast_method": "unavailable", "forecast_base_ref": None,
                 "because": "This fixture supplies no exact operating cash-flow concept."},
                {"role": "capital_expenditure", "concept": None,
                 "forecast_method": "unavailable", "forecast_base_ref": None,
                 "because": "This fixture supplies no exact capital-expenditure concept."},
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
    body.update(overrides)
    return body


class CompanyModelSpecTests(unittest.TestCase):
    # A sentinel, not ``or``: "" and None are responses this must refuse, and
    # a falsy default would quietly test the good specification instead.
    _UNSET = object()

    def verify(self, body=_UNSET, state=None):
        return spec_from_response(
            state or STATE, _spec() if body is self._UNSET else body,
            decided_by=DECIDED_BY)

    def test_a_well_formed_specification_verifies_and_hashes(self):
        spec = self.verify()
        self.assertEqual(spec["company_ref"], ACN)
        self.assertEqual(spec["state_hash"], STATE["state_hash"])
        self.assertEqual(spec["decided_by"], DECIDED_BY)
        self.assertEqual(len(spec["content_hash"]), 64)
        # Same judgement, same hash: a spec is identified by what it says.
        self.assertEqual(self.verify()["content_hash"], spec["content_hash"])

    def test_only_syntax_and_schema_text_bounds_are_typed_for_repair(self):
        with self.assertRaises(CompanyModelSpecError) as malformed:
            self.verify("not json")
        self.assertEqual(malformed.exception.code, "format")

        overlong = _spec(assessment="x" * 1201)
        with self.assertRaises(CompanyModelSpecError) as length:
            self.verify(overlong)
        self.assertEqual(length.exception.code, "text_length")

        semantic = _spec(revenue_anchor_concept="us-gaap:NotFiled")
        with self.assertRaises(CompanyModelSpecError) as authority:
            self.verify(semantic)
        self.assertEqual(authority.exception.code, "semantic")
        missing_ref = _spec()
        missing_ref["revenue_drivers"][0]["ref"] = ""
        with self.assertRaises(CompanyModelSpecError) as slot:
            self.verify(missing_ref)
        self.assertEqual(slot.exception.code, "semantic")
        overlong_ref = _spec()
        overlong_ref["revenue_drivers"][0]["ref"] = "x" * 61
        with self.assertRaises(CompanyModelSpecError) as ref:
            self.verify(overlong_ref)
        self.assertEqual(ref.exception.code, "semantic")

    def test_a_line_may_not_rest_on_a_concept_the_company_never_filed(self):
        for field, entry in (
            ("revenue_drivers", {"basis_concept": "us-gaap:Revenue"}),
            ("expense_lines", {"basis_concept": "acn:MadeUpExpense"}),
        ):
            with self.subTest(field=field):
                body = _spec()
                body[field][0].update(entry)
                with self.assertRaises(CompanyModelSpecError) as caught:
                    self.verify(body)
                self.assertIn("not a concept", str(caught.exception))

    def test_a_line_with_no_filed_counterpart_uses_null(self):
        spec = self.verify()
        self.assertIsNone(spec["revenue_drivers"][0]["basis_concept"])

    def test_the_income_statement_is_always_required(self):
        for importance in ("supporting", "not_material"):
            with self.subTest(importance=importance):
                body = _spec()
                body["forecast_statements"][0]["importance"] = importance
                with self.assertRaises(CompanyModelSpecError) as caught:
                    self.verify(body)
                self.assertIn("income statement is always required",
                              str(caught.exception))

    def test_the_other_statements_are_the_company_judgement(self):
        body = _spec()
        body["forecast_statements"][1]["importance"] = "not_material"
        spec = self.verify(body)
        self.assertEqual(forecast_statements(spec), ["income", "cash"])

    def test_cash_companion_selects_company_concepts_and_exact_income_base(self):
        state = copy.deepcopy(STATE)
        selected = {
            "operating_cash_flow": "acme:CashGeneratedFromOperations",
            "capital_expenditure": "acme:PurchasesOfEquipment",
        }
        state["concepts"].extend(selected.values())
        state["statements"]["cash"] = [
            {"concept": concept, "label": role, "level": 0,
             "parent_concept": None, "is_breakdown": False, "dimension_axis": None,
             "unit": "USD", "period_kind": "duration"}
            for role, concept in selected.items()
        ]
        body = _spec()
        body["cash_flow_companion"]["lines"] = [
            {"role": role, "concept": concept,
             "forecast_method": "share_of_line", "forecast_base_ref": "revenue",
             "because": f"{role} follows this company's revenue base."}
            for role, concept in selected.items()
        ]
        verified = self.verify(body, state=state)
        self.assertEqual(
            {item["role"]: item["concept"]
             for item in verified["cash_flow_companion"]["lines"]},
            selected,
        )

    def test_cash_companion_refuses_a_source_and_base_in_different_units(self):
        state = copy.deepcopy(STATE)
        concept = "acme:CashGeneratedFromOperations"
        state["concepts"].append(concept)
        state["statements"]["cash"] = [{
            "concept": concept, "label": "Cash generated", "level": 0,
            "parent_concept": None, "is_breakdown": False, "dimension_axis": None,
            "unit": "EUR", "period_kind": "duration",
        }]
        body = _spec()
        body["cash_flow_companion"]["lines"][0].update(
            concept=concept, forecast_method="share_of_line", forecast_base_ref="revenue")
        with self.assertRaisesRegex(CompanyModelSpecError, "units differ"):
            self.verify(body, state=state)

    def test_cash_companion_refuses_one_concept_for_both_cash_roles(self):
        state = copy.deepcopy(STATE)
        concept = "acme:CashGeneratedFromOperations"
        state["concepts"].append(concept)
        state["statements"]["cash"] = [{
            "concept": concept, "label": "Cash generated", "level": 0,
            "parent_concept": None, "is_breakdown": False, "dimension_axis": None,
            "unit": "USD", "period_kind": "duration",
        }]
        body = _spec()
        for line in body["cash_flow_companion"]["lines"]:
            line.update(
                concept=concept, forecast_method="share_of_line",
                forecast_base_ref="revenue")
        with self.assertRaisesRegex(CompanyModelSpecError, "one filed concept"):
            self.verify(body, state=state)

    def test_cash_companion_refuses_a_known_capex_concept_as_operating_cash(self):
        concept = "us-gaap:PaymentsToAcquirePropertyPlantAndEquipment"
        state = copy.deepcopy(STATE)
        state["concepts"].append(concept)
        state["statements"]["cash"] = [{
            "concept": concept, "label": "Capital expenditure", "level": 0,
            "parent_concept": None, "is_breakdown": False, "dimension_axis": None,
            "unit": "USD", "period_kind": "duration",
        }]
        body = _spec()
        body["cash_flow_companion"]["lines"][0].update(
            concept=concept, forecast_method="share_of_line",
            forecast_base_ref="revenue")
        with self.assertRaisesRegex(CompanyModelSpecError, "other-role semantics"):
            self.verify(body, state=state)

    def test_every_statement_must_be_answered_for(self):
        body = _spec()
        body["forecast_statements"] = body["forecast_statements"][:2]
        with self.assertRaises(CompanyModelSpecError) as caught:
            self.verify(body)
        self.assertIn("missing cash", str(caught.exception))

    def test_a_statement_answered_twice_is_refused(self):
        body = _spec()
        body["forecast_statements"].append(dict(body["forecast_statements"][0]))
        with self.assertRaises(CompanyModelSpecError):
            self.verify(body)

    def test_an_expense_may_only_follow_a_driver_that_exists(self):
        body = _spec()
        body["expense_lines"][0]["driver_ref"] = "some-other-thing"
        with self.assertRaises(CompanyModelSpecError) as caught:
            self.verify(body)
        self.assertIn("names no revenue driver", str(caught.exception))

    def test_refs_are_slugs_and_unique_within_their_list(self):
        for field, ref in (("revenue_drivers", "Billable Headcount"),
                           ("expense_lines", "cost of services")):
            with self.subTest(ref=ref):
                body = _spec()
                body[field][0]["ref"] = ref
                with self.assertRaises(CompanyModelSpecError):
                    self.verify(body)
        body = _spec()
        body["revenue_drivers"][1]["ref"] = body["revenue_drivers"][0]["ref"]
        with self.assertRaises(CompanyModelSpecError) as caught:
            self.verify(body)
        self.assertIn("used twice", str(caught.exception))

    def test_a_model_needs_a_driver_and_a_cost(self):
        for field in ("revenue_drivers", "expense_lines"):
            with self.subTest(field=field):
                body = _spec()
                body[field] = []
                with self.assertRaises(CompanyModelSpecError):
                    self.verify(body)

    def test_the_lists_are_bounded(self):
        body = _spec()
        driver = body["revenue_drivers"][0]
        body["revenue_drivers"] = [
            {**copy.deepcopy(driver), "ref": f"driver-{index}"}
            for index in range(MAX_REVENUE_DRIVERS + 1)
        ]
        with self.assertRaises(CompanyModelSpecError):
            self.verify(body)

    def test_every_entry_carries_a_reason(self):
        for field in ("revenue_drivers", "expense_lines", "operating_metrics"):
            with self.subTest(field=field):
                body = _spec()
                body[field][0]["because"] = "   "
                with self.assertRaises(CompanyModelSpecError):
                    self.verify(body)
        body = _spec()
        body["assessment"] = ""
        with self.assertRaises(CompanyModelSpecError):
            self.verify(body)

    def test_the_horizon_is_bounded_at_both_ends(self):
        for horizon in ({"historical_quarters": 0}, {"historical_quarters": 40},
                        {"forecast_quarters": 1},
                        {"forecast_quarters": MAX_FORECAST_QUARTERS + 1},
                        {"forecast_quarters": True}):
            with self.subTest(horizon=horizon):
                body = _spec()
                body["horizon"].update(horizon)
                with self.assertRaises(CompanyModelSpecError):
                    self.verify(body)

    def test_an_undisclosed_metric_is_named_as_one(self):
        spec = self.verify()
        self.assertEqual([item["ref"] for item in undisclosed_metrics(spec)],
                         ["utilisation"])
        body = _spec()
        body["operating_metrics"][0]["disclosed"] = "yes"
        with self.assertRaises(CompanyModelSpecError):
            self.verify(body)

    def test_a_company_with_no_filings_cannot_be_modelled(self):
        with self.assertRaises(CompanyModelSpecError) as caught:
            self.verify(state={**STATE, "concepts": []})
        self.assertIn("no filed statements", str(caught.exception))

    def test_the_decider_is_named_in_a_known_namespace(self):
        with self.assertRaises(CompanyModelSpecError):
            spec_from_response(STATE, _spec(), decided_by="whoever")

    def test_a_response_that_is_not_a_specification_is_refused(self):
        for response in ("", "no thanks", "```json\n[]\n```", None, 7):
            with self.subTest(response=response):
                with self.assertRaises(CompanyModelSpecError):
                    self.verify(response)

    def test_a_fenced_response_is_read(self):
        import json

        fenced = "```json\n" + json.dumps(_spec()) + "\n```"
        self.assertEqual(parse_response(fenced)["schema_version"], "0.4")

    def test_the_wrong_schema_version_is_refused(self):
        with self.assertRaises(CompanyModelSpecError):
            self.verify(_spec(schema_version="0.1"))

    def test_the_calculation_anchor_is_separate_from_economic_drivers(self):
        body = _spec(revenue_drivers=[{
            "ref": "heads", "label": "Billable heads", "kind": "volume",
            "basis_concept": None, "unit": "headcount",
            "because": "Capacity moves delivered revenue.",
        }], expense_lines=[{
            **_spec()["expense_lines"][0], "driver_ref": "heads",
        }])
        verified = self.verify(body)
        self.assertEqual(verified["revenue_anchor_concept"], "us-gaap:Revenues")
        self.assertIsNone(verified["revenue_drivers"][0]["basis_concept"])

    def test_an_anchor_not_in_the_filed_vocabulary_is_refused(self):
        with self.assertRaises(CompanyModelSpecError):
            self.verify(_spec(revenue_anchor_concept="us-gaap:SalesRevenueNet"))

    def test_a_filed_non_revenue_concept_is_refused_as_the_anchor(self):
        with self.assertRaises(CompanyModelSpecError):
            self.verify(_spec(revenue_anchor_concept="us-gaap:Assets"))

    def test_a_dimensional_revenue_member_is_not_a_consolidated_anchor(self):
        state = {**STATE, "statements": {
            **STATE["statements"],
            "income": [row for row in STATE["statements"]["income"]
                       if row["concept"] != "us-gaap:Revenues"
                       or row["is_breakdown"]],
        }}
        with self.assertRaises(CompanyModelSpecError):
            self.verify(_spec(), state=state)

    def test_the_prompt_carries_the_frame_and_the_company(self):
        prompt = build_prompt(STATE)
        self.assertIn("us-gaap:CostOfRevenue", prompt)
        self.assertIn("income statement is always required", prompt)
        self.assertIn("new bookings", prompt.lower())
        # The structure is written as a table, not as JSON: the same content
        # as objects costs three times the bytes, and the router reserves
        # budget against the size of the prompt.
        self.assertIn("[income]", prompt)
        self.assertIn("-1\tus-gaap:CostOfRevenue\tCost of services\t"
                      "us-gaap:Revenues\t", prompt)
        self.assertIn("*1\tus-gaap:Revenues\tAmericas\t\t"
                      "srt:StatementGeographicalAxis", prompt)
        # And the concept list is not repeated: every concept is in the table.
        self.assertNotIn('"concepts"', prompt)

    def test_prompt_places_a_derived_lines_filed_tie_only_on_its_formula(self):
        prompt = build_prompt(STATE)
        self.assertIn("Every derived line sets concept to null", prompt)
        self.assertIn(
            '"ref":"operating-income","kind":"derived","concept":null',
            prompt,
        )
        self.assertIn(
            '"output_ref":"operating-income",'
            '"tie_out_concept":"us-gaap:OperatingIncomeLoss"',
            prompt,
        )
        self.assertIn(
            "A derived line's historical filed tie belongs only in its "
            "formula.tie_out_concept.",
            prompt,
        )


if __name__ == "__main__":
    unittest.main()


class CompanyModelSpecStorageTests(unittest.TestCase):
    """A specification is kept the way a plan is: once, bound to its state."""

    def setUp(self) -> None:
        from dalton_core.coverage_mission import CoverageMissionAuthority
        from dalton_core.store import DaltonStore
        from tests.p9a_fixtures import bootstrap_method_authorities, mission_params

        self.store = DaltonStore(":memory:")
        self.addCleanup(self.store.close)
        state = bootstrap_method_authorities(self.store)
        self.missions = CoverageMissionAuthority(self.store)
        params = mission_params(state)
        self.mission = self.missions.create_mission(params.pop("mission_ref"), **params)

    def record(self, spec=None, **kwargs):
        return self.missions.record_company_model_spec(
            spec or spec_from_response(STATE, _spec(), decided_by=DECIDED_BY),
            mission_version_ref=self.mission["id"], **kwargs)

    def test_a_specification_is_stored_whole_and_read_back(self):
        stored = self.record(model_profile_ref="profile:model-spec")
        self.assertEqual(stored["status"], "fresh")
        self.assertEqual(stored["company_ref"], ACN)
        held = self.missions.latest_company_model_spec(ACN)
        self.assertEqual(held["revenue_drivers"][0]["ref"], "billable-headcount")
        self.assertEqual(held["horizon"]["forecast_quarters"], 8)
        self.assertEqual(
            [item["statement"] for item in held["forecast_statements"]],
            ["income", "balance", "cash"])
        self.assertEqual(held["model_profile_ref"], "profile:model-spec")
        self.assertEqual(held["schema_version"], "0.4")
        self.assertEqual(
            held["financial_statement_structure"], _statement_structure())

    def test_deciding_twice_about_an_unchanged_disclosure_is_one_decision(self):
        first = self.record()
        again = self.record()
        self.assertEqual(again["status"], "duplicate")
        self.assertEqual(again["spec_id"], first["spec_id"])
        self.assertEqual(len(self.missions.company_model_specs(ACN)), 1)

    def test_a_new_disclosure_gets_its_own_specification(self):
        self.record()
        moved = spec_from_response({**STATE, "state_hash": "b" * 64}, _spec(),
                                   decided_by=DECIDED_BY)
        self.assertEqual(self.record(moved)["status"], "fresh")
        self.assertEqual(len(self.missions.company_model_specs(ACN)), 2)

    def test_a_new_task_contract_gets_a_new_spec_for_the_same_state(self):
        from dalton_core.store import content_hash

        old = spec_from_response(STATE, _spec(), decided_by=DECIDED_BY)
        old = {**old, "task_hash": "b" * 64}
        old.pop("content_hash")
        old["content_hash"] = content_hash(old)
        self.record(old)
        current = self.record()
        self.assertEqual(current["status"], "fresh")
        self.assertEqual(len(self.missions.company_model_specs(ACN)), 2)

    def test_an_incomplete_specification_is_refused(self):
        from dalton_core.coverage_mission import CoverageMissionValidationError

        verified = spec_from_response(STATE, _spec(), decided_by=DECIDED_BY)
        for field in ("company_ref", "state_hash", "horizon", "content_hash"):
            with self.subTest(field=field):
                broken = {**verified, field: None}
                with self.assertRaises(CoverageMissionValidationError):
                    self.record(broken)

    def test_structure_cannot_be_changed_after_spec_validation(self):
        from dalton_core.coverage_mission import CoverageMissionValidationError

        verified = spec_from_response(STATE, _spec(), decided_by=DECIDED_BY)
        verified["financial_statement_structure"]["lines"][0]["label"] = "Changed"
        with self.assertRaisesRegex(CoverageMissionValidationError, "content_hash"):
            self.record(verified)

    def test_specifications_cannot_be_rewritten(self):
        import sqlite3

        self.record()
        for statement in (
            "UPDATE coverage_mission_company_model_specs SET assessment='x'",
            "DELETE FROM coverage_mission_company_model_specs",
        ):
            with self.subTest(statement=statement):
                with self.assertRaises(sqlite3.IntegrityError):
                    self.store.connection.execute(statement)

    def test_legacy_rows_keep_their_identity_and_hash_through_migration(self):
        from dalton_core.coverage_mission import CoverageMissionAuthority
        from dalton_core.store import DaltonStore, canonical_json

        legacy = DaltonStore(":memory:")
        self.addCleanup(legacy.close)
        legacy.connection.executescript("""
            CREATE TABLE coverage_mission_company_model_specs (
                spec_id TEXT PRIMARY KEY, company_ref TEXT NOT NULL,
                mission_version_ref TEXT NOT NULL, state_hash TEXT NOT NULL,
                assessment TEXT NOT NULL, revenue_drivers_json TEXT NOT NULL,
                expense_lines_json TEXT NOT NULL, forecast_statements_json TEXT NOT NULL,
                operating_metrics_json TEXT NOT NULL, horizon_json TEXT NOT NULL,
                task_hash TEXT NOT NULL, model_profile_ref TEXT, work_order_ref TEXT,
                decided_by TEXT NOT NULL, created_at TEXT NOT NULL,
                content_hash TEXT NOT NULL, UNIQUE(company_ref,state_hash));
        """)
        values = (
            "company-model-spec:legacy", ACN, "coverage-mission-version:legacy",
            "a" * 64, "legacy assessment", canonical_json([]), canonical_json([]),
            canonical_json([]), canonical_json([]), canonical_json({}), "b" * 64,
            None, None, "automation:legacy", "2026-09-09T00:00:00+00:00", "c" * 64,
        )
        legacy.connection.execute(
            "INSERT INTO coverage_mission_company_model_specs VALUES(" +
            ",".join("?" for _ in values) + ")", values)
        legacy.connection.commit()
        authority = CoverageMissionAuthority(legacy)
        held = authority.latest_company_model_spec(ACN)
        self.assertEqual(held["spec_id"], "company-model-spec:legacy")
        self.assertEqual(held["content_hash"], "c" * 64)
        self.assertNotIn("revenue_anchor_concept", held)

    def test_schema_places_the_anchor_only_on_model_specs(self):
        plan_columns = {row[1] for row in self.store.connection.execute(
            "PRAGMA table_info(coverage_mission_research_plans)")}
        spec_columns = {row[1] for row in self.store.connection.execute(
            "PRAGMA table_info(coverage_mission_company_model_specs)")}
        self.assertNotIn("revenue_anchor_json", plan_columns)
        self.assertIn("revenue_anchor_json", spec_columns)
        self.assertIn("metadata_json", spec_columns)

    def test_concurrent_same_contract_inserts_are_idempotent(self):
        from dalton_core.coverage_mission import CoverageMissionAuthority
        from dalton_core.store import DaltonStore

        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "core.sqlite")
            initial = DaltonStore(path)
            CoverageMissionAuthority(initial)
            initial.close()
            spec = spec_from_response(STATE, _spec(), decided_by=DECIDED_BY)
            barrier = threading.Barrier(2)
            results: list[str] = []
            errors: list[BaseException] = []

            def record() -> None:
                store = DaltonStore(path)
                try:
                    authority = CoverageMissionAuthority(store)
                    barrier.wait()
                    result = authority.record_company_model_spec(
                        spec, mission_version_ref="coverage-mission-version:test")
                    results.append(result["status"])
                except BaseException as exc:
                    errors.append(exc)
                finally:
                    store.close()

            threads = [threading.Thread(target=record) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=10)
            self.assertFalse(errors)
            self.assertEqual(sorted(results), ["duplicate", "fresh"])
            check = DaltonStore(path)
            try:
                self.assertEqual(
                    len(CoverageMissionAuthority(check).company_model_specs(ACN)), 1)
            finally:
                check.close()
