"""2026-09-25: a dossier unit reserved $1 + $1 against a $2.50 run and cost $0.27.

Live, ``588dc5aaf670f1e50b5b9983`` (DXC, 06:44Z): ``1 个单元未起草：本次运行
的成本上限 2500000 micros 已经不够再付一次（起草 1000000 + 校验 1000000）``
after spending 541,524 micros, and the demand_drivers contract repair was
refused "1958476 micros left, 2000000 reserved".  A call is now reserved at
what its route can cost, by ``cockpit_model``'s ceiling rule.
"""

from __future__ import annotations

import contextlib
import json
import unittest
from decimal import Decimal
from unittest import mock

from dalton_core import company_dossier_cli as cli
from dalton_core.cockpit_model import profile_call_ceiling_usd
from tests import test_dossier_lane as _lane

OPUS_GATEWAY = {"provider": "claude-cli-gateway", "cost": {
    "input_per_million_usd": 4.0, "output_per_million_usd": 20.0}}
FLASH = {"provider": "google", "cost": {
    "input_per_million_usd": 0.75, "output_per_million_usd": 3.75}}


class Priced:
    """The two read-only ``CockpitModel`` methods the estimate uses."""

    def __init__(self, profile, *, tier="deliverable"):
        self.config = {"model_router_db": "/nonexistent/router.sqlite"}
        self.profile, self.tier, self.asked = profile, tier, []

    def _chain_tier(self, router, purpose):
        return self.tier

    def _chain_ceiling(self, router, tier, prompt_bytes, *, purpose, call_budget):
        self.asked.append((purpose, prompt_bytes, call_budget["max_output_tokens"]))
        ceiling = profile_call_ceiling_usd(self.profile, prompt_bytes=prompt_bytes,
                                           max_output_tokens=call_budget["max_output_tokens"])
        return int(ceiling * 1_000_000)


@contextlib.contextmanager
def router(*args, **kwargs):
    assert kwargs.get("read_only") is True
    yield object()


class RouteReserveTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(mock.patch("dalton_core.model_router.ModelRouter", router))

    def test_a_gateway_route_is_priced_with_its_hidden_prompt(self):
        model = Priced(OPUS_GATEWAY)
        reserve = cli.route_call_reserve_micros(
            model, "dossier", prompt_bytes=20_000, max_output_tokens=3_000,
            cap_micros=1_000_000)
        # (20,000 + 32,000) tokens x $8/M (cache-write) + 3,000 x $20/M.
        self.assertEqual(reserve, 476_000)
        self.assertEqual(model.asked, [("dossier", 20_000, 3_000)])

    def test_never_above_the_cap_and_unpriced_is_the_cap(self):
        self.assertEqual(cli.route_call_reserve_micros(
            Priced(OPUS_GATEWAY), "dossier", prompt_bytes=200_000,
            max_output_tokens=3_000, cap_micros=1_000_000), 1_000_000)
        self.assertEqual(cli.route_call_reserve_micros(
            Priced(OPUS_GATEWAY, tier=None), "dossier", prompt_bytes=1,
            max_output_tokens=1, cap_micros=1_000_000), 1_000_000)
        self.assertEqual(cli.route_call_reserve_micros(
            object(), "dossier", prompt_bytes=1, max_output_tokens=1,
            cap_micros=1_000_000), 1_000_000)
        self.assertEqual(cli.route_call_reserve_micros(
            None, "dossier", prompt_bytes=1, max_output_tokens=1,
            cap_micros=1_000_000), 1_000_000)

        def broken(*a, **k):
            raise OSError("router unreadable")
        with mock.patch("dalton_core.model_router.ModelRouter", broken):
            self.assertEqual(cli.route_call_reserve_micros(
                Priced(FLASH), "dossier", prompt_bytes=1, max_output_tokens=1,
                cap_micros=1_000_000), 1_000_000)


class PricedDrafter(_lane.FakeModel, Priced):
    """Drafts like the lane fake, costs what a live unit costs, is priced."""

    def __init__(self):
        _lane.FakeModel.__init__(self)
        Priced.__init__(self, OPUS_GATEWAY)

    def budget_for(self, purpose):
        return {"max_cost_usd": 1.0, "max_output_tokens": 3_000}

    def _envelope(self, text):
        return dict(super()._envelope(text), cost_micros=270_000)


class RunBudgetTests(unittest.TestCase):
    def setUp(self):
        self.harness = _lane.Harness()
        self.addCleanup(self.harness.close)
        self.enterContext(mock.patch("dalton_core.model_router.ModelRouter", router))
        verifier_config = self.harness.state_dir / "verifier.json"
        verifier_config.write_text(json.dumps(
            {"purpose_call_budgets": {"dossier_verifier": {"max_cost_usd": 1.0}}}),
            encoding="utf-8")
        self.enterContext(mock.patch.object(
            cli, "CockpitModel", lambda settings, **kw: Priced(FLASH, tier="verifier")))
        self.verifier_config = verifier_config

    def test_three_units_and_the_verifier_fit_where_one_or_two_did(self):
        summary = self.harness.run(
            model_factory=PricedDrafter, max_units=3,
            verifier_model_config_path=self.verifier_config)
        budget = summary["budget"]
        self.assertEqual(budget["run_cost_micros"], 2_500_000)
        self.assertEqual(budget["units_skipped_for_cost"], [])
        self.assertEqual(len(summary["units_drafted"]), 3)
        # Priced reserves, not the $1 + $1 caps.
        self.assertLess(budget["verifier_reserve_micros"], 200_000)
        self.assertTrue(all(value < 1_000_000
                            for value in budget["unit_reserves_micros"].values()))
        # Drafting three units still leaves a draft-plus-verify repair's room.
        self.assertGreaterEqual(budget["remaining_micros"], budget["unit_reserve_micros"])

    def test_without_pricing_the_old_caps_still_bound_the_run(self):
        summary = self.harness.run(max_units=3)
        self.assertEqual(summary["budget"]["unit_reserve_micros"],
                         summary["budget"]["unit_reserves_micros"][
                             sorted(summary["budget"]["unit_reserves_micros"])[0]]
                         + summary["budget"]["verifier_reserve_micros"])


if __name__ == "__main__":
    unittest.main()
