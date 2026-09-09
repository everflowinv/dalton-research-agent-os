"""P13aj: the ticket machinery, once, with the restart case it exists for.

Four launchers each carried their own copy of this. The part worth sharing is
not the spawning -- it is what ``status`` says when the writer restarted under a
running child. A ticket that still reads ``running`` with no live process is
**orphaned**: not failed, not succeeded, and never read as succeeded from a
summary file lying beside it, because a summary can be written by a run that
then died.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from dalton_core.lane_child_launcher import (
    LaneChildConflict,
    LaneChildError,
    LaneChildLauncher,
    LaneChildRejected,
    LaneChildTicketNotFound,
)


class _Sleeper(LaneChildLauncher):
    TICKET_PREFIX = "probe-run"
    TICKETS_DIRNAME = "probe-runs"

    def __init__(self, *, script: str, **kwargs):
        super().__init__(**kwargs)
        self.script = script

    def _command(self, *, ticket_dir: Path) -> list[str]:
        return [self.python_executable, "-c", self.script]


class LauncherTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.state = Path(self._dir.name) / "state"
        self.state.mkdir()

    def launcher(self, script="import sys; sys.exit(0)"):
        made = _Sleeper(state_dir=self.state, script=script)
        self.addCleanup(made.close)
        return made

    def test_a_missing_state_directory_is_refused(self):
        with self.assertRaises(LaneChildError):
            _Sleeper(state_dir=self.state / "nowhere", script="pass")

    def test_a_child_settles_as_succeeded_or_failed_by_its_exit_code(self):
        for script, expected in (("import sys; sys.exit(0)", "succeeded"),
                                 ("import sys; sys.exit(3)", "failed")):
            with self.subTest(expected=expected):
                launcher = self.launcher(script)
                ticket = launcher.spawn(digest="a" * 24, record={"probe": True})
                launcher.wait(timeout=30)
                self.assertEqual(launcher.status(ticket["id"])["status"], expected)

    def test_a_ticket_left_running_by_a_restart_is_orphaned_not_guessed(self):
        launcher = self.launcher()
        ticket = launcher.spawn(digest="b" * 24, record={})
        launcher.wait(timeout=30)
        # A new launcher is a restarted writer: it holds no process handle.
        fresh = self.launcher()
        path = fresh._ticket_path(ticket["id"])
        record = json.loads(path.read_text(encoding="utf-8"))
        record.update({"status": "running", "pid": 999_999, "exit_code": None})
        path.write_text(json.dumps(record), encoding="utf-8")
        # A summary sitting beside it must not be read as success.
        path.with_name("summary.json").write_text(
            json.dumps({"status": "succeeded"}), encoding="utf-8")
        settled = fresh.status(ticket["id"])
        self.assertEqual(settled["status"], "orphaned")

    def test_a_settled_ticket_carries_its_summary(self):
        launcher = self.launcher()
        ticket = launcher.spawn(digest="c" * 24, record={})
        launcher.wait(timeout=30)
        path = launcher._ticket_path(ticket["id"])
        path.with_name("summary.json").write_text(
            json.dumps({"status": "succeeded", "lines": 495}), encoding="utf-8")
        self.assertEqual(launcher.status(ticket["id"])["summary"]["lines"], 495)

    def test_a_torn_summary_is_not_a_crash(self):
        launcher = self.launcher()
        ticket = launcher.spawn(digest="d" * 24, record={})
        launcher.wait(timeout=30)
        path = launcher._ticket_path(ticket["id"])
        path.with_name("summary.json").write_text("{not json", encoding="utf-8")
        self.assertIsNone(launcher.status(ticket["id"])["summary"])

    def test_only_one_child_runs_at_a_time(self):
        launcher = self.launcher("import time; time.sleep(30)")
        launcher.spawn(digest="e" * 24, record={})
        with self.assertRaises(LaneChildConflict):
            launcher.spawn(digest="f" * 24, record={})

    def test_a_ticket_ref_from_another_lane_is_refused(self):
        launcher = self.launcher()
        with self.assertRaises(LaneChildRejected):
            launcher.status("sec-lane-run:" + "a" * 24)

    def test_an_unknown_ticket_is_not_found(self):
        with self.assertRaises(LaneChildTicketNotFound):
            self.launcher().status("probe-run:" + "9" * 24)

    def test_a_digest_that_is_not_a_digest_is_refused(self):
        with self.assertRaises(LaneChildRejected):
            self.launcher().spawn(digest="../escape", record={})

    def test_tickets_and_their_directory_are_owner_only(self):
        launcher = self.launcher()
        ticket = launcher.spawn(digest="1" * 24, record={})
        launcher.wait(timeout=30)
        path = launcher._ticket_path(ticket["id"])
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(launcher.tickets_dir.stat().st_mode & 0o777, 0o700)

    def test_the_child_log_is_captured_owner_only(self):
        launcher = self.launcher("print('hello from the child')")
        ticket = launcher.spawn(digest="2" * 24, record={})
        launcher.wait(timeout=30)
        log = launcher._ticket_path(ticket["id"]).with_name("run.log")
        self.assertIn("hello from the child", log.read_text(encoding="utf-8"))
        self.assertEqual(log.stat().st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
