"""Batch t: the conviction v2 drafter proposes only inside the published policy.

Live 2026-10-07: ACN's draft came back ``avoid`` at ``low`` evidence confidence
under a policy that admits only long/short at medium or better.  The gate was
right to refuse it; the drafter should have been told the policy's bounds and
allowed to answer "no call today", and the lane should not redraft the same
evidence on a timer.
"""

from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from dalton_core.conviction_call import ConvictionCallAuthority  # noqa: F401
from dalton_core.conviction_call_draft_v2 import (
    NO_CALL_PREFIX, NO_CALL_STATUS, POLICY_REFUSED_PREFIX, build_prompt_v2,
    draft_conviction_call_v2, policy_no_call_reason,
)
from dalton_core.conviction_policy import (
    DEFAULT_POLICY, ConvictionPolicyAuthority, default_effective,
)
from dalton_core.research_quality_rubrics import CONVICTION_CALL
from tests.test_conviction_call_lane import ConvictionHarness, FakeLauncher, FakeModel
from tests.test_conviction_call import ACN, MISSION  # noqa: F401
from tests.test_conviction_v2 import (
    OWNER, PASS, PRICE, TODAY, VAL, v2_reply, v2_table, valuation,
)

BINDING = {"snapshot_ref": VAL, "snapshot_hash": "c" * 64, "as_of": TODAY,
           "price_version_ref": PRICE, "close": "193.4"}


def draft(table, replies):
    model = FakeModel(replies)
    result = draft_conviction_call_v2(
        table=table, valuation_binding=BINDING, model=model, mission=MISSION,
        family_of=lambda ref: None, rubric_ref=CONVICTION_CALL.rubric_ref,
        rubric_hash=CONVICTION_CALL.content_hash)
    return result, model


class PolicyBoundedPromptTests(unittest.TestCase):
    def test_the_prompt_offers_only_allowed_directions_and_a_no_call(self):
        prompt = build_prompt_v2(v2_table())
        self.assertIn('"direction": "long|short"', prompt)
        self.assertNotIn("long|short|avoid", prompt)
        self.assertIn('{"no_call": {"reason":', prompt)
        self.assertIn("evidence_confidence.level must be at least medium", prompt)

    def test_a_no_call_reply_is_a_governed_answer_after_one_call(self):
        result, model = draft(v2_table(), [json.dumps(
            {"no_call": {"reason": "证据置信度只有低，达不到策略要求"}}), PASS])
        self.assertEqual(result["status"], NO_CALL_STATUS)
        self.assertTrue(result["reason"].startswith(NO_CALL_PREFIX))
        self.assertEqual(len(model.prompts), 1)

    def test_an_avoid_draft_under_a_long_short_policy_is_a_no_call(self):
        # The live ACN reply: avoid, low evidence confidence.
        reply = json.loads(v2_reply())
        reply["direction"] = "avoid"
        reply["evidence_confidence"]["level"] = "low"
        result, model = draft(v2_table(), [json.dumps(reply), PASS])
        self.assertEqual(result["status"], NO_CALL_STATUS)
        self.assertTrue(result["reason"].startswith(NO_CALL_PREFIX))
        self.assertEqual(len(model.prompts), 1)

    def test_a_policy_refusal_names_itself(self):
        reply = json.loads(v2_reply())
        reply["scenarios"][0]["payoff"] = "10"
        result, _ = draft(v2_table(), [json.dumps(reply), PASS])
        self.assertEqual(result["status"], "policy_refused")
        self.assertTrue(result["reason"].startswith(POLICY_REFUSED_PREFIX))

    def test_no_thesis_at_the_minimum_confidence_costs_nothing(self):
        table = v2_table()
        table["theses"] = [{**row, "confidence": "low"} for row in table["theses"]]
        result, model = draft(table, [v2_reply(), PASS])
        self.assertEqual(result["status"], NO_CALL_STATUS)
        self.assertFalse(result["model_called"])
        self.assertEqual(model.prompts, [])
        self.assertIn("「中」", result["reason"])

    def test_the_free_check_reads_the_policy(self):
        policy = default_effective()["policy"]
        self.assertIsNone(policy_no_call_reason(policy, [{"confidence": "medium"}]))
        self.assertIsNotNone(policy_no_call_reason(policy, [{"confidence": None}]))
        self.assertIsNotNone(policy_no_call_reason(policy, []))


class PolicyBoundedLaneTests(ConvictionHarness):
    def setUp(self):
        super().setUp()
        gate = patch("dalton_core.conviction_call_cli.active_gate", return_value="v2")
        gate.start()
        self.addCleanup(gate.stop)
        from dalton_core.conviction_call import CHECKPOINT_KIND, WRITE_SCOPE

        self.grant(WRITE_SCOPE, checkpoints=(CHECKPOINT_KIND,))
        today = __import__("datetime").datetime.now(
            __import__("datetime").timezone.utc).date().isoformat()
        for target, value in (("latest_valuation", valuation(as_of=today)),
                              ("latest_price_ref", PRICE)):
            reader = patch(f"dalton_core.conviction_call_cli.{target}", return_value=value)
            reader.start()
            self.addCleanup(reader.stop)
        self.launcher = FakeLauncher()
        from dalton_core.mission_conviction_lane import MissionConvictionLaneCoordinator

        self.coordinator = MissionConvictionLaneCoordinator(
            store=self.store, launcher=self.launcher, mission=lambda: self.mission)

    def publish(self, **changes):
        body = json.loads(json.dumps(dict(DEFAULT_POLICY)))
        body["thesis_scope"] = "company_or_industry"
        body.update(changes)
        ConvictionPolicyAuthority(self.store).publish(
            policy=body, reason="fixture", actor_ref=OWNER)

    def test_nothing_draftable_says_so_in_chinese(self):
        result = self.coordinator.dispatch_once()
        self.assertEqual(result["status"], "idle")
        self.assertIn("没有可起草的公司", result["reason"])

    def test_a_no_call_settles_the_evidence_and_is_not_redrafted(self):
        self.admit_thesis()
        self.publish()
        launched = self.coordinator.dispatch_once()
        self.assertEqual(launched["status"], "launched")
        self.launcher.settle(launched["ticket_ref"], {
            "call_status": "no_call", "status": "succeeded",
            "failure_reason": NO_CALL_PREFIX + "证据不足"})
        again = self.coordinator.dispatch_once()
        self.assertEqual(again["status"], "idle", again)
        self.assertIn("今日不出观点", again["reason"])
        self.assertNotIn("没有可起草的公司", again["reason"])
        self.assertEqual(len(self.launcher.started), 1)
        self.assertEqual(self.coordinator.dispatch_once()["status"], "idle")
        self.assertEqual(len(self.launcher.started), 1)

    def test_a_policy_refusal_holds_with_its_own_words_and_no_second_draft(self):
        self.admit_thesis()
        self.publish()
        launched = self.coordinator.dispatch_once()
        self.launcher.settle(launched["ticket_ref"], {
            "call_status": "policy_refused", "status": "succeeded",
            "failure_reason": POLICY_REFUSED_PREFIX + "期望回报没有达到门槛"})
        for _ in range(3):
            held = self.coordinator.dispatch_once()
            self.assertEqual(held["status"], "held", held)
            self.assertIn("策略拒绝了草稿", held["reason"])
        self.assertEqual(len(self.launcher.started), 1)

    def test_a_policy_no_thesis_can_meet_launches_nothing(self):
        self.admit_thesis()  # confidence medium
        self.publish(min_evidence_confidence="high")
        result = self.coordinator.dispatch_once()
        self.assertEqual(result["status"], "idle", result)
        self.assertIn("今日不出观点", result["reason"])
        self.assertEqual(self.launcher.started, [])


if __name__ == "__main__":
    unittest.main()
