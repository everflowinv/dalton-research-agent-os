"""P13k: the planner gets its own routing policy, and nobody gets it by accident."""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dalton_core.model_deployment import openclaw_broker_profiles
from dalton_core.model_router import ModelRouter
from dalton_core.research_planner_setup import (
    POLICY_ID,
    PlannerSetupError,
    credential_slots_for,
    ensure_planner_policy,
    install,
)

NOW = datetime(2026, 9, 8, tzinfo=timezone.utc)
ASTRA = "profile:gpt-6-astra"


class PolicyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.router = ModelRouter(str(Path(self.temp.name) / "router.sqlite"))
        self.addCleanup(self.router.close)
        for profile in openclaw_broker_profiles(checked_at=NOW):
            self.router.register_profile(profile)

    def test_the_planner_policy_is_not_the_extraction_policy(self):
        from dalton_core.document_extraction_setup import POLICY_ID as EXTRACTION

        self.assertNotEqual(POLICY_ID, EXTRACTION)

    def test_a_policy_pins_exactly_what_it_was_given(self):
        policy = ensure_planner_policy(self.router, profile_ids=[ASTRA], now=NOW)
        wire = self.router.get_policy(policy["policy_version_ref"])
        self.assertEqual(wire["filters"]["allowed_profile_ids"], [ASTRA])

    def test_installing_the_same_policy_twice_appends_once(self):
        first = ensure_planner_policy(self.router, profile_ids=[ASTRA], now=NOW)
        again = ensure_planner_policy(self.router, profile_ids=[ASTRA], now=NOW)
        self.assertEqual(first["status"], "fresh")
        self.assertEqual(again["status"], "duplicate")
        self.assertEqual(first["policy_version_ref"], again["policy_version_ref"])

    def test_changing_the_pinned_profile_appends_a_new_version(self):
        first = ensure_planner_policy(self.router, profile_ids=[ASTRA], now=NOW)
        second = ensure_planner_policy(
            self.router, profile_ids=["profile:gpt-5-6-terra"], now=NOW)
        self.assertNotEqual(first["policy_version_ref"], second["policy_version_ref"])
        self.assertEqual(
            self.router.get_policy(second["policy_version_ref"])["prior_version_ref"],
            first["policy_version_ref"])

    def test_a_policy_pinning_nothing_is_refused(self):
        with self.assertRaises(PlannerSetupError):
            ensure_planner_policy(self.router, profile_ids=[], now=NOW)

    def test_the_credential_slot_is_read_from_the_profile_not_guessed(self):
        # A wrong slot is a call that fails at the broker with no useful reason.
        self.assertEqual(credential_slots_for(self.router, [ASTRA]),
                         ["credential-slot:openclaw:openai"])

    def test_pinning_a_profile_nobody_registered_is_refused(self):
        with self.assertRaises(PlannerSetupError) as caught:
            credential_slots_for(self.router, ["profile:not-installed"])
        self.assertIn("not registered", str(caught.exception))


class AstraProfileTests(unittest.TestCase):
    """The planner's model has to be usable for the capability it asks for."""

    def profile(self):
        return next(p for p in openclaw_broker_profiles(checked_at=NOW)
                    if p["id"] == ASTRA)

    def test_astra_is_trusted_for_research_not_only_verification(self):
        # Derived from the provider catalog alone it comes out verify-only, and
        # the planner asks for "research": it would never be selected.
        from dalton_core.research_planner import build_work

        self.assertIn("research", self.profile()["capabilities"])
        state = {"content_hash": "0" * 64, "companies": [], "goal": {}}
        self.assertIn("research", build_work(
            state, created_at=NOW.isoformat(), state_ref="research-state:1"
        ).requested_capabilities)

    def test_its_context_fits_the_planner_budget(self):
        from dalton_core.research_planner import build_work

        state = {"content_hash": "0" * 64, "companies": [], "goal": {}}
        work = build_work(state, created_at=NOW.isoformat(), state_ref="research-state:1")
        limits = self.profile()["limits"]
        self.assertGreater(limits["max_input_tokens"], work.budget["max_input_tokens"])
        self.assertGreaterEqual(limits["max_output_tokens"], work.budget["max_output_tokens"])

    def test_it_is_priced_as_the_expensive_model_it_is(self):
        # The reason nothing routes here by default.
        from dalton_core.model_deployment import openclaw_broker_profiles as profiles

        cheap = next(p for p in profiles(checked_at=NOW)
                     if p["id"] == "profile:deepseek-v4-flash")
        self.assertGreater(self.profile()["cost"]["input_per_million_usd"],
                           cheap["cost"]["input_per_million_usd"] * 10)


if __name__ == "__main__":
    unittest.main()


class CatalogTests(unittest.TestCase):
    """P13k: nothing in the deploy ever registered profiles, so the catalog drifted."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.router = ModelRouter(str(Path(self.temp.name) / "router.sqlite"))
        self.addCleanup(self.router.close)

    def test_an_empty_router_gets_the_whole_catalog(self):
        from dalton_core.model_deployment import _ENDPOINTS, ensure_broker_profiles

        result = ensure_broker_profiles(self.router, checked_at=NOW)
        self.assertEqual(len(result["added"]), len(_ENDPOINTS))
        self.assertIn(ASTRA, result["added"])

    def test_a_second_run_adds_nothing(self):
        from dalton_core.model_deployment import ensure_broker_profiles

        ensure_broker_profiles(self.router, checked_at=NOW)
        again = ensure_broker_profiles(self.router, checked_at=NOW + timedelta(days=1))
        self.assertEqual(again["added"], [])

    def test_re_registering_would_churn_versions_which_is_why_it_does_not(self):
        # A profile carries an availability timestamp, so registering it again
        # appends a version that says nothing: the live deepseek profile is at
        # version eleven for exactly that reason.
        from dalton_core.model_deployment import ensure_broker_profiles

        ensure_broker_profiles(self.router, checked_at=NOW)
        ensure_broker_profiles(self.router, checked_at=NOW + timedelta(days=1))
        versions = self.router.connection.execute(
            "SELECT COUNT(*) FROM model_endpoint_profile_versions WHERE profile_id=?",
            (ASTRA,),
        ).fetchone()[0]
        self.assertEqual(versions, 1)

    def test_only_the_missing_one_is_added(self):
        from dalton_core.model_deployment import ensure_broker_profiles, openclaw_broker_profiles

        for profile in openclaw_broker_profiles(checked_at=NOW):
            if profile["id"] != ASTRA:
                self.router.register_profile(profile)
        result = ensure_broker_profiles(self.router, checked_at=NOW)
        self.assertEqual(result["added"], [ASTRA])

    def test_pinning_now_succeeds_because_the_profile_exists(self):
        from dalton_core.model_deployment import ensure_broker_profiles

        ensure_broker_profiles(self.router, checked_at=NOW)
        self.assertEqual(credential_slots_for(self.router, [ASTRA]),
                         ["credential-slot:openclaw:openai"])
