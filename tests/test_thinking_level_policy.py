"""aw: request controls, reasoned immutable edits, fleet sync and measured comparisons."""
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from dalton_core.call_budget import CallBudgetError, MEDIUM_THINKING_PURPOSES, resolve_thinking_level
from dalton_core.cockpit_v2.admin.data import thinking_cost_comparison
from dalton_core.model_selection import ModelSelectionError, set_model_selection
from dalton_core.model_routing_sync import fan_out_selection
from dalton_core.thesis_impact_budget import ThesisImpactBudgetStore, ThesisImpactBudgetConflict
from tests.test_model_selection import StateDirectoryCase, NOW
from tests.test_model_routing_sync import _environment


class ThinkingDefaultsTests(unittest.TestCase):
    def test_defaults_and_verifier_floor_are_closed(self):
        self.assertEqual(MEDIUM_THINKING_PURPOSES, {
            "initial_screen", "investment_memo", "deep_insight_gate", "industry_framework"})
        for purpose in MEDIUM_THINKING_PURPOSES:
            self.assertEqual(resolve_thinking_level(purpose), "medium")
            self.assertEqual(resolve_thinking_level(purpose, requested="low"), "low")
        self.assertEqual(resolve_thinking_level("unregistered_stage"), "low")
        for level in ("off", "minimal", "medium", "high", "xhigh", True):
            with self.subTest(level=level), self.assertRaises(CallBudgetError):
                resolve_thinking_level("investment_memo_verifier", requested=level)


class ThinkingOwnerPolicyTests(StateDirectoryCase):
    def test_owner_edit_appends_and_repoints_without_changing_the_chain(self):
        before = self.policy()
        result = set_model_selection(self.root, purpose="investment_memo", mode="thinking",
            thinking_level="low", reason="Compare the golden replay before retaining medium",
            actor_ref="human:owner", now=NOW)
        self.assertEqual(self.router.get_policy(before["policy_version_ref"]), before)
        current = self.router.get_policy(self.stored()["routing_policy_ref"])
        self.assertEqual(current["fallback_chains"], before["fallback_chains"])
        self.assertEqual(current["thinking_level_overrides"]["investment_memo"], {
            "level": "low", "reason": result["reason"], "actor_ref": "human:owner",
            "created_at": NOW.isoformat(timespec="microseconds")})
        from tests.test_model_fallback_chain import _work
        routed = self.router.route(_work("work:thinking-owner-route"), attempt_number=1, capability="research",
            policy_version_ref=current["policy_version_ref"], purpose="investment_memo", tier="brain",
            credential_slot_refs=self.stored()["credential_slot_refs"], required_modalities=["text"],
            required_context_tokens=2_000, estimated_input_tokens=1_000, estimated_output_tokens=500,
            idempotency_key="thinking-owner-route")["decision"]
        self.assertEqual(routed["thinking_level"], "low")
        duplicate = set_model_selection(self.root, purpose="investment_memo", mode="thinking",
            thinking_level="low", reason=result["reason"], actor_ref="human:owner", now=NOW)
        self.assertEqual(duplicate["status"], "unchanged")
        self.assertEqual(self.stored()["routing_policy_ref"], current["policy_version_ref"])

    def test_missing_reason_nonowner_and_nonlow_verifier_never_publish(self):
        before = self.stored()
        for purpose, level, reason, actor in (("investment_memo", "low", "", "human:owner"),
                ("investment_memo", "low", "compare", "automation:mission"),
                ("investment_memo_verifier", "medium", "compare", "human:owner")):
            with self.subTest(purpose=purpose, actor=actor), self.assertRaises((ModelSelectionError, CallBudgetError)):
                set_model_selection(self.root, purpose=purpose, mode="thinking", thinking_level=level,
                                    reason=reason, actor_ref=actor)
        self.assertEqual(self.stored(), before)

    def test_fleet_receives_the_identical_reason_level_and_owner(self):
        other = _environment(self.root, "other", "ws-thinking")
        calls = []
        params = {"purpose": "initial_screen", "thinking_level": "low", "reason": "golden comparison"}
        result = fan_out_selection(manager_config_path=self.root / "manager.json", origin_state_dir=self.root,
            actor_ref="human:owner", params=params, environments=[other],
            writer_call=lambda env, **kwargs: calls.append(kwargs) or {"status": "published"})
        self.assertEqual(result["status"], "ok")
        self.assertEqual(calls, [{"actor_ref": "human:owner", "params": params}])

    def test_cost_comparison_splits_at_the_latest_reasoned_owner_change(self):
        base = datetime(2026, 10, 10, tzinfo=timezone.utc)
        at = [base]
        set_model_selection(self.root, purpose="investment_memo", mode="thinking", thinking_level="medium",
            reason="retain medium after the comparison", actor_ref="human:owner", now=base + timedelta(seconds=15))
        budget_path = self.root / "comparison-budget.sqlite"
        with ThesisImpactBudgetStore(budget_path, clock=lambda: at[0]) as ledger:
            ledger.register_policy(policy_version_id="budget:compare:1", day_cap_micros=5_000_000)
            for index, level in enumerate(("low", "medium")):
                at[0] = base + timedelta(seconds=index * 20)
                record = ledger.admit(policy_version_id="budget:compare:1", day="2026-10-10",
                    work_order_ref=f"work:compare-{index}", attempt_number=1, phase="assessment",
                    route_decision_ref=f"route:compare-{index}", reserved_micros=1_000_000,
                    purpose="investment_memo", thinking_level=level)
                at[0] += timedelta(seconds=10)
                ledger.settle(record["admission_id"], actual_micros=(index + 1) * 100_000)
        comparison = thinking_cost_comparison(budget_path, "2026-10-10", self.root / "model-router.sqlite")[0]
        self.assertEqual(comparison["current_level"], "medium")
        self.assertEqual(comparison["before"]["levels"], ["low"])
        self.assertEqual(comparison["after"]["levels"], ["medium"])
        self.assertEqual(comparison["before"]["average_cost_usd"], 0.1)
        self.assertEqual(comparison["after"]["average_cost_usd"], 0.2)


class ThinkingCostTests(unittest.TestCase):
    def test_ledger_freezes_level_and_compares_without_inventing_historical_levels(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "budget.sqlite"
            clock = [datetime(2026, 10, 10, tzinfo=timezone.utc)]
            with ThesisImpactBudgetStore(path, clock=lambda: clock[0]) as ledger:
                ledger.register_policy(policy_version_id="budget:thinking:1", day_cap_micros=10_000_000)
                for number, level in enumerate((None, "low", "medium")):
                    kwargs = {} if level is None else {"purpose": "investment_memo", "thinking_level": level}
                    record = ledger.admit(policy_version_id="budget:thinking:1", day="2026-10-10",
                        work_order_ref=f"work:thinking-{number}", attempt_number=1, phase="assessment",
                        route_decision_ref=f"route:thinking-{number}", reserved_micros=1_000_000, **kwargs)
                    clock[0] += timedelta(seconds=10)
                    ledger.settle(record["admission_id"], actual_micros=100_000 * (number + 1))
                self.assertEqual(record["thinking_level"], "medium")
                with self.assertRaises(ThesisImpactBudgetConflict):
                    ledger.admit(policy_version_id="budget:thinking:1", day="2026-10-10",
                        work_order_ref="work:thinking-2", attempt_number=1, phase="assessment",
                        route_decision_ref="route:thinking-2", reserved_micros=1_000_000,
                        purpose="investment_memo", thinking_level="low")
            compared = {row["purpose"]: row for row in thinking_cost_comparison(path, "2026-10-10")}
            self.assertEqual(compared["unknown"]["before"]["levels"], ["未记录"])
            self.assertEqual(compared["investment_memo"]["after"]["calls"], 2)
            self.assertEqual(compared["investment_memo"]["after"]["average_seconds"], 10)
            self.assertEqual(compared["investment_memo"]["after"]["average_cost_usd"], 0.25)
