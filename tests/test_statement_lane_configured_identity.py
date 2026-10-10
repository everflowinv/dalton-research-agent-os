from __future__ import annotations

import argparse
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from dalton_core.mission_statement_lane import add_arguments, build_launcher


class ConfiguredIdentityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        parser = argparse.ArgumentParser()
        add_arguments(parser)
        self.args = parser.parse_args([
            "--statement-lane-governance", str(Path(self.temp.name) / "approval.json")])
        self.args.db = str(Path(self.temp.name) / "core.sqlite")

    def command(self):
        launcher = build_launcher(self.args)
        return launcher, launcher._command(
            ticket_dir=Path(self.temp.name) / "ticket", ticker="SYNTH",
            form="10-K", limit=1)

    def test_existing_sec_identity_reaches_actual_child_command(self):
        self.args.sec_lane_user_agent = "Synthetic configured SEC operator sec@example.invalid"
        launcher, command = self.command()
        self.assertEqual(launcher.user_agent, self.args.sec_lane_user_agent)
        self.assertEqual(command[command.index("--user-agent") + 1], self.args.sec_lane_user_agent)
        self.assertIn("--allow-network", command)

    def test_resolver_config_is_used_when_staging_lane_is_not_enabled(self):
        self.args.sec_company_resolver_identity = "Synthetic SEC identity configured@example.invalid"
        launcher, command = self.command()
        self.assertEqual(command[command.index("--user-agent") + 1], self.args.sec_company_resolver_identity)

    def test_explicit_statement_override_still_wins(self):
        self.args.statement_lane_user_agent = "Explicit SEC identity explicit@example.invalid"
        self.args.sec_lane_user_agent = "Other configured identity other@example.invalid"
        self.args.sec_company_resolver_identity = "Resolver identity resolver@example.invalid"
        launcher, command = self.command()
        self.assertEqual(command[command.index("--user-agent") + 1], self.args.statement_lane_user_agent)

    def test_missing_or_placeholder_identity_refuses_network_before_spawn(self):
        for value in [None, "", "Dalton owner", "Dalton operator@localhost"]:
            with self.subTest(value=value):
                self.args.sec_company_resolver_identity = value
                with self.assertRaisesRegex(ValueError, "configured User-Agent"):
                    build_launcher(self.args)

    def test_fixture_replay_does_not_invent_contact_or_enable_network(self):
        self.args.statement_lane_fixture = str(Path(self.temp.name) / "fixture.json")
        launcher, command = self.command()
        self.assertIsNone(launcher.user_agent)
        self.assertNotIn("--user-agent", command)
        self.assertNotIn("--allow-network", command)
        self.assertIn("--fixture-file", command)

    def test_disabled_governance_does_not_require_or_invent_identity(self):
        self.args.statement_lane_governance = None
        self.assertIsNone(build_launcher(self.args))
