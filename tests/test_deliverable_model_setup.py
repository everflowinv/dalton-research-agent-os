"""P13ad: the deliverable may be written by a model chosen for writing it.

The Initial Screen was drafted by whatever the *extraction* lane used, because
the launcher was handed the extraction config and nobody ever chose otherwise.
That model is picked to pull a figure out of one window of a filing, cheaply,
thousands of times. The sections that carry the argument are exactly where that
shows: live, the anti-thesis section came back ``dropped_unsourced`` and the
relevance section published with no figures at all.

Nobody gets the expensive model by accident, and nobody loses the screen by
leaving the knob alone.
"""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from dalton_core.deliverable_model_setup import CONFIG_FILE_NAME, POLICY_ID
from dalton_core.model_deployment import openclaw_broker_profiles
from dalton_core.model_router import ModelRouter
from dalton_core.research_planner_setup import ensure_planner_policy

NOW = datetime(2026, 9, 9, tzinfo=timezone.utc)
ASTRA = "profile:gpt-6-astra"
FLASH = "profile:deepseek-v4-flash"


class PolicyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.router = ModelRouter(str(Path(self.temp.name) / "router.sqlite"))
        self.addCleanup(self.router.close)
        for profile in openclaw_broker_profiles(checked_at=NOW):
            self.router.register_profile(profile)

    def test_the_deliverable_policy_is_nobody_else_s(self):
        # Two jobs sharing one pinned policy can never differ, which is the
        # whole reason this exists.
        from dalton_core.document_extraction_setup import POLICY_ID as EXTRACTION
        from dalton_core.research_planner_setup import POLICY_ID as PLANNER

        self.assertNotEqual(POLICY_ID, EXTRACTION)
        self.assertNotEqual(POLICY_ID, PLANNER)
        self.assertNotEqual(CONFIG_FILE_NAME, "document-extraction-model-config.json")
        self.assertNotEqual(CONFIG_FILE_NAME, "research-planner-model-config.json")

    def test_pinning_the_deliverable_leaves_the_planner_policy_alone(self):
        from dalton_core.research_planner_setup import POLICY_ID as PLANNER

        ensure_planner_policy(self.router, profile_ids=[ASTRA], now=NOW,
                              policy_id=PLANNER)
        ensure_planner_policy(self.router, profile_ids=[FLASH], now=NOW,
                              policy_id=POLICY_ID)
        import json

        def pinned(policy_id):
            row = self.router.connection.execute(
                "SELECT policy_json FROM model_routing_policy_versions WHERE policy_id=? "
                "ORDER BY version DESC LIMIT 1", (policy_id,)).fetchone()
            return json.loads(row["policy_json"])["filters"]["allowed_profile_ids"]

        self.assertEqual(pinned(PLANNER), [ASTRA])
        self.assertEqual(pinned(POLICY_ID), [FLASH])

    def test_appending_the_same_pin_twice_appends_once(self):
        first = ensure_planner_policy(self.router, profile_ids=[ASTRA], now=NOW,
                                      policy_id=POLICY_ID)
        again = ensure_planner_policy(self.router, profile_ids=[ASTRA], now=NOW,
                                      policy_id=POLICY_ID)
        self.assertEqual(again["status"], "duplicate")
        self.assertEqual(again["policy_version_ref"], first["policy_version_ref"])

    def test_the_policy_version_ref_is_named_after_its_own_policy(self):
        policy = ensure_planner_policy(self.router, profile_ids=[ASTRA], now=NOW,
                                       policy_id=POLICY_ID)
        self.assertIn("deliverable-drafting", policy["policy_version_ref"])


if __name__ == "__main__":
    unittest.main()
