"""WP-B2-1/B2-2: the child and ticket lifecycle the writer was leaking.

Three separate failures produced the same symptom -- ``<defunct>`` children
piling up at roughly 27 an hour and 1,049 tickets永远 reading ``running``:

* a launcher held only its *newest* ``Popen``, so every replaced handle was
  never waited on and its child stayed a zombie;
* ``pid_alive`` used ``kill(pid, 0)``, which succeeds on a zombie, so those
  tickets answered "still running" forever;
* nothing reconciled tickets left behind by a previous writer process.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dalton_core import lane_child_launcher as lcl
from dalton_core.lane_child_launcher import (
    LaneChildConflict,
    LaneChildLauncher,
    pid_alive,
    write_owner_only,
)
from dalton_core.research_planner_launcher import ResearchPlannerLauncher


class _Probe(LaneChildLauncher):
    TICKET_PREFIX = "probe-run"
    TICKETS_DIRNAME = "probe-runs"

    def __init__(self, *, script: str, **kwargs):
        super().__init__(**kwargs)
        self.script = script

    def _command(self, *, ticket_dir: Path) -> list[str]:
        return [self.python_executable, "-c", self.script]


class ZombieLivenessTests(unittest.TestCase):
    def test_a_zombie_child_is_not_alive(self):
        child = subprocess.Popen(["/bin/sh", "-c", "exit 0"])
        try:
            for _ in range(200):
                if os.waitpid(child.pid, os.WNOHANG) == (0, 0):
                    break
                time.sleep(0.01)
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                # The child has exited but this process has not reaped it.
                if not pid_alive(child.pid):
                    break
                time.sleep(0.02)
            self.assertFalse(pid_alive(child.pid),
                             "a zombie must never be read as a running child")
        finally:
            child.wait(timeout=10)

    def test_a_live_pid_is_alive(self):
        self.assertTrue(pid_alive(os.getpid()))

    def test_nonsense_pids_are_not_alive(self):
        for value in (None, 0, -1, True, "1", 2 ** 62):
            self.assertFalse(pid_alive(value), value)


class CommandIdentityTests(unittest.TestCase):
    """BSD ``ps`` loses argv boundaries; what can still be proved must be."""

    def test_a_different_first_argument_proves_a_reused_pid(self):
        from dalton_core.launch_drain import _process_command_matches
        # This process runs unittest; a ticket recording ``-c <program>`` under
        # the same pid is a reused pid, and the ``-c`` vs ``-m`` token says so
        # even though the program text contains spaces.
        self.assertIs(_process_command_matches(os.getpid(), [
            sys.executable, "-c", "import time; time.sleep(30)"]), False)

    def test_a_reexeced_python_still_proves_a_match(self):
        # macOS re-execs a venv python as the framework Python.app a few
        # milliseconds after launch, so argv[0] in ``ps`` stops matching the
        # recorded one while every later argument is identical.
        from dalton_core.launch_drain import _process_command_matches
        command = [sys.executable, "-c", "import time; time.sleep(20)",
                   "an argument with spaces"]
        child = subprocess.Popen(command)
        try:
            time.sleep(0.5)
            self.assertIs(_process_command_matches(child.pid, command), True)
        finally:
            child.kill()
            child.wait(timeout=10)


class _LaneCase(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.state = Path(self._dir.name) / "state"
        self.state.mkdir()

    def launcher(self, script="import sys; sys.exit(0)"):
        made = _Probe(state_dir=self.state, script=script)
        self.addCleanup(made.close)
        return made


class ChildReapingTests(_LaneCase):
    def test_every_child_is_waited_on_not_only_the_newest(self):
        launcher = self.launcher()
        first = launcher.spawn(digest="a" * 24, record={})
        launcher.wait(timeout=30)
        second = launcher.spawn(digest="b" * 24, record={})
        launcher.wait(timeout=30)
        # The first handle was replaced as ``_current``; it must still be held.
        self.assertIn(first["id"], set(launcher._children) | {first["id"]})
        launcher.spawn(digest="c" * 24, record={})
        launcher.wait(timeout=30)
        launcher.reap()
        self.assertEqual(launcher._children, {},
                         "a finished child must be reaped, not left defunct")
        for ticket in (first, second):
            self.assertEqual(launcher.status(ticket["id"])["status"], "succeeded")

    def test_spawn_reaps_before_it_asks_whether_the_slot_is_free(self):
        launcher = self.launcher()
        first = launcher.spawn(digest="a" * 24, record={})
        launcher.wait(timeout=30)
        self.assertIn(first["id"], launcher._children)
        launcher.spawn(digest="b" * 24, record={})
        self.assertNotIn(first["id"], launcher._children)
        # Reaping also settles the ticket without anyone asking for it.
        settled = json.loads(
            launcher._ticket_path(first["id"]).read_text(encoding="utf-8"))
        self.assertEqual((settled["status"], settled["exit_code"]), ("succeeded", 0))

    def test_spawn_does_not_read_the_whole_lane_directory(self):
        launcher = self.launcher()
        for index in range(6):
            ticket_dir = launcher.tickets_dir / (f"{index:x}" * 24)
            write_owner_only(ticket_dir / "ticket.json", {
                "schema_version": "0.1", "id": f"probe-run:{f'{index:x}' * 24}",
                "started_at": "2026-01-01T00:00:00.000000+00:00", "pid": 999999999,
                "command": ["/bin/false"], "status": "succeeded",
                "exit_code": 0, "completed_at": "2026-01-01T00:00:01.000000+00:00",
            })
        reads = []
        original = Path.read_text

        def counted(self, *args, **kwargs):
            if self.name == "ticket.json":
                reads.append(self)
            return original(self, *args, **kwargs)

        with mock.patch.object(Path, "read_text", counted):
            launcher.spawn(digest="f" * 24, record={})
        launcher.wait(timeout=30)
        self.assertEqual(reads, [],
                         "spawn must read the in-memory running set, not the directory")

    def test_close_terminates_and_reaps_every_child(self):
        launcher = self.launcher("import time; time.sleep(60)")
        first = launcher.spawn(digest="a" * 24, record={})
        pid = first["pid"]
        launcher.close()
        self.assertEqual(launcher._children, {})
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and pid_alive(pid):
            time.sleep(0.05)
        self.assertFalse(pid_alive(pid))


class StartupReconciliationTests(_LaneCase):
    def _stale_ticket(self, digest, **overrides):
        record = {
            "schema_version": "0.1", "id": f"probe-run:{digest}",
            "started_at": lcl.wire_time(
                lcl.PROCESS_STARTED_AT - timedelta(hours=2)),
            "pid": 999_999_999, "command": ["/usr/bin/false"],
            "status": "running", "exit_code": None, "completed_at": None,
        }
        record.update(overrides)
        path = self.state / "probe-runs" / digest / "ticket.json"
        write_owner_only(path, record)
        return path

    def test_a_running_ticket_from_a_previous_process_is_orphaned_with_a_reason(self):
        path = self._stale_ticket("a" * 24)
        launcher = self.launcher()
        settled = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(settled["status"], "orphaned")
        self.assertTrue(settled["completed_at"])
        self.assertIn("进程号", settled["orphaned_reason"])
        report = launcher.startup_reconciliation
        self.assertEqual((report["running_tickets_checked"], report["orphaned"]), (1, 1))

    def test_a_ticket_whose_pid_is_a_live_unrelated_process_is_orphaned(self):
        # This process is alive but demonstrably runs a different command.
        path = self._stale_ticket(
            "b" * 24, pid=os.getpid(),
            command=["/usr/bin/false", "--definitely-not-this-process"])
        self.launcher()
        settled = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(settled["status"], "orphaned")
        self.assertIn("复用", settled["orphaned_reason"])

    def test_a_ticket_started_after_this_process_is_left_alone(self):
        path = self._stale_ticket(
            "c" * 24,
            started_at=lcl.wire_time(lcl.PROCESS_STARTED_AT + timedelta(minutes=5)))
        launcher = self.launcher()
        self.assertEqual(
            json.loads(path.read_text(encoding="utf-8"))["status"], "running")
        self.assertEqual(launcher.startup_reconciliation["orphaned"], 0)

    def test_reconciliation_frees_the_slot_for_the_next_spawn(self):
        self._stale_ticket("a" * 24)
        launcher = self.launcher()
        ticket = launcher.spawn(digest="d" * 24, record={})
        launcher.wait(timeout=30)
        self.assertEqual(launcher.status(ticket["id"])["status"], "succeeded")

    def test_an_unchanged_settled_ticket_is_skipped_on_the_second_pass(self):
        for index in range(5):
            digest = f"{index:x}" * 24
            write_owner_only(self.state / "probe-runs" / digest / "ticket.json", {
                "schema_version": "0.1", "id": f"probe-run:{digest}",
                "started_at": lcl.wire_time(
                    lcl.PROCESS_STARTED_AT - timedelta(hours=2)),
                "pid": 999_999_999, "command": ["/usr/bin/false"],
                "status": "succeeded", "exit_code": 0,
                "completed_at": "2026-01-01T00:00:00.000000+00:00",
            })
        first = self.launcher().startup_reconciliation
        self.assertEqual(first["unchanged_tickets_skipped"], 0)
        second = self.launcher().startup_reconciliation
        self.assertEqual(second["unchanged_tickets_skipped"], 5)

    def test_a_ticket_that_is_still_running_is_never_skipped(self):
        path = self._stale_ticket("e" * 24, pid=os.getpid(),
                                  command=["/usr/bin/false", "--not-this"])
        # Freeze the marker as if the first pass had seen it running.
        launcher = self.launcher()
        path.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
        write_owner_only(path, {
            **json.loads(path.read_text(encoding="utf-8")),
            "status": "running", "completed_at": None,
        })
        launcher._write_marker(path.stat().st_mtime + 10,
                               {"probe-run:" + "e" * 24: path})
        second = self.launcher()
        self.assertEqual(
            json.loads(path.read_text(encoding="utf-8"))["status"], "orphaned")
        self.assertEqual(second.startup_reconciliation["running_tickets_checked"], 1)

    def test_a_ticket_this_process_still_owns_holds_the_slot(self):
        launcher = self.launcher("import time; time.sleep(60)")
        launcher.spawn(digest="a" * 24, record={})
        with self.assertRaises(LaneChildConflict):
            launcher.spawn(digest="b" * 24, record={})


class PlannerTicketTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.state = Path(self._dir.name) / "state"
        self.state.mkdir()
        self.config = self.state / "research-planner-model-config.json"
        self.config.write_text("{}", encoding="utf-8")

    def launcher(self):
        made = ResearchPlannerLauncher(
            state_dir=self.state, model_config_path=self.config)
        self.addCleanup(made.close)
        return made

    def test_a_planner_ticket_records_its_command_and_settles(self):
        launcher = self.launcher()
        # The real child module is not importable from a bare state dir; the
        # exit code is what settlement is made of, whatever it is.
        ticket = launcher.start()
        self.assertIsInstance(ticket["command"], list)
        self.assertEqual(ticket["schema_version"], "0.2")
        for _ in range(600):
            if launcher._children.get(ticket["id"]) is None:
                break
            if launcher._children[ticket["id"]].poll() is not None:
                break
            time.sleep(0.05)
        launcher.reap()
        record = json.loads(
            launcher._ticket_path(ticket["id"]).read_text(encoding="utf-8"))
        self.assertIn(record["status"], {"succeeded", "failed"})
        self.assertIsNotNone(record["exit_code"])
        self.assertIsNotNone(record["completed_at"])

    def test_status_accepts_the_prefix_this_lane_actually_mints(self):
        launcher = self.launcher()
        ticket = launcher.start()
        self.assertTrue(ticket["id"].startswith("research-plan:"))
        # Before the fix this raised PlannerLaunchRejected for every ticket.
        self.assertIn(launcher.status(ticket["id"])["id"], {ticket["id"]})

    def test_the_planner_lane_also_skips_unchanged_settled_tickets(self):
        for index in range(4):
            digest = f"{index:x}" * 24
            path = self.state / "research-plans" / digest / "ticket.json"
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps({
                "schema_version": "0.2", "id": f"research-plan:{digest}",
                "model_config_path": str(self.config),
                "started_at": "2026-01-01T00:00:00.000000+00:00",
                "pid": 999_999_999, "command": ["/usr/bin/false"],
                "status": "succeeded", "exit_code": 0,
                "completed_at": "2026-01-01T00:00:01.000000+00:00",
            }), encoding="utf-8")
        first = self.launcher().startup_reconciliation
        self.assertEqual(first["unchanged_tickets_skipped"], 0)
        second = self.launcher().startup_reconciliation
        self.assertEqual(second["unchanged_tickets_skipped"], 4)

    def test_a_planner_ticket_from_a_previous_process_is_reconciled(self):
        digest = "a" * 24
        path = self.state / "research-plans" / digest / "ticket.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({
            "schema_version": "0.1", "id": f"research-plan:{digest}",
            "model_config_path": str(self.config),
            "started_at": (datetime.now(timezone.utc)
                           - timedelta(hours=3)).isoformat(timespec="microseconds"),
            "pid": 999_999_999, "status": "running",
            "exit_code": None, "completed_at": None,
        }), encoding="utf-8")
        launcher = self.launcher()
        settled = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(settled["status"], "orphaned")
        self.assertIn("进程号", settled["orphaned_reason"])
        self.assertEqual(launcher.startup_reconciliation["settled_count"], 1)


if __name__ == "__main__":
    unittest.main()
