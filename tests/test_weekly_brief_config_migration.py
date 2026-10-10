import json
import tempfile
import unittest
from pathlib import Path

from dalton_core.bootstrap import bootstrap
from dalton_core.store import content_hash
from dalton_core.weekly_brief_config_migration import migrate
from dalton_core.weekly_brief_workspace_plan import (
    WorkspaceWeeklyBriefConflict, build_workspace_weekly_brief_plan, read_plan,
)


class WeeklyConfigMigrationTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.state = Path(temp.name) / "state"
        self.config = Path(temp.name) / "service.json"
        bootstrap(self.state, self.config)
        self.plan = build_workspace_weekly_brief_plan(
            {"universe": [{"ticker": "SYN"}]}, brief_slug="synthetic", version=4,
            weekday=3, hour=7, minute=0, timezone="America/New_York",
            destination_ref="cockpit:weekly-brief:synthetic",
            effective_from="2026-10-01T00:00:00+00:00")

    def inline(self, enabled=True):
        config = json.loads(self.config.read_text())
        config["weekly_brief"] = {"enabled": enabled, "interval_seconds": 300,
            "config": {"writer_socket": str(self.state / "run/writer.sock"),
                       "token_config": str(self.state / "writer-tokens.json"),
                       "plan": self.plan}}
        self.config.write_text(json.dumps(config))

    def test_inline_migration_preserves_hash_cas_and_schedule_without_read_side_writes(self):
        self.inline()
        original = self.config.read_bytes()
        preview = migrate(self.state, self.config)
        self.assertEqual(self.config.read_bytes(), original)
        self.assertIsNone(read_plan(self.state))
        result = migrate(self.state, self.config, apply=True)
        self.assertEqual(result["plan_hash"], content_hash(self.plan))
        self.assertEqual(preview["after_config_hash"], result["after_config_hash"])
        self.assertEqual(read_plan(self.state), self.plan)
        self.assertFalse(migrate(self.state, self.config, apply=True)["changed"])

    def test_empty_disabled_lane_becomes_reader_without_creating_schedule(self):
        result = migrate(self.state, self.config, apply=True)
        self.assertFalse(result["scheduled"])
        self.assertIsNone(read_plan(self.state))
        self.assertTrue(json.loads(self.config.read_text())["weekly_brief"]["enabled"])

    def test_conflicting_plan_and_suspended_schedule_are_not_overwritten(self):
        self.inline(False)
        original = self.config.read_bytes()
        with self.assertRaisesRegex(ValueError, "explicit resumption"):
            migrate(self.state, self.config, apply=True)
        self.assertEqual(self.config.read_bytes(), original)
        self.inline()
        other = {**self.plan, "hour": 9}
        (self.state / "weekly-brief-plan.json").write_text(json.dumps(other))
        original = self.config.read_bytes()
        with self.assertRaises(WorkspaceWeeklyBriefConflict):
            migrate(self.state, self.config, apply=True)
        self.assertEqual(self.config.read_bytes(), original)
        self.assertEqual(read_plan(self.state), other)
