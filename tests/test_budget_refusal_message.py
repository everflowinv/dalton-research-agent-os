"""P1-6 (2026-09-30): a budget refusal names the cap that actually fired.

ws-e399's setup mission was refused by its own $1 cap and the message read
"committed 949140 + reserved 158190 against day cap 180000000" -- the day
policy's numbers, which do not support the conclusion printed next to them.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from dalton_core.cockpit_plane import _budget_rejection_detail, _display_reason
from dalton_core.thesis_impact_budget import (
    ThesisImpactBudgetStore, ThesisImpactDayBudgetExceeded,
)

DAY_CAP = 180_000_000


class BudgetRefusalMessageTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.store = ThesisImpactBudgetStore(Path(folder.name) / "budget.sqlite")
        self.addCleanup(self.store.close)
        self.store.register_policy(policy_version_id="budget:owner:1", day_cap_micros=DAY_CAP)
        self.scope = {"mission_ref": "coverage-mission:workspace-setup",
                      "mission_version_ref": "coverage-mission:workspace-setup:1",
                      "mission_version_hash": "1" * 64,
                      "max_daily_paid_calls": 50, "max_daily_cost_micros": 1_000_000}

    def admit(self, work, reserved, scope=None, **extra):
        return self.store.admit(
            policy_version_id="budget:owner:1", day="2026-09-15", work_order_ref=work,
            attempt_number=1, phase="assessment", route_decision_ref="route:" + work,
            reserved_micros=reserved, mission_binding=scope, **extra)

    def test_mission_cap_refusal_prints_the_mission_cap_not_the_day_cap(self):
        first = self.admit("work:a", 900_000, self.scope)
        self.store.settle(first["admission_id"], actual_micros=949_140)
        with self.assertRaises(ThesisImpactDayBudgetExceeded) as caught:
            self.admit("work:b", 158_190, self.scope)
        message = str(caught.exception)
        self.assertEqual(caught.exception.rejection["reason"], "mission_budget_exceeded")
        self.assertIn("committed 949140 + reserved 158190 against mission cap 1000000", message)
        self.assertNotIn("day cap", message)
        self.assertNotIn(str(DAY_CAP), message)
        self.assertEqual(caught.exception.tier["tier"], "mission")
        self.assertEqual(caught.exception.tier["limit"], "cost")
        # The persisted record keeps its shape: nothing new is written into it.
        self.assertNotIn("tier", caught.exception.rejection)

    def test_mission_call_limit_says_it_tripped_on_calls(self):
        scope = {**self.scope, "max_daily_paid_calls": 1}
        self.admit("work:a", 1_000, scope)
        with self.assertRaises(ThesisImpactDayBudgetExceeded) as caught:
            self.admit("work:b", 1_000, scope)
        message = str(caught.exception)
        self.assertIn("1 paid calls + 1 against mission cap call limit 1", message)
        self.assertIn("tripped on calls", message)

    def test_outer_cap_refusal_prints_the_outer_cap(self):
        outer = {"mandate_ref": "mandate:test", "mandate_version_ref": "mandate:test:1",
                 "mandate_version_hash": "1" * 64, "governance_policy_ref": "governance:test",
                 "governance_policy_version_ref": "governance:test:1",
                 "governance_policy_version_hash": "2" * 64,
                 "max_daily_paid_calls": 10, "max_daily_cost_micros": 500_000}
        scope = {**self.scope, "max_daily_cost_micros": 500_000, "max_daily_paid_calls": 10,
                 "outer_budget": outer}
        self.admit("work:a", 400_000, scope)
        with self.assertRaises(ThesisImpactDayBudgetExceeded) as caught:
            self.admit("work:b", 200_000, {**scope, "mission_ref": "coverage-mission:other"})
        message = str(caught.exception)
        self.assertEqual(caught.exception.rejection["reason"], "outer_research_budget_exceeded")
        self.assertIn("against outer research cap 500000", message)
        self.assertNotIn("day cap", message)

    def test_day_cap_refusal_still_prints_the_day_cap(self):
        self.store.register_policy(policy_version_id="budget:owner:small",
                                   day_cap_micros=100_000)
        with self.assertRaises(ThesisImpactDayBudgetExceeded) as caught:
            self.store.admit(
                policy_version_id="budget:owner:small", day="2026-09-15",
                work_order_ref="work:big", attempt_number=1, phase="assessment",
                route_decision_ref="route:big", reserved_micros=200_000)
        message = str(caught.exception)
        self.assertIn("committed 0 + reserved 200000 against day cap 100000", message)
        self.assertEqual(caught.exception.tier["tier"], "day")

    def test_exception_built_from_a_record_alone_falls_back_to_day_numbers(self):
        error = ThesisImpactDayBudgetExceeded({
            "reason": "owner_budget_exceeded", "reserved_micros": 5,
            "day_committed_micros": 10, "day_cap_micros": 12})
        self.assertIn("committed 10 + reserved 5 against day cap 12", str(error))


class CockpitBudgetRefusalDisplayTests(unittest.TestCase):
    def test_trajectory_detail_uses_the_cap_that_fired(self):
        detail = _budget_rejection_detail({
            "reason": "mission_budget_exceeded", "reserved_micros": 158_190,
            "day_committed_micros": 949_140, "day_cap_micros": DAY_CAP,
            "mission_binding": {"max_daily_cost_micros": 1_000_000,
                                "max_daily_paid_calls": 50}})
        self.assertIn("本研究任务自己的每日额度（上限 $1.00、50 次付费调用）", detail)
        self.assertNotIn("180", detail)
        self.assertIn("$0.1582", detail)

    def test_trajectory_detail_for_the_day_cap_reads_real_fields(self):
        # These fields were misnamed and every refusal read 已用 $0.00 / 上限 $0.
        detail = _budget_rejection_detail({
            "reason": "owner_budget_exceeded", "reserved_micros": 1_000_000,
            "day_committed_micros": 179_500_000, "day_cap_micros": DAY_CAP})
        self.assertIn("当日已用 $179.50 / 当日总上限 $180.00", detail)

    def test_the_english_refusal_reads_in_chinese_on_the_page(self):
        text = ("thesis-impact budget refused the call (mission_budget_exceeded): "
                "committed 949140 + reserved 158190 against mission cap 1000000")
        self.assertEqual(_display_reason(text), "研究任务自身的每日模型预算已用完")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
