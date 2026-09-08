"""P13m: a plan the system made about its own work, bound to what it saw."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from dalton_core.coverage_mission import (
    CoverageMissionAuthority,
    CoverageMissionValidationError,
)
from dalton_core.store import DaltonStore

MISSION = "coverage-mission-version:us-it-services:12"


def plan(**overrides):
    base = {
        "mission_version_ref": MISSION,
        "state_hash": "a" * 64,
        "assessment": "ACN is one filing short of a screen.",
        "directives": [{"rank": 0, "company_ref": "company:sec-cik:0001467373",
                        "item_ref": "quarterly_financials", "action": "acquire",
                        "reason": "three quarters short"}],
        "inquiries": [],
        "content_hash": "b" * 64,
    }
    base.update(overrides)
    return base


class PlanStoreTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.store = DaltonStore(str(Path(self._dir.name) / "core.sqlite"))
        self.addCleanup(self.store.close)
        self.authority = CoverageMissionAuthority(self.store)

    def record(self, **overrides):
        return self.authority.record_research_plan(
            plan(**overrides), decided_by="automation:coverage-mission",
            model_profile_ref="profile:gpt-6-astra", work_order_ref="work:research-plan-1")

    def test_a_plan_is_stored_with_its_directives_intact(self):
        stored = self.record()
        self.assertEqual(stored["status"], "fresh")
        self.assertEqual(stored["directives"][0]["action"], "acquire")
        self.assertEqual(stored["model_profile_ref"], "profile:gpt-6-astra")

    def test_deciding_twice_against_an_unchanged_state_is_one_decision(self):
        self.record()
        again = self.record()
        self.assertEqual(again["status"], "duplicate")
        self.assertEqual(
            self.store.connection.execute(
                "SELECT COUNT(*) FROM coverage_mission_research_plans").fetchone()[0], 1)

    def test_a_moved_state_is_a_new_plan(self):
        self.record()
        moved = self.record(state_hash="c" * 64)
        self.assertEqual(moved["status"], "fresh")
        self.assertEqual(
            self.authority.latest_research_plan(MISSION)["state_hash"], "c" * 64)

    def test_the_plan_for_an_exact_state_can_be_found_again(self):
        self.record()
        found = self.authority.research_plan_for_state(MISSION, "a" * 64)
        self.assertIsNotNone(found)
        self.assertEqual(found["assessment"], "ACN is one filing short of a screen.")
        self.assertIsNone(self.authority.research_plan_for_state(MISSION, "z" * 64))

    def test_a_mission_with_no_plan_has_none(self):
        self.assertIsNone(self.authority.latest_research_plan(MISSION))

    def test_an_incomplete_plan_is_refused(self):
        for field in ("state_hash", "assessment", "directives", "inquiries", "content_hash"):
            body = plan()
            body.pop(field)
            with self.assertRaises(CoverageMissionValidationError, msg=field):
                self.authority.record_research_plan(body, decided_by="automation:x")

    def test_plans_are_append_only_and_authority_only(self):
        self.record()
        for statement in (
            "UPDATE coverage_mission_research_plans SET assessment='x'",
            "DELETE FROM coverage_mission_research_plans",
            "INSERT INTO coverage_mission_research_plans(plan_id,mission_version_ref,"
            "state_hash,assessment,directives_json,inquiries_json,decided_by,created_at,"
            "content_hash) VALUES('x','m','s','a','[]','[]','d','t','h')",
        ):
            with self.assertRaises(Exception):
                self.store.connection.execute(statement)

    def test_an_empty_plan_is_still_a_decision_worth_recording(self):
        # "nothing needs doing" is an answer, and losing it means re-paying for
        # it every tick.
        stored = self.record(directives=[], assessment="Everything is on track.")
        self.assertEqual(stored["status"], "fresh")
        self.assertEqual(stored["directives"], [])


if __name__ == "__main__":
    unittest.main()
