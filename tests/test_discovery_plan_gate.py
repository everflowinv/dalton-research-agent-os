"""P13v: the plan decides, the deterministic floor catches what it does not.

Cadence and the overshoot ceiling stop obvious waste without anyone deciding
anything. A plan moves in either direction on top of that: `stop` refuses work
the floor would have allowed, `search` allows work it would have blocked. A
plan that says nothing leaves the floor alone -- so an empty plan changes
nothing rather than stopping everything.
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from dalton_core import mission_source_discovery as m

ACN = "company:sec-cik:0001467373"
INDUSTRY = "industry:us-it-services"
MISSION = {"id": "coverage-mission-version:x:1", "industry_ref": INDUSTRY}


def directive(**overrides):
    base = {"rank": 0, "company_ref": ACN, "item_ref": "earnings_calls",
            "action": "stop", "reason": "four of four held"}
    base.update(overrides)
    return base


class PlanGateTests(unittest.TestCase):
    def coordinator(self, plan):
        c = object.__new__(m.MissionSourceDiscoveryCoordinator)
        c.store = type("S", (), {"connection": object()})()
        c.missions = type("M", (), {"latest_research_plan": staticmethod(lambda _ref: plan)})()
        return c

    def decide(self, plan, spec_ref, company_ref=ACN):
        return m.MissionSourceDiscoveryCoordinator._plan_decision(
            self.coordinator(plan), MISSION, company_ref, spec_ref)

    def test_a_stop_directive_blocks_a_spec_the_floor_would_allow(self):
        block, allow = self.decide({"directives": [directive()]},
                                   "earnings-call-transcripts")
        self.assertIsNotNone(block)
        self.assertIn("four of four held", block)
        self.assertIsNone(allow)

    def test_a_search_directive_allows_a_spec_the_floor_would_block(self):
        block, allow = self.decide(
            {"directives": [directive(action="search", reason="one filing short")]},
            "earnings-call-transcripts")
        self.assertIsNone(block)
        self.assertIn("one filing short", allow)

    def test_acquire_counts_as_asking_for_it(self):
        _, allow = self.decide(
            {"directives": [directive(action="acquire")]}, "earnings-call-transcripts")
        self.assertIsNotNone(allow)

    def test_a_plan_that_says_nothing_leaves_the_floor_alone(self):
        for plan in ({"directives": []}, {}, None):
            self.assertEqual(self.decide(plan, "earnings-call-transcripts"), (None, None))

    def test_a_directive_for_another_item_does_not_apply(self):
        self.assertEqual(
            self.decide({"directives": [directive(item_ref="broker_research")]},
                        "earnings-call-transcripts"),
            (None, None))

    def test_a_directive_for_another_company_does_not_apply(self):
        self.assertEqual(
            self.decide({"directives": [directive(company_ref="company:sec-cik:9")]},
                        "earnings-call-transcripts"),
            (None, None))

    def test_an_industry_item_is_addressed_as_the_industry(self):
        # The planner says stop on industry_demand for the industry, not for
        # whichever company's search happened to find the documents.
        plan = {"directives": [directive(company_ref=INDUSTRY,
                                         item_ref="industry_demand",
                                         reason="91 held against 3 required")]}
        block, _ = self.decide(plan, "industry-demand", company_ref=ACN)
        self.assertIsNotNone(block)
        self.assertIn("91 held", block)

    def test_an_industry_item_is_not_addressable_as_a_company(self):
        plan = {"directives": [directive(company_ref=ACN, item_ref="industry_demand")]}
        self.assertEqual(self.decide(plan, "industry-demand"), (None, None))

    def test_a_spec_no_checklist_item_feeds_has_no_plan_opinion(self):
        self.assertEqual(
            self.decide({"directives": [directive()]}, "management-changes"),
            (None, None))

    def test_an_unreadable_plan_is_not_a_decision(self):
        c = object.__new__(m.MissionSourceDiscoveryCoordinator)
        c.store = type("S", (), {"connection": object()})()

        def boom(_ref):
            raise RuntimeError("core is busy")

        c.missions = type("M", (), {"latest_research_plan": staticmethod(boom)})()
        self.assertEqual(
            m.MissionSourceDiscoveryCoordinator._plan_decision(
                c, MISSION, ACN, "earnings-call-transcripts"),
            (None, None))


if __name__ == "__main__":
    unittest.main()
