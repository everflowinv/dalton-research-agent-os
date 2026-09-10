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
import time
import unittest
from pathlib import Path

from dalton_core.lane_child_launcher import (
    LaneChildConflict,
    LaneChildError,
    LaneChildLauncher,
    LaneChildRejected,
    LaneChildTicketNotFound,
    write_owner_only,
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

    def test_restart_adopts_same_digest_without_relaunch_or_log_truncation(self):
        launcher = self.launcher(
            "import time; print('original', flush=True); time.sleep(30)")
        ticket = launcher.spawn(digest="6" * 24, record={"generation": 1})
        log = launcher._ticket_path(ticket["id"]).with_name("run.log")
        for _ in range(100):
            if log.is_file() and "original" in log.read_text(encoding="utf-8"):
                break
            time.sleep(0.01)
        fresh = self.launcher("raise SystemExit('must not launch')")
        adopted = fresh.spawn(digest="6" * 24, record={"generation": 2})
        self.assertEqual(adopted["pid"], ticket["pid"])
        self.assertEqual(adopted["generation"], 1)
        self.assertIn("original", log.read_text(encoding="utf-8"))

    def test_restart_refuses_a_different_digest_while_child_is_alive(self):
        launcher = self.launcher("import time; time.sleep(30)")
        launcher.spawn(digest="7" * 24, record={})
        fresh = self.launcher()
        with self.assertRaises(LaneChildConflict):
            fresh.spawn(digest="8" * 24, record={})

    def test_adopted_ticket_does_not_follow_a_reused_pid_forever(self):
        launcher = self.launcher("import time; time.sleep(30)")
        ticket = launcher.spawn(digest="b" * 24, record={})
        fresh = self.launcher("import time; time.sleep(30)")
        fresh.spawn(digest="b" * 24, record={})
        path = fresh._ticket_path(ticket["id"])
        record = json.loads(path.read_text(encoding="utf-8"))
        record["pid"] = os.getpid()
        record["command"] = ["definitely-not-this-test-process"]
        path.write_text(json.dumps(record), encoding="utf-8")
        self.assertEqual(fresh.status(ticket["id"])["status"], "orphaned")

    def test_legacy_running_ticket_keeps_its_pid_liveness_contract(self):
        launcher = self.launcher()
        ticket_id = "probe-run:" + "c" * 24
        path = launcher._ticket_path(ticket_id)
        write_owner_only(path, {
            "schema_version": "0.1", "id": ticket_id, "pid": os.getpid(),
            "status": "running", "exit_code": None, "completed_at": None,
        })
        self.assertEqual(launcher.status(ticket_id)["status"], "running")

    def test_dead_ticket_and_reused_pid_do_not_freeze_future_work(self):
        launcher = self.launcher()
        old = launcher.spawn(digest="9" * 24, record={})
        launcher.wait(timeout=30)
        path = launcher._ticket_path(old["id"])
        record = json.loads(path.read_text(encoding="utf-8"))
        # A live but unrelated PID exercises the PID-reuse boundary.
        record["pid"] = os.getpid()
        record["command"] = ["definitely-not-this-test-process"]
        path.write_text(json.dumps(record), encoding="utf-8")
        fresh = self.launcher()
        started = fresh.spawn(digest="a" * 24, record={})
        self.assertEqual(started["status"], "running")
        self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["status"],
                         "orphaned")

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


class OwnerOnlyWriteTests(unittest.TestCase):
    """INT1 / S3: a child writing its refusal must not die writing it."""

    def test_the_parent_directory_is_made_rather_than_assumed(self):
        # A child that refuses before it has done anything else writes its
        # summary through this function, into a directory the run never got
        # far enough to create. S3 found those children dying on
        # FileNotFoundError, which loses the sentence explaining the refusal
        # and leaves a lane that looks crashed rather than refused.
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "runs" / "never-made" / "summary.json"
            write_owner_only(target, {"status": "refused", "reason": "no grant"})
            self.assertEqual(
                json.loads(target.read_text(encoding="utf-8"))["reason"], "no grant")
            self.assertEqual(target.stat().st_mode & 0o777, 0o600)
            # Owner-only the whole way down: the directory listing names the
            # company and the run, so it is 0700 like every other directory
            # this launcher makes rather than whatever the umask allows.
            self.assertEqual(target.parent.stat().st_mode & 0o777, 0o700)

    def test_writing_twice_still_replaces_in_place(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "deep" / "ticket.json"
            write_owner_only(target, {"status": "running"})
            write_owner_only(target, {"status": "failed"})
            self.assertEqual(
                json.loads(target.read_text(encoding="utf-8"))["status"], "failed")
            self.assertEqual(sorted(p.name for p in target.parent.iterdir()),
                             ["ticket.json"])


if __name__ == "__main__":
    unittest.main()
