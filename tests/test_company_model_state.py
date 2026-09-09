"""P13al: the ledger projected down to the structure a model is decided from.

Values move every quarter; the vocabulary is what a model rests on. This drops
the numbers, deduplicates concepts across filings and periods, and hashes what
is left -- so a specification is bound to the disclosure it was written for and
becomes history when a filing introduces a line nobody had seen.
"""

from __future__ import annotations

import unittest

from dalton_core.company_model_state import (
    MAX_CONCEPTS_PER_STATEMENT,
    CompanyModelStateError,
    build_company_model_state,
)
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

    def ingest(self, accession, lines, *, report_date="2026-06-30", attempt=0):
        dispatch = self.missions.queue_statement_dispatch(
            authorization=self.authorization, attempt=attempt)
        self.missions.mark_statement_dispatch_launched(
            dispatch["dispatch_id"], f"sec-financials-run:{attempt:024d}")
        self.missions.record_statement_observation(
            dispatch_id=dispatch["dispatch_id"],
            observation={
                "schema_version": "0.1", "cik": "0001467373",
                "entity_name": "Accenture plc",
                "filings": [{
                    "accession": accession, "form": "10-Q", "filed": report_date,
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


if __name__ == "__main__":
    unittest.main()
