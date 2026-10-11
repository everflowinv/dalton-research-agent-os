from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3
import tempfile
import unittest

from dalton_core.lane_failure_ledger import LaneFailureLedger
from dalton_core.model_execution_failure_feedback import last_failures
from dalton_core.store import content_hash

COMPANY = "company:sec-cik:0000000001"
AT = datetime(2026, 1, 1, tzinfo=timezone.utc)


class FailureFeedbackTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.ledger = LaneFailureLedger(self.root / "lane-failure-ledger.sqlite")
        self.addCleanup(self.ledger.close)
        self.core = sqlite3.connect(":memory:")
        self.core.row_factory = sqlite3.Row
        self.addCleanup(self.core.close)

    def event(self, *, lane="mission_model_spec", company=COMPANY, key="input",
              event="terminal", at=AT, dependency=None, failure_class="content_refused"):
        return self.ledger.append_event(lane=lane, item_key=company + "|" + key,
            event=event, failure_class=failure_class, reason="exact driver identity missing an axis",
            rule="lane_content_refusal", status="refused", at=at, dependency=dependency)

    def read(self, **kwargs):
        return last_failures(self.core, state_dir=self.root, company_ref=COMPANY, **kwargs)

    def test_exact_company_two_lanes_only_and_unlinked_scope(self):
        first = self.event()
        self.event(lane="mission_model_forecast")
        self.event(company=COMPANY + "2")
        self.event(lane="unrelated_lane")
        result = self.read()
        self.assertEqual(len(result["items"]), 2)
        self.assertEqual(result["items"][0]["content_hash"], first["content_hash"])
        for item in result["items"]:
            self.assertEqual(item["company_ref"], COMPANY)
            self.assertEqual(item["scope"], "operations_last_failure_only")
            self.assertEqual(item["work_formal_binding"], "not_linked_by_this_ledger")
            self.assertFalse(item["financial_evidence"])
            self.assertFalse(item["retry_authority"])

    def test_clear_and_supersede_do_not_return_old_failure(self):
        for outcome in ("resumed", "permission_ok", "superseded"):
            with self.subTest(outcome=outcome):
                self.event(key=outcome)
                self.event(key=outcome, event=outcome, at=AT + timedelta(seconds=1))
                self.assertEqual(self.read()["items"], [])

    def test_checked_dependency_recovery_clears_parked(self):
        self.event(event="parked", dependency="model_budget", failure_class="dependency_unavailable")
        self.ledger.append_dependency_ok(lane="other_probe_lane", dependency="model_budget",
                                         at=AT + timedelta(seconds=1))
        self.assertEqual(self.read()["items"], [])

    def test_mutated_event_refuses_instead_of_becoming_a_fact(self):
        self.event()
        self.ledger.connection.execute("DROP TRIGGER lane_failure_events_no_update")
        self.ledger.connection.execute("UPDATE lane_failure_events SET reason='forged reason'")
        with self.assertRaisesRegex(ValueError, "hash or identity differs"):
            self.read()

    def test_new_accepted_spec_supersedes_old_failure_with_checked_body(self):
        from dalton_core.store import DaltonStore
        from dalton_core.coverage_mission import CoverageMissionAuthority
        from dalton_core.company_model_spec import spec_from_response
        from tests import test_company_model_spec as fixtures
        store = DaltonStore(":memory:")
        self.addCleanup(store.close)
        missions = CoverageMissionAuthority(store)
        spec = spec_from_response({**fixtures.STATE, "company_ref": COMPANY},
                                  fixtures._spec(), decided_by=fixtures.DECIDED_BY)
        missions.record_company_model_spec(spec, mission_version_ref="mission:example")
        self.event()
        result = last_failures(store.connection, state_dir=self.root, company_ref=COMPANY)
        self.assertEqual(result["items"], [])

    def test_unchecked_forecast_completion_cannot_hide_failure(self):
        self.event(lane="mission_model_forecast")
        model = {"id": "forecast:example", "company_ref": COMPANY,
                 "created_at": (AT + timedelta(seconds=1)).isoformat()}
        model["content_hash"] = content_hash(model)
        self.assertEqual(self.read(checked_forecast=model)["items"], [])
        model["created_at"] = (AT + timedelta(days=1)).isoformat()
        with self.assertRaisesRegex(ValueError, "completion hash differs"):
            self.read(checked_forecast=model)

    def test_window_is_bounded_and_omissions_are_hashed(self):
        for index in range(40):
            self.event(key=f"input{index:03d}", at=AT + timedelta(seconds=index))
        result = self.read()
        self.assertEqual(len(result["items"]), 1)
        self.assertEqual(result["items"][0]["item_key"], COMPANY + "|input039")
        summary = result["window_summary"][0]
        self.assertEqual(summary["events_read"], 33)
        self.assertTrue(summary["event_window_truncated"])
        self.assertEqual(summary["additional_active_failures_in_window"], 31)
        self.assertEqual(len(summary["additional_active_failures_hash"]), 64)

    def test_financial_api_and_compression_preserve_failure_at_keep_zero(self):
        from dalton_core.research_financial_state import financial_state
        from dalton_core.research_state import company_state, planning_hash
        from dalton_core.research_planner import project_state_for_prompt
        self.event()
        financial = financial_state(self.core, COMPANY, {"id": "mission:example"}, state_dir=self.root)
        self.assertEqual(financial["status"], "unavailable")
        subject = company_state({"company_ref": COMPANY, "ticker": "EX", "items": []},
                                financial_model=financial)
        subject["document_research_feedback"] = [
            {"id": f"old-feedback:{i}", "question": "old question " * 10000,
             "outcome": "unrelated_history", "created_at": "2025-01-01"} for i in range(12)]
        source = {"schema_version": "0.1", "companies": [subject]}
        projected = project_state_for_prompt(source, max_input_bytes=64000)
        self.assertEqual(projected["prompt_projection"]["document_research_feedback_keep_per_company"], 0)
        kept = projected["companies"][0]
        self.assertEqual(kept["model_execution_failures"], subject["model_execution_failures"])
        changed = {**source, "companies": [{**subject, "model_execution_failures": []}]}
        self.assertNotEqual(planning_hash(source), planning_hash(changed))


if __name__ == "__main__":
    unittest.main()
