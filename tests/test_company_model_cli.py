"""P13am: decide, store, go quiet.

This file exists for one bug. The lane picked a company using a projection
built without its ticker, while the run stored its specification against a
projection built *with* it. The ticker is part of the state and therefore part
of its hash, so the selector could never see the answer it had just produced.

It cost nothing -- the child found the stored specification and replayed it for
free -- but it cost progress: the lane relaunched one company every tick while
the other four waited behind it forever. It reached production and was caught
by reading a heartbeat, not by a test, because every test had one side of the
projection or the other and never both.

So the invariant here is end to end and deliberately blunt: after a
specification is stored, there is nothing left to decide.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from dalton_core.company_model_cli import choose_company, run_model_spec
from dalton_core.company_model_spec import spec_from_response
from dalton_core.company_model_state import build_company_model_state
from dalton_core.coverage_mission import CoverageMissionAuthority
from dalton_core.store import DaltonStore
from tests.p9a_fixtures import bootstrap_method_authorities, mission_params

ACN = "company:sec-cik:0001467373"


def _spec_body():
    return {
        "schema_version": "0.1",
        "assessment": "A people business: billable heads times realised rate.",
        "revenue_drivers": [{
            "ref": "heads", "label": "Billable headcount", "kind": "volume",
            "basis_concept": None, "unit": "headcount",
            "because": "Capacity is the binding constraint on delivery revenue.",
        }],
        "expense_lines": [{
            "ref": "delivery", "label": "Cost of services",
            "basis_concept": "us-gaap:Revenues",
            "behaviour": "variable_with_headcount", "driver_ref": "heads",
            "because": "Delivery payroll follows the billable base.",
        }],
        "forecast_statements": [
            {"statement": "income", "importance": "required",
             "because": "Revenue and margin are the question."},
            {"statement": "balance", "importance": "supporting",
             "because": "Capital light."},
            {"statement": "cash", "importance": "required",
             "because": "Free cash flow funds the buyback."},
        ],
        "operating_metrics": [],
        "horizon": {"historical_quarters": 12, "forecast_quarters": 8,
                    "because": "Three years spans the cycle."},
    }


class ChooseCompanyTests(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.state_dir = Path(self._dir.name)
        self.store = DaltonStore(str(self.state_dir / "core.sqlite"))
        self.addCleanup(self.store.close)
        state = bootstrap_method_authorities(self.store)
        self.missions = CoverageMissionAuthority(self.store)
        params = mission_params(state)
        self.mission = self.missions.create_mission(params.pop("mission_ref"), **params)
        authorization = self.missions.authorize_sec_lane(
            company_ref=ACN, ticker="ACN", actor_ref="automation:coverage-mission",
            mission_version_ref=self.mission["id"],
            mission_version_hash=self.mission["content_hash"])
        dispatch = self.missions.queue_statement_dispatch(authorization=authorization)
        self.missions.mark_statement_dispatch_launched(
            dispatch["dispatch_id"], "sec-financials-run:" + "1" * 24)
        self.missions.record_statement_observation(
            dispatch_id=dispatch["dispatch_id"],
            observation={
                "schema_version": "0.1", "cik": "0001467373",
                "entity_name": "Accenture plc",
                "filings": [{
                    "accession": "0001467373-26-000031", "form": "10-Q",
                    "filed": "2026-06-25", "report_date": "2026-06-30",
                    "lines": [{
                        "statement": "income", "concept": "us-gaap:Revenues",
                        "label": "Revenues", "level": 0, "parent_concept": None,
                        "is_breakdown": False, "dimension_axis": None,
                        "dimension_member": None, "period_start": "2026-04-01",
                        "period_end": "2026-06-30", "value": "17700000000",
                        "unit": "USD", "balance": "credit",
                    }],
                }],
                "source_record_refs": ["raw-sink:" + "c" * 64],
                "next_cursor": None, "provider_status": 200,
            },
            governance_ref="g", governance_hash="b" * 64)
        self.missions.settle_statement_dispatch(
            dispatch["dispatch_id"], outcome="succeeded")

    def test_the_chosen_state_is_the_one_a_specification_is_stored_against(self):
        company_ref, state = choose_company(self.missions, self.mission)
        self.assertEqual(company_ref, ACN)
        # The projection carries the ticker, so its hash is the ticker's hash.
        self.assertEqual(state["ticker"], "ACN")
        self.assertEqual(
            state["state_hash"],
            build_company_model_state(self.missions, ACN, ticker="ACN")["state_hash"])

    def test_once_a_specification_is_stored_there_is_nothing_left_to_decide(self):
        _, state = choose_company(self.missions, self.mission)
        self.missions.record_company_model_spec(
            spec_from_response(state, _spec_body(), decided_by="automation:x"),
            mission_version_ref=self.mission["id"])
        self.assertEqual(choose_company(self.missions, self.mission), (None, None))

    def test_a_named_company_is_used_as_given(self):
        company_ref, state = choose_company(
            self.missions, self.mission, company_ref=ACN)
        self.assertEqual(company_ref, ACN)
        self.assertEqual(state["ticker"], "ACN")

    def test_a_company_with_no_statements_is_skipped_not_chosen(self):
        stranger = "company:sec-cik:0009999999"
        mission = {**self.mission, "universe": [
            {"company_ref": stranger, "ticker": "ZZZ"}]}
        self.assertEqual(choose_company(self.missions, mission), (None, None))

    def test_a_dry_run_reports_the_same_state_the_lane_would_launch_for(self):
        _, state = choose_company(self.missions, self.mission)
        summary = run_model_spec(
            state_dir=self.state_dir, model_config_path=None,
            summary_dir=self.state_dir / "summary", scheduler_db=None,
            dry_run=True)
        self.assertEqual(summary["status"], "succeeded")
        self.assertEqual(summary["spec_status"], "gated")
        self.assertEqual(summary["state_hash"], state["state_hash"])
        self.assertEqual(summary["company_ref"], ACN)
        self.assertEqual(summary["formal_authority_writes"], 0)
        written = json.loads(
            (self.state_dir / "summary" / "summary.json").read_text(encoding="utf-8"))
        self.assertEqual(written["state_hash"], state["state_hash"])

    def test_a_run_with_nothing_to_decide_says_so_rather_than_paying(self):
        _, state = choose_company(self.missions, self.mission)
        self.missions.record_company_model_spec(
            spec_from_response(state, _spec_body(), decided_by="automation:x"),
            mission_version_ref=self.mission["id"])
        summary = run_model_spec(
            state_dir=self.state_dir, model_config_path=Path("/nonexistent.json"),
            summary_dir=self.state_dir / "summary", scheduler_db=None)
        self.assertEqual(summary["status"], "idle")
        self.assertEqual(summary["spec_status"], "nothing_to_decide")
        self.assertEqual(summary["cost_micros"], 0)


if __name__ == "__main__":
    unittest.main()
