"""The two model vocabularies a lane used to have to edit by hand.

A lane that spends money on a model touched two closed lists it had no
business owning: the cockpit model purposes, which are what a WorkOrder is
identified by, and the tuple of installed model configuration file names that
a day-cap raise repoints. Both are now registries with the old literals as the
seed, so a lane names its own purpose and its own configuration from its own
module.

The seeds are pinned here as expected values: making them extensible is only
safe if dropping one is visible.
"""

from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

from dalton_core import cockpit_model
from dalton_core.cockpit_model import (
    CockpitModelError,
    build_work,
    purposes,
    register_purpose,
)
from dalton_core.model_configurations import (
    ModelConfigurationError,
    model_config_names,
    register_model_config_name,
)


ROOT = Path(__file__).resolve().parents[1]
SEED_PURPOSES = frozenset({"ask", "goal", "steer", "draft", "plan", "model_spec"})
SEED_MODEL_CONFIGS = (
    "document-extraction-model-config.json",
    "research-planner-model-config.json",
    "initial-screen-model-config.json",
)


def work(purpose: str):
    return build_work(
        purpose=purpose,
        request_id="request-1",
        prompt="how does this company make money",
        mission_version_ref="coverage-mission-version:us-it-services:1",
        max_input_tokens=8_000,
        max_output_tokens=1_000,
        max_cost_usd=0.5,
        max_seconds=60,
        created_at="2026-09-09T00:00:00.000000+00:00",
    )


class ModelPurposeRegistryTests(unittest.TestCase):
    def setUp(self) -> None:
        # Restored to what was found, not to the seed. Q1 registers "quality"
        # from research_quality_score at import, which is what the registry is
        # for; a teardown that reset to the seed would delete a live lane's
        # purpose for every test that ran afterwards, and this class only
        # passed because its own alphabetical order hid it.
        self._registered = set(cockpit_model._PURPOSES)

    def tearDown(self) -> None:
        cockpit_model._PURPOSES.clear()
        cockpit_model._PURPOSES.update(self._registered)

    def test_the_seed_is_what_the_literal_said(self) -> None:
        # The seed is pinned; the live registry is the seed plus whatever the
        # lanes have registered, so it is a superset and not an equality.
        self.assertEqual(frozenset(cockpit_model._SEED_PURPOSES), SEED_PURPOSES)
        self.assertLessEqual(SEED_PURPOSES, purposes())

    def test_there_is_no_module_constant_to_go_stale(self) -> None:
        # A rebound frozenset is a snapshot: whoever imported it before a lane
        # registered keeps the old vocabulary and refuses a legal purpose.
        self.assertFalse(hasattr(cockpit_model, "PURPOSES"))

    def test_an_unregistered_purpose_is_refused(self) -> None:
        with self.assertRaises(CockpitModelError):
            work("valuation")

    def test_a_registered_purpose_may_build_work(self) -> None:
        register_purpose("valuation")
        self.assertIn("valuation", purposes())
        order = work("valuation")
        self.assertIn("valuation", order.id)

    def test_registering_twice_is_a_no_op(self) -> None:
        register_purpose("valuation")
        before = purposes()
        register_purpose("valuation")
        self.assertEqual(purposes(), before)

    def test_a_purpose_that_could_not_be_a_work_order_id_is_refused(self) -> None:
        for bad in ("Valuation", "market price", "", "market/price", 7, "_x"):
            with self.assertRaises(CockpitModelError):
                register_purpose(bad)

    def test_the_seed_purposes_still_build_work(self) -> None:
        for purpose in sorted(SEED_PURPOSES):
            self.assertIsNotNone(work(purpose))


class ModelConfigurationRegistryTests(unittest.TestCase):
    def setUp(self) -> None:
        from dalton_core import model_configurations

        # Snapshotted here, not reset to the seed in teardown. A lane module
        # registers its own configuration name at import -- claim_index_tagging
        # does -- and resetting to the seed deleted it for every test that ran
        # afterwards. This class only passed because the one test that asserts
        # the seed sorts last inside it, so an earlier test's teardown had
        # already put the registry back. That is a coincidence of alphabetical
        # order, not a fixture. Same lesson the purpose registry above learned.
        self._names = list(model_configurations._NAMES)

    def tearDown(self) -> None:
        from dalton_core import model_configurations

        model_configurations._NAMES[:] = self._names

    def test_the_seed_is_what_the_script_tuple_said(self) -> None:
        # The three this Core installs are still there, still first, still in
        # order. Whatever follows them is a lane that registered its own, which
        # is the registry working rather than drift.
        self.assertEqual(
            model_config_names()[:len(SEED_MODEL_CONFIGS)], SEED_MODEL_CONFIGS)

    def test_a_registered_name_is_appended_once_in_order(self) -> None:
        before = model_config_names()
        register_model_config_name("market-model-config.json")
        register_model_config_name("market-model-config.json")
        self.assertEqual(
            model_config_names(), before + ("market-model-config.json",))

    def test_a_name_that_is_not_a_state_directory_file_is_refused(self) -> None:
        for bad in ("../escape.json", "Market.json", "market-model-config",
                    "/abs/path.json", "", 7):
            with self.assertRaises(ModelConfigurationError):
                register_model_config_name(bad)

    def test_the_cap_raise_script_repoints_every_registered_name(self) -> None:
        # The script is the only consumer, and the bug the registry prevents is
        # a configuration it never visits.
        spec = importlib.util.spec_from_file_location(
            "raise_day_budget_cap", ROOT / "scripts" / "raise_day_budget_cap.py"
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.assertIs(module.model_config_names, model_config_names)
        source = (ROOT / "scripts" / "raise_day_budget_cap.py").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("MODEL_CONFIG_NAMES", source)


if __name__ == "__main__":
    unittest.main()
