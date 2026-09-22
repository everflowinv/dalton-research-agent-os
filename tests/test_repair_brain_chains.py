"""WP-A/A5 + A6: repairing the live chains, and not bottling the broken ones.

The planning half of ``scripts/repair_brain_chains.py`` is pure -- given the
catalog and the policies, what should the chains say -- so it is tested here
against the exact shape 2026-09-16 found on the live host, without a writer and
without a database.  The applying half goes through ``set_tier_selection`` /
``set_model_selection``, which have their own tests in test_model_selection.
"""

from __future__ import annotations

import importlib.util
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).parents[1]
SPEC = importlib.util.spec_from_file_location(
    "repair_brain_chains", ROOT / "scripts/repair_brain_chains.py")
REPAIR = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(REPAIR)

NOW = datetime(2026, 9, 16, 8, 0, tzinfo=timezone.utc)
LIVE_BRAIN = [
    "profile:deepseek-v4-flash",
    "profile:zai-glm-5-3",
    "profile:gemini-3-8-flash-antigravity-high",
    "model-profile:claude-opus-5",
    "profile:gpt-6-astra",
]


def _profile(profile_id: str, *, family: str, provider: str = "p",
             model: str | None = None, expired: bool = False) -> dict:
    checked = NOW - timedelta(days=33 if expired else 1)
    valid = checked + timedelta(days=1 if expired else 8)
    return {
        "id": profile_id,
        "family": family,
        # provider/model compose the identity research_output_preparation reads
        # back off the served route decision, so the transport contract can be
        # checked against exactly what the worker will assert on.
        "provider": provider,
        "model": model or profile_id.split(":", 1)[-1],
        "limits": {"max_input_tokens": 983_040},
        "availability": {
            "state": "available",
            "checked_at": checked.isoformat(timespec="microseconds"),
            "valid_until": valid.isoformat(timespec="microseconds"),
        },
    }


ANTIGRAVITY = "profile:gemini-3-8-flash-antigravity"
ANTIGRAVITY_HIGH = "profile:gemini-3-8-flash-antigravity-high"


def _catalog() -> dict[str, dict]:
    return {
        profile["id"]: profile
        for profile in (
            _profile("profile:deepseek-v4-flash", family="deepseek",
                     provider="deepseek", model="deepseek-flash"),
            _profile("profile:zai-glm-5-3", family="zai", provider="zai"),
            _profile(ANTIGRAVITY, family="gemini",
                     provider="antigravity-cli-gateway", model="gemini-3.8-flash"),
            _profile(ANTIGRAVITY_HIGH, family="gemini",
                     provider="antigravity-cli-gateway", model="gemini-3.8-flash"),
            _profile("profile:gemini-3-8-flash", family="gemini", provider="google"),
            _profile("profile:gemini-3-1-pro-preview", family="gemini",
                     provider="google"),
            _profile("profile:claude-opus-5", family="claude", provider="claude"),
            _profile("model-profile:claude-opus-5", family="claude",
                     provider="claude", expired=True),
            _profile("profile:gpt-6-astra", family="gpt", provider="openai"),
        )
    }


DELIVERABLE = ["profile:claude-opus-5", "profile:deepseek-v4-flash"]


def _policy(policy_id: str, *, brain=None, overrides=None,
            deliverable=None) -> dict:
    tiers = {
        "brain": list(LIVE_BRAIN if brain is None else brain),
        "cheap": ["profile:deepseek-v4-flash"],
        "verifier": ["profile:gemini-3-8-flash"],
    }
    # 2026-09-17: a host that has not been split yet declares three tiers and
    # pins the three drafting stages by hand; one that has, declares four.
    if deliverable is not None:
        tiers["deliverable"] = list(deliverable)
    return {
        "id": policy_id,
        "policy_version_ref": f"{policy_id.replace('policy:', 'policy-version:')}:43",
        "fallback_chains": {"tiers": tiers},
        "purpose_overrides": dict(overrides or {}),
    }


class RepairPlanTests(unittest.TestCase):
    def setUp(self) -> None:
        self.profiles = _catalog()
        self.now = NOW.isoformat(timespec="microseconds")
        # These regressions replay the original September 16 incident.
        from unittest.mock import patch
        historical = {ANTIGRAVITY: 30_000, ANTIGRAVITY_HIGH: 30_000}
        guard = patch.object(REPAIR, "MEASURED_INPUT_BOUNDS", historical)
        guard.start()
        self.addCleanup(guard.stop)

    def test_the_live_brain_chain_loses_exactly_the_three_links_that_cannot_serve(self):
        repaired = REPAIR.repair_chain(
            LIVE_BRAIN, tier="brain", profiles=self.profiles, now=self.now)
        self.assertEqual(repaired["after"], [
            "profile:deepseek-v4-flash",
            "profile:zai-glm-5-3",
            "profile:claude-opus-5",
        ])
        joined = " ".join(repaired["notes"])
        self.assertIn("429", joined)
        self.assertIn("实测输入上限 30000", joined)
        self.assertIn("model-profile:claude-opus-5 → profile:claude-opus-5", joined)

    def test_keep_astra_puts_it_last_instead_of_removing_it(self) -> None:
        repaired = REPAIR.repair_chain(
            LIVE_BRAIN, tier="brain", profiles=self.profiles, now=self.now,
            keep_astra=True)
        self.assertEqual(repaired["after"][-1], "profile:gpt-6-astra")
        self.assertEqual(repaired["after"][:3], [
            "profile:deepseek-v4-flash", "profile:zai-glm-5-3",
            "profile:claude-opus-5",
        ])

    def test_a_cooled_profile_is_taken_out_of_the_chain_as_well(self) -> None:
        repaired = REPAIR.repair_chain(
            LIVE_BRAIN, tier="brain", profiles=self.profiles, now=self.now,
            cooled={"profile:zai-glm-5-3": {"reason": "provider_rate_limited"}})
        self.assertNotIn("profile:zai-glm-5-3", repaired["after"])

    def test_the_input_ceiling_only_disqualifies_the_reasoning_tier(self) -> None:
        # The same endpoint is fine for batch reading, which is chunked.
        cheap = REPAIR.repair_chain(
            ["profile:gemini-3-8-flash-antigravity-high"], tier="cheap",
            profiles=self.profiles, now=self.now)
        self.assertEqual(cheap["after"],
                         ["profile:gemini-3-8-flash-antigravity-high"])

    def test_it_refuses_to_publish_an_empty_chain(self) -> None:
        with self.assertRaises(REPAIR.RepairError):
            REPAIR.repair_chain(["profile:gpt-6-astra"], tier="brain",
                                profiles=self.profiles, now=self.now)

    def test_the_plan_is_one_tier_save_plus_the_transport_pin(self) -> None:
        policies = [_policy(f"model-routing-policy:p{index}") for index in range(3)]
        plan = REPAIR.plan_repair(policies, self.profiles, now=self.now)
        self.assertEqual(plan["conflicts"], [])
        self.assertEqual(len(plan["diffs"]), 3)
        tiers = [item for item in plan["publish"] if item["kind"] == "tier"]
        purposes = [item for item in plan["publish"] if item["kind"] == "purpose"]
        self.assertEqual(len(tiers), 1)
        self.assertEqual(tiers[0]["tier"], "brain")
        self.assertEqual(tiers[0]["chain"], [
            "profile:deepseek-v4-flash", "profile:zai-glm-5-3",
            "profile:claude-opus-5",
        ])
        # 2026-09-17: only the language checker's transport pin is left. The
        # three schema-bound drafts are the deliverable tier now, so this
        # script does not republish them as per-stage pins -- the next tier
        # save would only drop them again. It says which policies still need
        # splitting instead.
        self.assertEqual([item["purpose"] for item in purposes],
                         ["research_language_check"])
        self.assertEqual(purposes[0]["chain"], [ANTIGRAVITY, ANTIGRAVITY_HIGH])
        self.assertTrue(any("split_deliverable_tier" in note
                            for note in plan["notes"]))

    def test_a_split_host_repairs_the_deliverable_chain_like_any_other(self) -> None:
        # After the split there is nothing special to say about the drafting
        # stages: their chain is a tier chain and the ordinary tier pass owns
        # it, including taking the rate-limited endpoint back out of it.
        policies = [_policy("model-routing-policy:p0",
                            deliverable=[*DELIVERABLE, "profile:gpt-6-astra"])]
        plan = REPAIR.plan_repair(policies, self.profiles, now=self.now)
        self.assertEqual(plan["conflicts"], [])
        self.assertEqual(plan["notes"], [])
        tiers = {item["tier"]: item["chain"] for item in plan["publish"]
                 if item["kind"] == "tier"}
        self.assertEqual(tiers["deliverable"], DELIVERABLE)

    def test_the_long_prompt_ceiling_also_applies_to_the_deliverable_tier(self) -> None:
        # A dossier prompt is not chunked either, so the 30k transport ceiling
        # disqualifies the same endpoint here as in the brain tier.
        repaired = REPAIR.repair_chain(
            [ANTIGRAVITY_HIGH, "profile:claude-opus-5"], tier="deliverable",
            profiles=self.profiles, now=self.now)
        self.assertEqual(repaired["after"], ["profile:claude-opus-5"])
        self.assertIn("实测输入上限 30000", " ".join(repaired["notes"]))

    def test_running_it_again_on_a_repaired_host_publishes_nothing(self) -> None:
        repaired_chain = ["profile:deepseek-v4-flash", "profile:zai-glm-5-3",
                          "profile:claude-opus-5"]
        overrides = {
            "research_language_check": {
                "mode": "explicit", "chain": [ANTIGRAVITY, ANTIGRAVITY_HIGH]},
        }
        policies = [_policy("model-routing-policy:p0", brain=repaired_chain,
                            overrides=overrides, deliverable=DELIVERABLE)]
        plan = REPAIR.plan_repair(policies, self.profiles, now=self.now)
        self.assertEqual(plan["diffs"], [])
        self.assertEqual(plan["publish"], [])
        self.assertIn("没有需要修复的链", REPAIR.render(plan))

    def test_policies_that_disagree_are_a_decision_for_a_human(self) -> None:
        policies = [
            _policy("model-routing-policy:p0"),
            _policy("model-routing-policy:p1",
                    brain=["profile:zai-glm-5-3", "profile:deepseek-v4-flash"]),
        ]
        plan = REPAIR.plan_repair(policies, self.profiles, now=self.now)
        self.assertTrue(any("brain" in item for item in plan["conflicts"]))
        self.assertFalse([item for item in plan["publish"]
                          if item["kind"] == "tier"])

    def test_a_drafting_pin_matching_its_verifier_s_family_is_refused(self) -> None:
        # Still enforced for a stage named by hand with --structured-purpose,
        # i.e. one the deliverable tier does not carry.
        policies = [_policy("model-routing-policy:p0", overrides={
            "investment_memo_verifier": {
                "mode": "explicit",
                "chain": ["profile:claude-opus-5"],
            },
        })]
        plan = REPAIR.plan_repair(policies, self.profiles, now=self.now,
                                  structured_purposes=("investment_memo",))
        self.assertTrue(any("起草与复核必须不同厂商" in item
                            for item in plan["conflicts"]))
        self.assertFalse([item for item in plan["publish"]
                          if item.get("purpose") == "investment_memo"])


class TransportPinTests(unittest.TestCase):
    """A4b: the publication language checker gets a chain that can serve it."""

    CHECKER = "research_language_check"
    REQUIRED = {"provider": "antigravity-cli-gateway",
                "model": "antigravity-cli-gateway/gemini-3.8-flash"}

    def _profiles(self, *, high: bool = True) -> dict[str, dict]:
        profiles = _catalog()
        if not high:
            profiles.pop(ANTIGRAVITY_HIGH)
        return profiles

    def _without_antigravity(self) -> dict[str, dict]:
        profiles = _catalog()
        profiles.pop(ANTIGRAVITY)
        profiles.pop(ANTIGRAVITY_HIGH)
        return profiles

    def test_the_head_serves_the_contract_and_the_sibling_backs_it_up(self) -> None:
        pin = REPAIR.transport_pin(
            self.CHECKER, self.REQUIRED, self._profiles(),
            now=NOW.isoformat(timespec="microseconds"))
        self.assertIsNone(pin["problem"])
        self.assertEqual(pin["chain"], [ANTIGRAVITY, ANTIGRAVITY_HIGH])
        self.assertEqual(pin["notes"], [])

    def test_a_cooled_profile_is_moved_behind_the_one_that_can_serve(self) -> None:
        pin = REPAIR.transport_pin(
            self.CHECKER, self.REQUIRED, self._profiles(),
            now=NOW.isoformat(timespec="microseconds"),
            cooled={ANTIGRAVITY: {"reason": "x"}})
        self.assertEqual(pin["chain"][0], ANTIGRAVITY_HIGH)
        self.assertEqual(pin["chain"][1], ANTIGRAVITY)
        self.assertTrue(any("正在冷却" in note for note in pin["notes"]))

    def test_a_single_serving_profile_publishes_alone_and_says_so(self) -> None:
        pin = REPAIR.transport_pin(
            self.CHECKER, self.REQUIRED, self._profiles(high=False),
            now=NOW.isoformat(timespec="microseconds"))
        self.assertEqual(pin["chain"], [ANTIGRAVITY])
        self.assertTrue(any("没有后备可用" in note for note in pin["notes"]))

    def test_no_serving_profile_at_all_is_refused_rather_than_guessed(self) -> None:
        pin = REPAIR.transport_pin(
            self.CHECKER, self.REQUIRED, self._without_antigravity(),
            now=NOW.isoformat(timespec="microseconds"))
        self.assertEqual(pin["chain"], [])
        self.assertIn("没有任何当前可路由的档案", pin["problem"])

    def test_the_plan_publishes_the_checker_pin_with_its_reason(self) -> None:
        plan = REPAIR.plan_repair(
            [_policy("model-routing-policy:p0")], self._profiles(),
            now=NOW.isoformat(timespec="microseconds"))
        pin = next(item for item in plan["publish"]
                   if item.get("purpose") == self.CHECKER)
        self.assertEqual(pin["chain"][0], ANTIGRAVITY)
        self.assertIn("antigravity-cli-gateway/gemini-3.8-flash", pin["reason"])
        rendered = REPAIR.render(plan)
        self.assertIn("research_language_check", rendered)
        self.assertIn("下游按供应商和模型逐次核对", rendered)

    def test_an_already_pinned_checker_is_not_republished(self) -> None:
        policies = [_policy("model-routing-policy:p0", overrides={
            self.CHECKER: {"mode": "explicit",
                           "chain": [ANTIGRAVITY, ANTIGRAVITY_HIGH]},
        })]
        plan = REPAIR.plan_repair(policies, self._profiles(),
                                  now=NOW.isoformat(timespec="microseconds"))
        self.assertFalse([item for item in plan["publish"]
                          if item.get("purpose") == self.CHECKER])


class PlannerOutputFloorTests(unittest.TestCase):
    """A6: a new environment never starts under the cap Gemini cannot answer in."""

    def test_the_floor_is_raised_and_a_larger_owner_setting_is_kept(self) -> None:
        from dalton_core.workspace_service_setup import (
            MIN_PLANNER_MAX_OUTPUT_TOKENS, _with_output_floor,
        )

        self.assertEqual(MIN_PLANNER_MAX_OUTPUT_TOKENS, 16_000)
        raised = _with_output_floor({"max_output_tokens": 4_000, "max_cost_usd": 1.0})
        self.assertEqual(raised["max_output_tokens"], 16_000)
        self.assertEqual(raised["max_cost_usd"], 1.0)
        kept = _with_output_floor({"max_output_tokens": 32_000})
        self.assertEqual(kept["max_output_tokens"], 32_000)
        self.assertEqual(_with_output_floor(None)["max_output_tokens"], 16_000)


class TemplateChainGuardTests(unittest.TestCase):
    """A6: the export refuses to hand a new environment an unroutable chain."""

    def _router(self, tmp: Path):
        from dalton_core.model_router import ModelRouter
        from dalton_core.openclaw_catalog_reconcile import sync_openclaw_model_catalog
        from tests.test_openclaw_catalog_reconcile import _config

        router = ModelRouter(tmp / "router.sqlite", clock=lambda: NOW)
        self.addCleanup(router.close)
        sync_openclaw_model_catalog(router, _config(), checked_at=NOW,
                                    availability_ttl=timedelta(days=3650))
        return router

    def test_a_cooled_or_missing_link_stops_the_export_with_its_name(self) -> None:
        from dalton_core.workspace_model_setup import (
            WorkspaceModelSetupError, _assert_pinned_chains_are_routable,
        )

        with tempfile.TemporaryDirectory() as directory:
            router = self._router(Path(directory))
            good = _policy("model-routing-policy:ok", brain=[
                "profile:deepseek-v4-flash", "profile:claude-fable-5-1"])
            _assert_pinned_chains_are_routable(router, [good])

            router.record_provider_outcome(
                profile_id="profile:gpt-6-astra", outcome="provider_failure",
                failure_code="RATE_LIMITED")
            with self.assertRaises(WorkspaceModelSetupError) as raised:
                _assert_pinned_chains_are_routable(router, [_policy(
                    "model-routing-policy:cooled",
                    brain=["profile:gpt-6-astra", "profile:claude-fable-5-1"])])
            self.assertIn("profile:gpt-6-astra", str(raised.exception))

            with self.assertRaises(WorkspaceModelSetupError) as raised:
                _assert_pinned_chains_are_routable(router, [_policy(
                    "model-routing-policy:stale",
                    brain=["model-profile:claude-opus-5"])])
            self.assertIn("没有档案", str(raised.exception))

class UpdatedAntigravityBoundsTests(unittest.TestCase):
    def test_verified_larger_transport_is_retained_in_reasoning_and_deliverable_chains(self):
        for tier in ("brain", "deliverable"):
            repaired = REPAIR.repair_chain(
                [ANTIGRAVITY_HIGH, "profile:claude-opus-5"], tier=tier,
                profiles=_catalog(), now=NOW.isoformat(timespec="microseconds"))
            self.assertEqual(repaired["after"], [ANTIGRAVITY_HIGH, "profile:claude-opus-5"])


if __name__ == "__main__":
    unittest.main()
