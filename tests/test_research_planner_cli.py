"""P13n: the child that decides, and what it does when it cannot."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from dalton_core.coverage_mission import CoverageMissionAuthority
from dalton_core.research_planner_cli import MAX_COST_USD, run_planner
from dalton_core.store import DaltonStore
from tests.p9a_fixtures import bootstrap_method_authorities, mission_params


class PlannerChildTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.state = self.root / "state"
        self.state.mkdir(mode=0o700)
        self.summary_dir = self.root / "out"
        self.store = DaltonStore(str(self.state / "core.sqlite"))
        self.missions = CoverageMissionAuthority(self.store)

    def close(self):
        self.store.close()

    def publish_mission(self):
        fixtures = bootstrap_method_authorities(self.store)
        params = mission_params(fixtures)
        ref = params.pop("mission_ref")
        mission = self.missions.create_mission(ref, **params)
        self.store.connection.execute(
            "SELECT mission_version_id FROM coverage_mission_pointer").fetchone()
        return mission

    def plan(self, **overrides):
        kwargs = {
            "state_dir": self.state, "model_config_path": None,
            "summary_dir": self.summary_dir, "scheduler_db": None,
            "plans_dir": self.state / "discovery-plans", "dry_run": True,
        }
        kwargs.update(overrides)
        self.close()
        try:
            return run_planner(**kwargs)
        finally:
            self.store = DaltonStore(str(self.state / "core.sqlite"))
            self.missions = CoverageMissionAuthority(self.store)

    def test_a_core_with_no_mission_is_idle_not_an_error(self):
        summary = self.plan()
        self.assertEqual(summary["status"], "idle")
        self.assertEqual(summary["plan_status"], "no_mission")

    def test_a_dry_run_assembles_the_state_and_spends_nothing(self):
        self.publish_mission()
        summary = self.plan()
        self.assertEqual(summary["status"], "succeeded")
        self.assertEqual(summary["plan_status"], "gated")
        self.assertIsNotNone(summary["state_hash"])
        self.assertEqual(summary["cost_micros"], 0)
        self.assertGreater(summary["prompt_bytes"], 0)

    def test_no_model_configured_is_gated_and_says_so(self):
        self.publish_mission()
        summary = self.plan(dry_run=False, model_config_path=None)
        self.assertEqual(summary["plan_status"], "gated")
        self.assertIn("no planner model", summary["failure_reason"])

    def test_an_unchanged_state_replays_the_plan_instead_of_paying(self):
        # The whole reason the state hashes: an expensive model on a
        # five-minute tick is only affordable if an unchanged world is free.
        mission = self.publish_mission()
        first = self.plan()
        self.missions.record_research_plan(
            {"mission_version_ref": mission["id"], "state_hash": first["state_hash"],
             "assessment": "steady", "directives": [], "inquiries": [],
             "content_hash": "a" * 64},
            decided_by="automation:x")
        # The model config is never read: an unchanged state short-circuits
        # before anything is opened, which is the property under test.
        config = self.root / "never-read.json"
        config.write_text("{}", encoding="utf-8")
        again = self.plan(dry_run=False, model_config_path=config)
        self.assertEqual(again["plan_status"], "unchanged")
        self.assertTrue(again["replayed"])
        self.assertEqual(again["cost_micros"], 0)

    def test_the_summary_is_written_owner_only_even_on_a_dry_run(self):
        self.publish_mission()
        self.plan()
        path = self.summary_dir / "summary.json"
        self.assertTrue(path.exists())
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(json.loads(path.read_text())["formal_authority_writes"], 0)

    def test_it_never_claims_authority_writes(self):
        self.publish_mission()
        self.assertEqual(self.plan()["formal_authority_writes"], 0)

    def test_the_reservation_is_sized_to_the_call(self):
        # $1.50 reserved for an $0.08 call pushed the day past the mission's
        # $5 cap and the call was refused before it was made.
        self.assertLess(MAX_COST_USD, 0.5)
        self.assertGreater(MAX_COST_USD, 0.33)


if __name__ == "__main__":
    unittest.main()
