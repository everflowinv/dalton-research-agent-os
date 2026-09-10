"""P13am: the specification launcher names a run by the judgement it is making.

A ticket keyed only by the company would collapse every quarter's decision
into one directory; keyed by the company *and* the disclosure, asking twice
about a company that has filed nothing new is the same ticket, and a company
that has filed something new is a different one.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from dalton_core.company_model_launcher import CompanyModelSpecLauncher
from dalton_core.lane_child_launcher import LaneChildRejected

ACN = "company:sec-cik:0001467373"
HASH = "a" * 64


class CompanyModelSpecLauncherTests(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.state = Path(self._dir.name)

    def launcher(self, **kwargs):
        made = CompanyModelSpecLauncher(state_dir=self.state, **kwargs)
        self.addCleanup(made.close)
        return made

    def test_the_command_carries_the_company_and_the_model(self):
        config = self.state / "model.json"
        config.write_text("{}", encoding="utf-8")
        launcher = self.launcher(model_config_path=config,
                                 scheduler_db=self.state / "scheduler.sqlite")
        command = launcher._command(
            ticket_dir=self.state, company_ref=ACN,
            expected_state_hash=HASH, expected_task_hash="b" * 64)
        self.assertIn("dalton_core.company_model_cli", command)
        self.assertIn(ACN, command)
        self.assertIn("--model-config", command)
        self.assertIn("--scheduler-db", command)
        self.assertEqual(command[command.index("--expected-state-hash") + 1], HASH)
        self.assertEqual(command[command.index("--expected-task-hash") + 1], "b" * 64)
        self.assertTrue(launcher.configured)

    def test_without_a_model_the_lane_says_so_rather_than_pretending(self):
        launcher = self.launcher()
        self.assertFalse(launcher.configured)
        self.assertNotIn("--model-config",
                         launcher._command(
                             ticket_dir=self.state, company_ref=ACN,
                             expected_state_hash=HASH, expected_task_hash="b" * 64))

    def test_the_same_company_and_disclosure_is_the_same_ticket(self):
        launcher = self.launcher()
        first = launcher.start(company_ref=ACN, state_hash=HASH)
        launcher.wait(timeout=30)
        again = launcher.start(company_ref=ACN, state_hash=HASH)
        launcher.wait(timeout=30)
        self.assertEqual(first["id"], again["id"])
        moved = launcher.start(company_ref=ACN, state_hash="b" * 64)
        launcher.wait(timeout=30)
        self.assertNotEqual(moved["id"], first["id"])

    def test_the_ticket_records_what_the_run_was_about(self):
        launcher = self.launcher()
        ticket = launcher.start(company_ref=ACN, state_hash=HASH)
        launcher.wait(timeout=30)
        self.assertEqual(ticket["company_ref"], ACN)
        self.assertEqual(ticket["state_hash"], HASH)
        self.assertIs(ticket["model_configured"], False)
        self.assertEqual(launcher.status(ticket["id"])["state_hash"], HASH)

    def test_a_request_that_names_nothing_is_refused_before_spawning(self):
        launcher = self.launcher()
        for kwargs in ({"company_ref": "", "state_hash": HASH},
                       {"company_ref": ACN, "state_hash": "short"},
                       {"company_ref": ACN, "state_hash": None}):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(LaneChildRejected):
                    launcher.start(**kwargs)


if __name__ == "__main__":
    unittest.main()
