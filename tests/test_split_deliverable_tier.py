"""2026-09-17: splitting 交付物起草 out of the three hand-written pins.

The planning half of ``scripts/split_deliverable_tier.py`` is pure -- given the
catalog and the policies, what the one tier save should carry and what it will
remove -- so it is tested here against the exact shape the live host had on
2026-09-16, without a writer and without a database.  The applying half goes
through ``set_tier_selection``, which has its own tests in test_model_selection.
"""

from __future__ import annotations

import importlib.util
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).parents[1]
SPEC = importlib.util.spec_from_file_location(
    "split_deliverable_tier", ROOT / "scripts/split_deliverable_tier.py")
SPLIT = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(SPLIT)

NOW = datetime(2026, 9, 17, 8, 0, tzinfo=timezone.utc)
MOMENT = NOW.isoformat(timespec="microseconds")
OPUS = "profile:claude-opus-5"
DEEPSEEK = "profile:deepseek-v4-flash"
#: What the three overrides named on the live host, set by hand on 2026-09-16.
LIVE_PIN = [OPUS, DEEPSEEK]
ANTIGRAVITY = "profile:gemini-3-8-flash-antigravity"


def _profile(profile_id: str, *, family: str, expired: bool = False) -> dict:
    checked = NOW - timedelta(days=33 if expired else 1)
    valid = checked + timedelta(days=1 if expired else 8)
    return {
        "id": profile_id,
        "family": family,
        "availability": {
            "state": "available",
            "checked_at": checked.isoformat(timespec="microseconds"),
            "valid_until": valid.isoformat(timespec="microseconds"),
        },
    }


def _catalog() -> dict[str, dict]:
    return {
        profile["id"]: profile
        for profile in (
            _profile(OPUS, family="claude"),
            _profile("model-profile:claude-opus-5", family="claude", expired=True),
            _profile(DEEPSEEK, family="deepseek"),
            _profile("profile:zai-glm-5-3", family="zai"),
            _profile("profile:gemini-3-8-flash", family="gemini"),
            _profile(ANTIGRAVITY, family="gemini"),
        )
    }


def _policy(policy_id: str, *, pin=None, deliverable=None, overrides=None) -> dict:
    """A policy version in the pre-split shape unless told otherwise."""

    tiers = {
        "brain": [DEEPSEEK, "profile:zai-glm-5-3", OPUS],
        "cheap": [DEEPSEEK],
        "verifier": ["profile:gemini-3-8-flash"],
    }
    if deliverable is not None:
        tiers["deliverable"] = list(deliverable)
    held = {
        purpose: {"mode": "explicit", "chain": list(LIVE_PIN if pin is None else pin)}
        for purpose in ("dossier", "debate_map", "model_spec")
    } if pin is not False else {}
    held["research_language_check"] = {"mode": "explicit", "chain": [ANTIGRAVITY]}
    held.update(overrides or {})
    return {
        "id": policy_id,
        "policy_version_ref": f"{policy_id.replace('policy:', 'policy-version:')}:47",
        "fallback_chains": {"tiers": tiers},
        "purpose_overrides": held,
    }


class PlanSplitTests(unittest.TestCase):
    def setUp(self) -> None:
        self.profiles = _catalog()

    def test_the_tier_takes_the_chain_the_dossier_pin_holds_today(self) -> None:
        policies = [_policy(f"model-routing-policy:p{index}") for index in range(3)]
        plan = SPLIT.plan_split(policies, self.profiles, now=MOMENT)
        self.assertEqual(plan["conflicts"], [])
        self.assertEqual(plan["chain"], LIVE_PIN)
        self.assertEqual(plan["purposes"],
                         ["debate_map", "dossier", "model_spec"])
        self.assertEqual(plan["publish"],
                         [{"kind": "tier", "tier": "deliverable",
                           "chain": LIVE_PIN}])

    def test_the_before_and_after_name_exactly_what_moves(self) -> None:
        plan = SPLIT.plan_split([_policy("model-routing-policy:p0")],
                                self.profiles, now=MOMENT)
        diff = plan["diffs"][0]
        self.assertNotIn("deliverable", diff["tiers_before"])
        self.assertEqual(diff["tiers_after"]["deliverable"], LIVE_PIN)
        # Every other tier is byte-identical: this save adds a tier, it does
        # not reorder the ones the owner already set.
        for tier, links in diff["tiers_before"].items():
            self.assertEqual(diff["tiers_after"][tier], links, tier)
        self.assertEqual(diff["removed_overrides"],
                         ["debate_map", "dossier", "model_spec"])
        self.assertEqual(diff["kept_overrides"], ["research_language_check"])
        self.assertEqual(list(diff["overrides_after"]), ["research_language_check"])
        self.assertEqual(diff["overrides_after"]["research_language_check"],
                         [ANTIGRAVITY])
        rendered = SPLIT.render(plan)
        self.assertIn("deliverable： profile:claude-opus-5 → profile:deepseek-v4-flash ←新增",
                      rendered)
        self.assertIn("移除：debate_map、dossier、model_spec", rendered)
        self.assertIn("保留：research_language_check", rendered)

    def test_running_it_again_on_a_split_host_publishes_nothing(self) -> None:
        policies = [_policy("model-routing-policy:p0", pin=False,
                            deliverable=LIVE_PIN)]
        plan = SPLIT.plan_split(policies, self.profiles, now=MOMENT)
        self.assertEqual(plan["conflicts"], [])
        self.assertEqual(plan["publish"], [])
        self.assertIn("没有需要拆分的", SPLIT.render(plan))

    def test_a_tier_already_declared_but_still_pinned_is_published_again(self) -> None:
        # Half-applied: the chain is there and the pins never went. One more
        # save drops them, which is the half that matters.
        policies = [_policy("model-routing-policy:p0", deliverable=LIVE_PIN)]
        plan = SPLIT.plan_split(policies, self.profiles, now=MOMENT)
        self.assertEqual(plan["publish"],
                         [{"kind": "tier", "tier": "deliverable",
                           "chain": LIVE_PIN}])

    def test_policies_that_pin_different_models_are_a_decision_for_a_human(self) -> None:
        policies = [
            _policy("model-routing-policy:p0"),
            _policy("model-routing-policy:p1", pin=[DEEPSEEK, OPUS]),
        ]
        plan = SPLIT.plan_split(policies, self.profiles, now=MOMENT)
        self.assertTrue(any("--chain" in item for item in plan["conflicts"]))
        self.assertEqual(plan["publish"], [])

    def test_naming_the_chain_settles_that_disagreement(self) -> None:
        policies = [
            _policy("model-routing-policy:p0"),
            _policy("model-routing-policy:p1", pin=[DEEPSEEK, OPUS]),
        ]
        plan = SPLIT.plan_split(policies, self.profiles, now=MOMENT,
                                chain=[OPUS, DEEPSEEK])
        self.assertEqual(plan["conflicts"], [])
        self.assertEqual(plan["publish"][0]["chain"], LIVE_PIN)

    def test_an_expired_id_is_published_as_the_live_one(self) -> None:
        # ``model-profile:claude-opus-5`` and ``profile:claude-opus-5`` are the
        # same model under two id shapes; publishing the dead one publishes a
        # link that can never be selected.
        policies = [_policy("model-routing-policy:p0",
                            pin=["model-profile:claude-opus-5", DEEPSEEK])]
        plan = SPLIT.plan_split(policies, self.profiles, now=MOMENT)
        self.assertEqual(plan["chain"], LIVE_PIN)
        self.assertTrue(any("已过期" in note for note in plan["notes"]))

    def test_a_model_the_catalog_does_not_hold_is_refused(self) -> None:
        policies = [_policy("model-routing-policy:p0",
                            pin=["profile:nothing-like-this"])]
        plan = SPLIT.plan_split(policies, self.profiles, now=MOMENT)
        self.assertTrue(any("没有当前有效的档案" in item
                            for item in plan["conflicts"]))
        self.assertEqual(plan["publish"], [])

    def test_a_chain_its_verifiers_could_not_stay_independent_of_is_refused(self) -> None:
        policies = [_policy("model-routing-policy:p0", overrides={
            "dossier_verifier": {"mode": "explicit", "chain": [OPUS]},
        })]
        plan = SPLIT.plan_split(policies, self.profiles, now=MOMENT)
        self.assertTrue(any("起草与复核必须不同厂商" in item
                            for item in plan["conflicts"]))
        self.assertEqual(plan["publish"], [])

    def test_the_live_verifier_pins_do_not_clash(self) -> None:
        policies = [_policy("model-routing-policy:p0", overrides={
            f"{purpose}_verifier": {"mode": "explicit",
                                    "chain": ["profile:gemini-3-8-flash"]}
            for purpose in ("dossier", "debate_map")
        })]
        plan = SPLIT.plan_split(policies, self.profiles, now=MOMENT)
        self.assertEqual(plan["conflicts"], [])
        self.assertEqual(plan["publish"][0]["chain"], LIVE_PIN)

    def test_nothing_to_read_the_chain_off_is_said_rather_than_guessed(self) -> None:
        policies = [_policy("model-routing-policy:p0", pin=False)]
        plan = SPLIT.plan_split(policies, self.profiles, now=MOMENT)
        self.assertTrue(any("--chain" in item for item in plan["conflicts"]))
        self.assertEqual(plan["publish"], [])


class PurposeSetTests(unittest.TestCase):
    def test_the_tier_members_come_from_the_code_map(self) -> None:
        from dalton_core.model_fallback_chain import TIER_DELIVERABLE, purpose_tiers

        self.assertEqual(
            SPLIT.deliverable_purposes(),
            tuple(sorted(name for name, tier in purpose_tiers().items()
                         if tier == TIER_DELIVERABLE)))
        self.assertEqual(SPLIT.deliverable_purposes(),
                         ("debate_map", "dossier", "model_spec"))


if __name__ == "__main__":
    unittest.main()
