"""P13h: cadence says how often a search may repeat, not whether to run it.

Nothing asked whether the checklist item a spec feeds already had enough, so a
requirement met long ago went on being searched every seven or fourteen days
forever. Live that is 91 industry-demand documents against a requirement of
three, and 132 competitive-landscape against three -- 223 documents for a need
of six, with 83 more queued. That is most of the answer to the owner's "why
does it keep fetching web pages".
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from dalton_core import mission_source_discovery as m
from dalton_core.mission_source_discovery import SATISFIED_OVERSHOOT

ACN = "company:sec-cik:0001467373"
MISSION = {"id": "coverage-mission-version:x:1", "industry_ref": "industry:us-it-services"}


def item(item_ref, required, have):
    return {"item_ref": item_ref, "required": required, "have": have}


class SatisfiedGateTests(unittest.TestCase):
    def coordinator(self):
        coordinator = object.__new__(m.MissionSourceDiscoveryCoordinator)
        # A store with a connection: the gate reads the checklist from Core,
        # and a store without one would be swallowed by the guard below and
        # look like "no opinion" rather than a broken test.
        coordinator.store = type("S", (), {"connection": object()})()
        return coordinator

    def gate(self, spec_ref, *, company_items=(), industry_items=()):
        coordinator = self.coordinator()
        with patch("dalton_core.mission_stage.evaluate_mission",
                   return_value=[{"company_ref": ACN, "items": list(company_items)}]), \
             patch("dalton_core.mission_stage.evaluate_industry",
                   return_value={"items": list(industry_items)}):
            return m.MissionSourceDiscoveryCoordinator._satisfied_block(
                coordinator, MISSION, ACN, spec_ref)

    def test_a_spec_far_past_its_requirement_is_not_searched(self):
        block = self.gate("industry-demand",
                          industry_items=[item("industry_demand", 3, 91)])
        self.assertIsNotNone(block)
        self.assertIn("91 of 3", block)

    def test_a_spec_with_a_real_gap_is_searched(self):
        self.assertIsNone(self.gate(
            "earnings-call-transcripts",
            company_items=[item("earnings_calls", 4, 1)]))

    def test_a_satisfied_item_may_still_refresh_up_to_the_overshoot(self):
        # The checklist counts documents, not their age: four transcripts from
        # last year are "complete" and still want this quarter's.
        self.assertIsNone(self.gate(
            "earnings-call-transcripts",
            company_items=[item("earnings_calls", 4, 4)]))
        self.assertIsNone(self.gate(
            "earnings-call-transcripts",
            company_items=[item("earnings_calls", 4, 7)]))
        self.assertIsNotNone(self.gate(
            "earnings-call-transcripts",
            company_items=[item("earnings_calls", 4, 8)]))

    def test_the_ceiling_is_the_requirement_times_the_overshoot(self):
        required = 3
        self.assertIsNone(self.gate(
            "sell-side-reports",
            company_items=[item("broker_research", required,
                                required * SATISFIED_OVERSHOOT - 1)]))
        self.assertIsNotNone(self.gate(
            "sell-side-reports",
            company_items=[item("broker_research", required,
                                required * SATISFIED_OVERSHOOT)]))

    def test_a_spec_no_checklist_item_feeds_is_left_to_the_cadence(self):
        # management-changes has no requirement anywhere; this gate has no
        # opinion about it rather than inventing one.
        self.assertIsNone(self.gate("management-changes"))

    def test_an_unreadable_checklist_does_not_block_the_search(self):
        coordinator = self.coordinator()
        with patch("dalton_core.mission_stage.evaluate_mission",
                   side_effect=RuntimeError("core is busy")):
            self.assertIsNone(m.MissionSourceDiscoveryCoordinator._satisfied_block(
                coordinator, MISSION, ACN, "earnings-call-transcripts"))

    def test_an_item_the_checklist_does_not_carry_is_not_blocked(self):
        self.assertIsNone(self.gate("earnings-call-transcripts", company_items=[]))


if __name__ == "__main__":
    unittest.main()


class FloorAndPlanTests(unittest.TestCase):
    """Which of the three gates wins, exercised rather than read."""

    SPEC = {"spec_ref": "earnings-call-transcripts", "rediscovery_interval_days": 7,
            "retry_interval_days": 1}
    MISSION = {"id": "coverage-mission-version:x:1",
               "industry_ref": "industry:us-it-services"}

    def coordinator(self, *, plan=(None, None), satisfied=None, cadence=None):
        c = object.__new__(m.MissionSourceDiscoveryCoordinator)
        c._plan_decision = lambda *_a, **_k: plan
        c._satisfied_block = lambda *_a, **_k: satisfied
        c._cadence_block = lambda *_a, **_k: cadence
        return c

    def block(self, **kwargs):
        c = self.coordinator(**kwargs)
        return m.MissionSourceDiscoveryCoordinator._spec_block(c, self.MISSION, ACN, self.SPEC)

    def test_a_plan_stop_beats_everything(self):
        self.assertEqual(self.block(plan=("plan says stop", None),
                                    satisfied=None, cadence=None), "plan says stop")

    def test_a_plan_asking_for_work_overrides_the_overshoot_ceiling(self):
        # The floor must not pre-empt the decision the planner exists to make.
        self.assertIsNone(self.block(plan=(None, "plan asks"),
                                     satisfied="already holds 91 of 3"))

    def test_a_plan_asking_for_work_is_still_bounded_by_the_cadence(self):
        # A plan may say "search this"; it may not say "search this every five
        # minutes". An open dispatch is still an open dispatch.
        self.assertEqual(self.block(plan=(None, "plan asks"),
                                    cadence="previous discovery still open"),
                         "previous discovery still open")

    def test_a_silent_plan_leaves_the_floor_in_charge(self):
        self.assertEqual(self.block(satisfied="already holds 91 of 3"),
                         "already holds 91 of 3")

    def test_a_silent_plan_and_a_satisfied_floor_still_defer_to_cadence(self):
        self.assertEqual(self.block(cadence="rediscovered 2d ago"),
                         "rediscovered 2d ago")

    def test_nothing_blocking_means_search(self):
        self.assertIsNone(self.block())
