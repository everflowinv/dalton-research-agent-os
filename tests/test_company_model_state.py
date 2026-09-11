"""P13al: the ledger projected down to the structure a model is decided from.

Values move every quarter; the vocabulary is what a model rests on. This drops
the numbers, deduplicates concepts across filings and periods, and hashes what
is left -- so a specification is bound to the disclosure it was written for and
becomes history when a filing introduces a line nobody had seen.
"""

from __future__ import annotations

import unittest

from dalton_core.company_model_state import (
    DEFAULT_NUMERIC_CONTEXT_POLICY,
    MAX_CONCEPTS_PER_STATEMENT,
    CompanyModelPromptBudgetError,
    CompanyModelStateError,
    _numeric_line_authority,
    build_company_model_state,
    validate_numeric_context_policy,
)
from dalton_core.company_model_spec import build_prompt
from dalton_core.company_model_cli import MAX_INPUT_TOKENS
from dalton_core.coverage_mission import CoverageMissionAuthority
from dalton_core.store import DaltonStore
from tests.p9a_fixtures import bootstrap_method_authorities, mission_params

ACN = "company:sec-cik:0001467373"


def _line(concept, **overrides):
    line = {
        "statement": "income", "concept": concept, "label": concept.split(":")[-1],
        "level": 1, "parent_concept": None, "is_breakdown": False,
        "dimension_axis": None, "dimension_member": None,
        "period_start": "2026-04-01", "period_end": "2026-06-30",
        "value": "1000", "unit": "USD", "balance": "credit",
    }
    line.update(overrides)
    return line


class CompanyModelStateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = DaltonStore(":memory:")
        self.addCleanup(self.store.close)
        self.state = bootstrap_method_authorities(self.store)
        self.missions = CoverageMissionAuthority(self.store)
        params = mission_params(self.state)
        self.mission = self.missions.create_mission(params.pop("mission_ref"), **params)
        self.authorization = self.missions.authorize_sec_lane(
            company_ref=ACN, ticker="ACN", actor_ref="automation:coverage-mission",
            mission_version_ref=self.mission["id"],
            mission_version_hash=self.mission["content_hash"],
        )

    def ingest(self, accession, lines, *, report_date="2026-06-30",
               filed=None, form="10-Q", attempt=0):
        dispatch = self.missions.queue_statement_dispatch(
            authorization=self.authorization, form=form, attempt=attempt)
        self.missions.mark_statement_dispatch_launched(
            dispatch["dispatch_id"], f"sec-financials-run:{attempt:024d}")
        self.missions.record_statement_observation(
            dispatch_id=dispatch["dispatch_id"],
            observation={
                "schema_version": "0.1", "cik": "0001467373",
                "entity_name": "Accenture plc",
                "filings": [{
                    "accession": accession, "form": form,
                    "filed": filed or report_date,
                    "report_date": report_date, "lines": lines,
                }],
                "source_record_refs": ["raw-sink:" + "c" * 64],
                "next_cursor": None, "provider_status": 200,
            },
            governance_ref="g", governance_hash="b" * 64)
        self.missions.settle_statement_dispatch(
            dispatch["dispatch_id"], outcome="succeeded")

    def test_a_company_with_no_statements_cannot_be_modelled(self):
        with self.assertRaises(CompanyModelStateError):
            build_company_model_state(self.missions, ACN)

    def test_the_projection_keeps_structure_and_drops_values(self):
        self.ingest("0001467373-26-000031", [
            _line("us-gaap:Revenues", level=0),
            _line("us-gaap:CostOfRevenue", level=1,
                  parent_concept="us-gaap:Revenues"),
            _line("us-gaap:Assets", statement="balance", period_start=None),
        ])
        state = build_company_model_state(self.missions, ACN, ticker="ACN")
        self.assertEqual(state["company_ref"], ACN)
        self.assertEqual(state["ticker"], "ACN")
        self.assertEqual(state["entity_name"], "Accenture plc")
        self.assertEqual(state["concepts"], [
            "us-gaap:Assets", "us-gaap:CostOfRevenue", "us-gaap:Revenues"])
        income = state["statements"]["income"]
        self.assertEqual(income[1]["parent_concept"], "us-gaap:Revenues")
        self.assertNotIn("value", income[0])
        self.assertNotIn("period_end", income[0])
        self.assertEqual(len(state["state_hash"]), 64)
        numeric = state["numeric_context"]
        self.assertEqual(numeric["policy"], DEFAULT_NUMERIC_CONTEXT_POLICY)
        self.assertEqual(numeric["included_cells"], 3)
        self.assertLessEqual(numeric["prompt_bytes"], numeric["prompt_byte_limit"])
        self.assertEqual(numeric["prompt_bytes"], len(build_prompt(state).encode("utf-8")))
        revenue = next(
            item for item in numeric["cells"]
            if item["concept"] == "us-gaap:Revenues"
        )
        filing = self.missions.statement_filings(ACN)[0]
        raw_line = next(
            item for item in self.missions.statement_lines(filing["ingest_id"])
            if item["concept"] == "us-gaap:Revenues"
        )
        self.assertEqual(revenue["value"], "1000")
        self.assertEqual(revenue["filing_content_hash"], filing["content_hash"])
        self.assertEqual(
            revenue["line_content_hash"],
            _numeric_line_authority(raw_line)["content_hash"],
        )
        self.assertEqual(revenue["filing_form"], "10-Q")

    def test_the_same_concept_across_periods_appears_once(self):
        self.ingest("0001467373-26-000031", [
            _line("us-gaap:Revenues", period_start="2026-04-01"),
            _line("us-gaap:Revenues", period_start="2025-09-01"),
            _line("us-gaap:Revenues", period_start="2025-04-01"),
        ])
        state = build_company_model_state(self.missions, ACN)
        self.assertEqual(len(state["statements"]["income"]), 1)

    def test_a_segment_breakdown_is_kept_and_marked(self):
        self.ingest("0001467373-26-000031", [
            _line("us-gaap:Revenues"),
            _line("us-gaap:Revenues", is_breakdown=True,
                  dimension_axis="srt:StatementGeographicalAxis",
                  dimension_member="acn:AmericasMember"),
        ])
        state = build_company_model_state(self.missions, ACN)
        income = state["statements"]["income"]
        self.assertEqual(len(income), 2)
        self.assertTrue(income[1]["is_breakdown"])
        self.assertEqual(income[1]["dimension_axis"],
                         "srt:StatementGeographicalAxis")

    def test_the_newest_filing_wins_when_a_line_was_restructured(self):
        self.ingest("0001467373-26-000030", [
            _line("us-gaap:CostOfRevenue", label="Cost of services", level=2)],
            report_date="2026-03-31")
        self.ingest("0001467373-26-000031", [
            _line("us-gaap:CostOfRevenue", label="Cost of services, net", level=1)],
            report_date="2026-06-30", attempt=1)
        state = build_company_model_state(self.missions, ACN)
        line = state["statements"]["income"][0]
        self.assertEqual(line["label"], "Cost of services, net")
        self.assertEqual(line["level"], 1)

    def test_a_new_disclosure_moves_the_state_hash(self):
        self.ingest("0001467373-26-000030", [_line("us-gaap:Revenues")],
                    report_date="2026-03-31")
        before = build_company_model_state(self.missions, ACN)["state_hash"]
        self.assertEqual(
            build_company_model_state(self.missions, ACN)["state_hash"], before)
        self.ingest("0001467373-26-000031",
                    [_line("us-gaap:Revenues"), _line("acn:NewBookings")],
                    report_date="2026-06-30", attempt=1)
        after = build_company_model_state(self.missions, ACN)["state_hash"]
        self.assertNotEqual(after, before)

    def test_a_runaway_filing_is_capped_rather_than_sent_whole(self):
        self.ingest("0001467373-26-000031", [
            _line(f"acn:Concept{index}")
            for index in range(MAX_CONCEPTS_PER_STATEMENT + 50)
        ])
        state = build_company_model_state(self.missions, ACN)
        self.assertEqual(len(state["statements"]["income"]),
                         MAX_CONCEPTS_PER_STATEMENT)
        self.assertLess(len(build_prompt(state).encode("utf-8")), MAX_INPUT_TOKENS)

    def test_full_numeric_cell_budget_fits_the_existing_prompt_limit(self):
        lines = []
        for index in range(150):
            lines.extend([
                _line(f"acn:Concept{index}", period_start="2026-04-01",
                      period_end="2026-06-30"),
                _line(f"acn:Concept{index}", period_start="2026-01-01",
                      period_end="2026-03-31"),
            ])
        self.ingest("0001467373-26-000031", lines)
        state = build_company_model_state(self.missions, ACN)
        self.assertEqual(state["numeric_context"]["included_cells"], 300)
        self.assertLess(len(build_prompt(state).encode("utf-8")), MAX_INPUT_TOKENS)

    def test_numeric_context_preserves_quarter_and_annual_windows_without_fiscal_inference(self):
        self.ingest("0001467373-26-000031", [
            _line("us-gaap:WeightedAverageNumberOfDilutedSharesOutstanding",
                  period_start="2026-04-01", period_end="2026-06-30",
                  value="100.2500", unit="shares"),
            _line("us-gaap:WeightedAverageNumberOfDilutedSharesOutstanding",
                  period_start="2025-07-01", period_end="2026-06-30",
                  value="99.8750", unit="shares"),
        ])
        cells = build_company_model_state(self.missions, ACN)["numeric_context"]["cells"]
        by_start = {item["period_start"]: item for item in cells}
        self.assertEqual(by_start["2026-04-01"]["period_shape"], "quarter")
        self.assertEqual(by_start["2026-04-01"]["duration_days"], 91)
        self.assertEqual(by_start["2025-07-01"]["period_shape"], "cumulative")
        self.assertEqual(by_start["2025-07-01"]["duration_days"], 365)
        self.assertEqual(by_start["2025-07-01"]["value"], "99.8750")
        self.assertNotIn("fiscal_year", by_start["2025-07-01"])

    def test_latest_filing_wins_but_same_filing_conflicts_remain_explicit(self):
        concept = "us-gaap:OtherNonoperatingIncomeExpense"
        self.ingest("0001467373-26-000030", [
            _line(concept, value="10.00")], report_date="2026-03-31",
                    filed="2026-04-10")
        self.ingest("0001467373-26-000031", [
            _line(concept, value="15.00"),
            _line(concept, value="16.00"),
        ], report_date="2026-06-30", filed="2026-07-10", attempt=1)
        cells = [
            item for item in build_company_model_state(
                self.missions, ACN,
            )["numeric_context"]["cells"]
            if item["concept"] == concept
        ]
        self.assertEqual([item["value"] for item in cells], ["15.00", "16.00"])
        self.assertEqual({item["status"] for item in cells}, {"ambiguous"})
        self.assertEqual(len({item["ambiguity_ref"] for item in cells}), 1)
        self.assertNotIn("10.00", {item["value"] for item in cells})
        bounded = build_company_model_state(
            self.missions, ACN,
            numeric_context_policy={"max_periods_per_series": 1,
                                    "max_total_cells": 1},
        )["numeric_context"]
        self.assertEqual(bounded["included_cells"], 0)
        self.assertEqual(bounded["omitted_by_total_limit"], 2)

    def test_prompt_budget_omits_a_whole_ambiguity_group(self):
        concept = "us-gaap:OtherNonoperatingIncomeExpense"
        self.ingest("0001467373-26-000031", [
            _line(concept, value="15.00"),
            _line(concept, value="16.00"),
        ])
        complete = build_company_model_state(self.missions, ACN)
        base = complete["numeric_context"]["base_prompt_bytes"]
        bounded = build_company_model_state(
            self.missions, ACN, prompt_byte_limit=base,
        )
        context = bounded["numeric_context"]
        held = [cell for cell in context["cells"] if cell["concept"] == concept]
        self.assertEqual(held, [])
        self.assertEqual(context["omitted_by_prompt_limit"], 2)
        self.assertEqual(context["prompt_bytes"], len(build_prompt(bounded).encode("utf-8")))
        self.assertLessEqual(context["prompt_bytes"], context["prompt_byte_limit"])

    def test_an_overbudget_base_prompt_reports_the_exact_boundary(self):
        self.ingest("0001467373-26-000031", [_line("us-gaap:Revenues")])
        with self.assertRaises(CompanyModelPromptBudgetError) as raised:
            build_company_model_state(self.missions, ACN, prompt_byte_limit=1_000)
        report = raised.exception.report
        self.assertEqual(report["prompt_byte_limit"], 1_000)
        self.assertGreater(report["base_prompt_bytes"], 1_000)
        self.assertEqual(
            report["over_by_bytes"],
            report["base_prompt_bytes"] - report["prompt_byte_limit"],
        )

    def test_bounds_are_explicit_and_policy_changes_the_state_identity(self):
        self.ingest("0001467373-26-000031", [
            _line("us-gaap:Revenues", value="100"),
            _line("us-gaap:OperatingIncomeLoss", value="20"),
        ])
        default = build_company_model_state(self.missions, ACN)
        bounded = build_company_model_state(
            self.missions, ACN,
            numeric_context_policy={"max_periods_per_series": 1,
                                    "max_total_cells": 1},
        )
        context = bounded["numeric_context"]
        self.assertEqual(context["included_cells"], 1)
        self.assertEqual(context["omitted_by_total_limit"], 1)
        self.assertTrue(context["truncated"])
        self.assertNotEqual(default["state_hash"], bounded["state_hash"])
        other_prompt_budget = build_company_model_state(
            self.missions, ACN, prompt_byte_limit=119_999,
        )
        self.assertNotEqual(default["state_hash"], other_prompt_budget["state_hash"])
        with self.assertRaisesRegex(ValueError, "positive integer"):
            validate_numeric_context_policy({"max_periods_per_series": 0,
                                             "max_total_cells": 1})
        with self.assertRaisesRegex(ValueError, "closed shape"):
            validate_numeric_context_policy({
                "max_periods_per_series": 1, "max_total_cells": 1,
                "prefer_concept": "us-gaap:Revenues",
            })

    def test_prompt_exposes_the_source_bound_nonoperating_bridge_values(self):
        self.ingest("0001467373-26-000031", [
            _line("us-gaap:Revenues", value="1000.00"),
            _line("us-gaap:OperatingIncomeLoss", value="100.00"),
            _line("us-gaap:OtherNonoperatingIncomeExpense", value="15.00"),
            _line("us-gaap:IncomeBeforeTaxExpenseBenefit", value="115.00"),
            _line("us-gaap:IncomeTaxExpenseBenefit", value="20.00"),
            _line("us-gaap:NetIncomeLoss", value="95.00"),
        ])
        state = build_company_model_state(self.missions, ACN)
        prompt = build_prompt(state)

        self.assertIn("NUMERIC_PERIODS", prompt)
        self.assertIn('"us-gaap:OtherNonoperatingIncomeExpense"', prompt)
        self.assertIn('"15.00"', prompt)
        self.assertIn('"100.00"', prompt)
        self.assertIn('"95.00"', prompt)
        cell = next(
            item for item in state["numeric_context"]["cells"]
            if item["concept"] == "us-gaap:OtherNonoperatingIncomeExpense"
        )
        self.assertIn(state["numeric_context"]["content_hash"], prompt)
        self.assertIn(cell["filing_content_hash"], prompt)


if __name__ == "__main__":
    unittest.main()
