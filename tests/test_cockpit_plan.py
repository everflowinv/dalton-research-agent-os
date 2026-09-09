"""P13w: the system's own decision, where the owner can read it.

The planner steered the lanes for a while with nothing on the page to show
for it -- the plan lived in one table and its effect showed up only as lanes
going quiet. A decision nobody can read is indistinguishable from the calendar
it replaced, which is the objection this was built to answer.
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from dalton_core.cockpit_plane import ITEM_LABELS, PLAN_ACTION_LABELS, CockpitPlane
from dalton_core.coverage_mission import CoverageMissionAuthority
from dalton_core.store import DaltonStore

ACN = "company:sec-cik:0001467373"
INDUSTRY = "industry:us-it-services"
MISSION = {
    "id": "coverage-mission-version:us-it-services:13",
    "industry_ref": INDUSTRY,
    "universe": [{"company_ref": ACN, "ticker": "ACN"}],
}


def directive(**overrides):
    base = {"rank": 0, "company_ref": ACN, "item_ref": "quarterly_financials",
            "action": "acquire", "reason": "one filing short of a screen"}
    base.update(overrides)
    return base


class PlanProjectionTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.path = Path(self._dir.name) / "core.sqlite"
        self.store = DaltonStore(str(self.path))
        self.addCleanup(self.store.close)
        self.authority = CoverageMissionAuthority(self.store)
        self.members = {m["company_ref"]: dict(m) for m in MISSION["universe"]}

    def record(self, *directives, inquiries=(), assessment="ACN is one filing short."):
        self.authority.record_research_plan(
            {"mission_version_ref": MISSION["id"], "state_hash": "a" * 64,
             "assessment": assessment, "directives": list(directives),
             "inquiries": list(inquiries), "content_hash": "b" * 64},
            decided_by="automation:x")

    def project(self):
        connection = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        try:
            return CockpitPlane._plan(CockpitPlane, connection, MISSION, self.members)
        finally:
            connection.close()

    def test_no_plan_yet_projects_nothing_rather_than_an_empty_box(self):
        self.assertIsNone(self.project())

    def test_a_directive_is_shown_with_its_subject_item_and_reason(self):
        self.record(directive())
        [shown] = self.project()["directives"]
        self.assertIn("ACN", shown["subject"])
        self.assertEqual(shown["item"], ITEM_LABELS["quarterly_financials"])
        self.assertEqual(shown["action_label"], PLAN_ACTION_LABELS["acquire"])
        # The reason is the point: a plan that cannot be argued with is a
        # calendar with extra steps.
        self.assertEqual(shown["reason"], "one filing short of a screen")

    def test_the_industry_is_named_as_the_industry(self):
        self.record(directive(company_ref=INDUSTRY, item_ref="industry_demand",
                              action="stop", reason="91 held against 3"))
        [shown] = self.project()["directives"]
        self.assertEqual(shown["subject"], "整个行业")
        self.assertEqual(shown["item"], ITEM_LABELS["industry_demand"])

    def test_stops_are_counted_because_that_is_the_new_behaviour(self):
        self.record(directive(), directive(rank=1, action="stop"),
                    directive(rank=2, action="stop"))
        self.assertEqual(self.project()["stopped"], 2)

    def test_inquiries_carry_what_would_answer_them(self):
        self.record(directive(), inquiries=[{
            "rank": 0, "company_ref": ACN,
            "question": "Does this metric belong to CTSH?",
            "wants": "the passages behind the assignment",
            "because": "no document underpins it"}])
        [asked] = self.project()["inquiries"]
        self.assertIn("ACN", asked["subject"])
        self.assertTrue(asked["wants"])

    def test_an_industry_wide_inquiry_has_a_subject_too(self):
        self.record(directive(), inquiries=[{
            "rank": 0, "company_ref": None, "question": "q", "wants": "w",
            "because": "b"}])
        self.assertEqual(self.project()["inquiries"][0]["subject"], "整个行业")

    def test_the_newest_plan_is_the_one_shown(self):
        self.record(directive(reason="older"))
        self.authority.record_research_plan(
            {"mission_version_ref": MISSION["id"], "state_hash": "c" * 64,
             "assessment": "newer", "directives": [directive(reason="newer")],
             "inquiries": [], "content_hash": "d" * 64},
            decided_by="automation:x")
        self.assertEqual(self.project()["assessment"], "newer")

    def test_a_core_without_the_table_yet_projects_nothing(self):
        bare = Path(self._dir.name) / "bare.sqlite"
        connection = sqlite3.connect(bare)
        connection.row_factory = sqlite3.Row
        try:
            self.assertIsNone(CockpitPlane._plan(CockpitPlane, connection, MISSION, {}))
        finally:
            connection.close()


class PageTests(unittest.TestCase):
    def test_the_page_renders_the_plan_and_marks_a_stop(self):
        html = (Path(__file__).resolve().parents[1] / "src" / "dalton_core"
                / "cockpit_control.html").read_text(encoding="utf-8")
        for hook in ("plan-assessment", "plan-directives", "plan-inquiries", "plan-when"):
            self.assertIn(hook, html)
        # A stop must look different from work being ordered.
        self.assertIn('d.action==="stop"', html)


if __name__ == "__main__":
    unittest.main()
