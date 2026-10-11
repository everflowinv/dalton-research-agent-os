"""Batch C2 item 4: long-context purposes route in every environment.

The STRL Initial Screen sends the whole material pack (up to ~600K prompt
bytes) and asks for up to 16K output.  Every live environment's stored model
config carried ``purpose_call_budgets`` of 1 USD and the host's shared cost
policy defaulted every purpose to 1 USD, so the router refused every
long-context link on cost.  The code floor wins over both; the chain for the
whole-pack purposes tries the links that can carry the pack first.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from dalton_core import call_budget as cb
from dalton_core.model_fallback_chain import TIER_BRAIN, TIER_VERIFIER, tier_for
from dalton_core.model_router import long_context_capable, resolve_chain
from dalton_core.store import content_hash


def _profile(pid: str, *, input_limit: int, output_limit: int = 64_000, window: int = 1_000_000,
             provider: str = "openai", rates: tuple[float, float] = (2.0, 10.0)) -> dict:
    return {"id": pid, "provider": provider, "status": "active",
            "limits": {"max_input_tokens": input_limit, "max_output_tokens": output_limit,
                       "max_total_tokens": input_limit + output_limit, "max_cost_usd": 250.0},
            "context": {"max_context_tokens": window, "max_output_tokens": output_limit},
            "cost": {"currency": "USD", "input_per_million_usd": rates[0],
                     "output_per_million_usd": rates[1]}}


def _shared_policy(directory: Path, default: float = 1.0) -> str:
    wire = {"schema_version": "dalton-shared-call-budget-policy-0.1", "default_max_cost_usd": default,
            "purpose_max_cost_usd": {"plan": 1.5}, "revision": 4, "prior_hash": None,
            "updated_at": "2026-10-01T00:00:00+00:00", "actor_ref": "human:owner"}
    wire["content_hash"] = content_hash(wire)
    path = directory / "model-call-budget-policy.json"
    path.write_text(json.dumps(wire), encoding="utf-8")
    return str(path)


class LongContextFloorTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        # Product defaults may exceed the shared minimum (Initial Screen now
        # asks for more output). Read the installed catalog independently of
        # the resolver so every field must still match its purpose contract.
        self.product_defaults = json.loads(
            Path(cb.__file__).with_name("call_budget_defaults.json").read_text(
                encoding="utf-8"))["purposes"]
        # What every live environment stored on 2026-10-09.
        self.live_like = {
            "purpose_call_budgets": {purpose: {"max_cost_usd": 1.0} for purpose in cb.LONG_CONTEXT_PURPOSES}
            | {"draft": {"max_cost_usd": 1.0}},
            "shared_call_budget_policy_path": _shared_policy(self.dir),
        }

    def test_floor_holds_the_strl_prompt_and_its_worst_link(self) -> None:
        floor = cb.LONG_CONTEXT_CALL_FLOOR
        self.assertGreaterEqual(floor["max_input_tokens"], 600_000)
        self.assertGreaterEqual(floor["max_output_tokens"], 16_000)
        from dalton_core.cockpit_model import profile_call_ceiling_usd

        dearest = _profile("profile:x", input_limit=900_000, provider="claude-cli-gateway",
                           rates=(10.0, 50.0))
        ceiling = profile_call_ceiling_usd(dearest, prompt_bytes=floor["max_input_tokens"],
                                           max_output_tokens=floor["max_output_tokens"])
        self.assertGreaterEqual(Decimal(str(floor["max_cost_usd"])), ceiling)
        self.assertLess(Decimal(str(floor["max_cost_usd"])) - ceiling, 1)  # whole dollar, not a guess

    def test_stored_config_and_shared_policy_cannot_go_below_the_floor(self) -> None:
        for purpose in sorted(cb.LONG_CONTEXT_PURPOSES):
            with self.subTest(purpose=purpose):
                resolved = cb.resolve_call_budget(self.live_like, purpose, defaults={
                    "max_input_tokens": 120_000, "max_output_tokens": 3_000, "max_cost_usd": 1.0,
                    "timeout_seconds": 120})
                default = cb.default_call_budget(purpose)
                self.assertEqual(set(resolved), set(cb.LONG_CONTEXT_CALL_FLOOR))
                self.assertEqual(set(default), set(cb.LONG_CONTEXT_CALL_FLOOR))
                for field, minimum in cb.LONG_CONTEXT_CALL_FLOOR.items():
                    with self.subTest(field=field):
                        expected = max(minimum, self.product_defaults[purpose][field])
                        self.assertEqual(default[field], expected)
                        # This fixture's stored/shared cost override is $1;
                        # the authorized product cost floor still wins.
                        self.assertEqual(resolved[field],
                                         minimum if field == "max_cost_usd" else expected)

    def test_a_higher_stored_value_still_wins(self) -> None:
        config = {"purpose_call_budgets": {"initial_screen": {"max_cost_usd": 30.0,
                                                              "max_input_tokens": 800_000}}}
        resolved = cb.resolve_call_budget(config, "initial_screen", defaults=cb.LONG_CONTEXT_CALL_FLOOR)
        self.assertEqual(resolved["max_cost_usd"], 30.0)
        self.assertEqual(resolved["max_input_tokens"], 800_000)

    def test_other_purposes_are_untouched(self) -> None:
        resolved = cb.resolve_call_budget(self.live_like, "draft", defaults={
            "max_input_tokens": 120_000, "max_output_tokens": 1_800, "max_cost_usd": 1.0,
            "timeout_seconds": 120})
        self.assertEqual(resolved["max_cost_usd"], 1.0)
        self.assertEqual(resolved["max_input_tokens"], 120_000)
        self.assertIsNone(cb.call_budget_floor("event_judgement"))

    def test_product_lanes_share_the_floor_and_required_run_arithmetic(self) -> None:
        for purpose in ("investment_memo", "investment_memo_verifier", "dossier", "dossier_verifier",
                        "debate_map", "debate_map_verifier"):
            self.assertEqual(cb.call_budget_floor(purpose), cb.LONG_CONTEXT_CALL_FLOOR)
        for purpose, calls in (("investment_memo", 5), ("dossier", 4), ("debate_map", 2)):
            with self.subTest(purpose=purpose):
                config = {**self.live_like, "run_budget": {"max_cost_usd": 1},
                          "purpose_run_budgets": {purpose: {"max_cost_usd": 0.5}}}
                resolved = cb.resolve_run_budget(config, purpose, defaults={"max_cost_usd": 1})
                self.assertEqual(resolved["max_cost_usd"], calls * cb.LONG_CONTEXT_CALL_FLOOR["max_cost_usd"])
                self.assertEqual(cb.default_run_budget(purpose)["max_cost_usd"], resolved["max_cost_usd"])

    def test_run_floor_uses_unit_count_and_higher_independent_verifier_cap(self) -> None:
        budgets = {"dossier": {"max_cost_usd": 20}, "dossier_verifier": {"max_cost_usd": 30}}
        config = {"purpose_run_budgets": {"dossier": {"max_units": 12, "max_cost_usd": 2.5}}}
        resolved = cb.resolve_run_budget(config, "dossier", defaults={"max_cost_usd": 2.5},
                                         call_budgets=budgets)
        self.assertEqual(resolved["max_cost_usd"], 12 * 20 + 30)
        memo = cb.resolve_run_budget({"run_budget": {"max_cost_usd": 6}}, "investment_memo",
                                     defaults={"max_cost_usd": 6}, call_budgets={
                                         "investment_memo": {"max_cost_usd": 20},
                                         "investment_memo_verifier": {"max_cost_usd": 30}})
        self.assertEqual(memo["max_cost_usd"], 4 * 20 + 30)

    def test_run_floor_preserves_higher_caps_and_works_without_packaged_defaults(self) -> None:
        config = {"run_budget": {"max_cost_usd": 200}, "purpose_call_budgets": {
            "investment_memo": {"max_cost_usd": 20}, "investment_memo_verifier": {"max_cost_usd": 30}}}
        self.assertEqual(cb.resolve_run_budget(config, "investment_memo", defaults={"max_cost_usd": 6})[
            "max_cost_usd"], 200)
        with patch.object(Path, "is_file", return_value=False):
            budget = cb.resolve_run_budget({}, "investment_memo", defaults={"max_cost_usd": 6, "max_units": 4})
        self.assertEqual(budget["max_cost_usd"], 70)

    def test_cockpit_model_budget_for_the_strl_purposes(self) -> None:
        from dalton_core.cockpit_model import CockpitModel

        model = CockpitModel.__new__(CockpitModel)
        model.config = self.live_like
        model.max_input_tokens, model.max_output_tokens = 600_000, 16_000
        model.max_cost_usd, model.timeout_seconds = None, 120
        for purpose in sorted(cb.LONG_CONTEXT_PURPOSES):
            with self.subTest(purpose=purpose):
                resolved = model.budget_for(purpose)
                self.assertEqual(set(resolved), set(cb.LONG_CONTEXT_CALL_FLOOR))
                for field, minimum in cb.LONG_CONTEXT_CALL_FLOOR.items():
                    with self.subTest(field=field):
                        expected = (minimum if field == "max_cost_usd" else
                                    max(minimum, self.product_defaults[purpose][field]))
                        self.assertEqual(resolved[field], expected)


class LongContextChainTest(unittest.TestCase):
    PROFILES = {
        "profile:claude-opus-5": _profile("profile:claude-opus-5", input_limit=872_000,
                                          provider="claude-cli-gateway"),
        "profile:muse": _profile("profile:muse", input_limit=256_000),
        # declared 983K, measured transport bound 170K (model_profile_bounds)
        "profile:gemini-3-8-flash-antigravity-high": _profile(
            "profile:gemini-3-8-flash-antigravity-high", input_limit=983_040, output_limit=65_536,
            window=1_048_576),
        "profile:zai-glm-5-3": _profile("profile:zai-glm-5-3", input_limit=868_928),
        "profile:gemini-3-1-pro-preview": _profile("profile:gemini-3-1-pro-preview", input_limit=983_040,
                                                   output_limit=65_536, window=1_048_576),
    }
    POLICY = {"fallback_chains": {"tiers": {
        "brain": ["profile:claude-opus-5", "profile:muse", "profile:gemini-3-8-flash-antigravity-high",
                        "profile:zai-glm-5-3"],
        "verifier": ["profile:muse", "profile:gemini-3-1-pro-preview"]}}}

    def test_strl_purposes_have_tiers(self) -> None:
        self.assertEqual(tier_for("initial_screen"), TIER_BRAIN)
        self.assertEqual(tier_for("initial_screen"), tier_for("draft"))
        self.assertEqual(tier_for("initial_screen_verifier"), TIER_VERIFIER)

    def test_capability_uses_the_effective_bound(self) -> None:
        self.assertTrue(long_context_capable(self.PROFILES["profile:claude-opus-5"]))
        self.assertTrue(long_context_capable(self.PROFILES["profile:gemini-3-1-pro-preview"]))
        self.assertFalse(long_context_capable(self.PROFILES["profile:muse"]))
        self.assertFalse(long_context_capable(self.PROFILES["profile:gemini-3-8-flash-antigravity-high"]))
        self.assertFalse(long_context_capable(None))

    def test_whole_pack_purposes_try_long_context_links_first(self) -> None:
        for purpose in sorted(cb.LONG_CONTEXT_PURPOSES):
            with self.subTest(purpose=purpose):
                tier = "verifier" if purpose.endswith("_verifier") else "brain"
                resolved = resolve_chain(self.POLICY, tier=tier, purpose=purpose, profiles=self.PROFILES)
                expected = (("profile:gemini-3-1-pro-preview", "profile:muse") if tier == "verifier" else
                            ("profile:claude-opus-5", "profile:zai-glm-5-3", "profile:muse",
                             "profile:gemini-3-8-flash-antigravity-high"))
                self.assertEqual(resolved["chain"], expected)

    def test_other_purposes_keep_the_owner_order(self) -> None:
        for purpose in ("event_judgement", "draft"):
            resolved = resolve_chain(self.POLICY, tier="brain", purpose=purpose, profiles=self.PROFILES)
            self.assertEqual(resolved["chain"], tuple(self.POLICY["fallback_chains"]["tiers"]["brain"]))
            self.assertNotIn("long_context_preferred", resolved)


if __name__ == "__main__":
    unittest.main()
