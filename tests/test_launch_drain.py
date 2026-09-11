"""P9d-11: the deploy drain waits for in-flight lane children, read-only."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

import time
from unittest import mock

from dalton_core.launch_drain import TICKET_DIRECTORIES, _pid_alive, drain, main, running_tickets


def _ticket(root: Path, lane: str, digest: str, **fields) -> Path:
    directory = root / lane / digest
    directory.mkdir(parents=True)
    record = {"id": f"{lane}:{digest}", "status": "running", "pid": os.getpid(),
              "started_at": "2026-09-07T06:00:00.000000"}
    record.update(fields)
    path = directory / "ticket.json"
    path.write_text(json.dumps(record), encoding="utf-8")
    return path


class LaunchDrainTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_only_running_tickets_with_live_pids_count(self) -> None:
        self.assertEqual(running_tickets(self.root), [])
        live = _ticket(self.root, "fetches", "a" * 24)
        _ticket(self.root, "discoveries", "b" * 24, pid=2**22 - 1)      # dead pid
        _ticket(self.root, "acquisitions", "c" * 24, status="succeeded")  # settled
        _ticket(self.root, "sec-lane-runs", "d" * 24, pid="not-a-pid")
        (self.root / "fetches" / ("e" * 24)).mkdir()
        (self.root / "fetches" / ("e" * 24) / "ticket.json").write_text("{not json", encoding="utf-8")
        found = running_tickets(self.root)
        self.assertEqual([(item["lane"], item["ticket"]) for item in found], [("fetches", "fetches:" + "a" * 24)])
        self.assertEqual(set(TICKET_DIRECTORIES), {"acquisitions", "discoveries", "extractions", "fetches", "sec-lane-runs"})
        # Read-only: the drain never rewrites a ticket.
        before = live.read_text(encoding="utf-8")
        drain(self.root, timeout_seconds=0, poll_seconds=0.01)
        self.assertEqual(live.read_text(encoding="utf-8"), before)

    def test_drain_returns_once_children_finish_and_reports_timeout_honestly(self) -> None:
        path = _ticket(self.root, "fetches", "a" * 24)
        ticks = iter([0.0, 0.0, 1.0, 2.0, 3.0, 4.0, 5.0])
        sleeps: list[float] = []

        def sleep(seconds: float) -> None:
            sleeps.append(seconds)
            if len(sleeps) == 2:
                record = json.loads(path.read_text(encoding="utf-8"))
                record["status"] = "succeeded"
                path.write_text(json.dumps(record), encoding="utf-8")

        result = drain(self.root, timeout_seconds=100, poll_seconds=1.0, clock=lambda: next(ticks), sleep=sleep)
        self.assertTrue(result["drained"])
        self.assertEqual((result["polls"], result["remaining"]), (3, []))
        self.assertEqual(sleeps, [1.0, 1.0])

        stuck = _ticket(self.root, "discoveries", "b" * 24)
        ticks = iter([0.0, 0.0, 2.0, 4.0])
        result = drain(self.root, timeout_seconds=3, poll_seconds=2.0, clock=lambda: next(ticks), sleep=lambda _s: None)
        self.assertFalse(result["drained"])
        self.assertEqual([item["ticket"] for item in result["remaining"]], ["discoveries:" + "b" * 24])
        self.assertEqual(stuck.read_text(encoding="utf-8"), json.dumps(json.loads(stuck.read_text(encoding="utf-8"))))
        with self.assertRaises(ValueError):
            drain(self.root, timeout_seconds=-1)

    def test_an_exited_but_unreaped_child_counts_as_exited(self) -> None:
        """A zombie answers kill(pid, 0); the drain must not wait on it."""

        import subprocess
        import sys
        child = subprocess.Popen([sys.executable, "-c", "pass"])
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            out = subprocess.run(["ps", "-o", "stat=", "-p", str(child.pid)], capture_output=True, text=True).stdout.strip()
            if out.startswith("Z"):
                break
            time.sleep(0.05)
        else:
            self.skipTest("could not observe the child as a zombie on this platform")
        try:
            _ticket(self.root, "fetches", "z" * 24, pid=child.pid)
            self.assertEqual(running_tickets(self.root), [])
            self.assertFalse(_pid_alive(child.pid))
        finally:
            child.wait()

    def test_new_lane_directories_are_drained_without_a_hardcoded_allowlist(self) -> None:
        for lane in ("zero-base-review-runs", "hkex-filings-runs", "future-lane-runs"):
            _ticket(self.root, lane, "x" * 24)
        found = running_tickets(self.root)
        self.assertEqual({item["lane"] for item in found}, {
            "zero-base-review-runs", "hkex-filings-runs", "future-lane-runs"})
        self.assertFalse(drain(self.root, timeout_seconds=0)["drained"])
        _ticket(self.root / "backups" / "old-state", "ignored", "x" * 24)
        self.assertEqual(len(running_tickets(self.root)), 3)

    def test_installer_stops_and_drains_before_upgrading_runtime(self) -> None:
        script = (Path(__file__).resolve().parents[1] / "deploy/macos/install.sh").read_text()
        drain_at = script.index('"$repo_root/src/dalton_core/launch_drain.py"')
        writer_at = script.index("stop_job space.lumos.dalton.writer")
        pip_at = script.index("-m pip install")
        self.assertLess(script.index('stop_job "$label"'), drain_at)
        self.assertLess(drain_at, writer_at)
        self.assertLess(writer_at, pip_at)
        self.assertIn("exit 1", script[drain_at:writer_at])

    def test_cli_exit_code_follows_drain_result(self) -> None:
        self.assertEqual(main(["--state-dir", str(self.root), "--timeout", "0"]), 0)
        _ticket(self.root, "fetches", "a" * 24)
        self.assertEqual(main(["--state-dir", str(self.root), "--timeout", "0", "--poll", "0.01"]), 1)

    def test_reused_pid_with_different_command_does_not_block_drain(self) -> None:
        _ticket(self.root, "research-plans", "a" * 24,
                command=["definitely-not-this-process", "--child"])
        self.assertEqual(running_tickets(self.root), [])

    def test_real_matching_child_blocks_until_it_exits(self) -> None:
        import subprocess
        import sys
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        self.addCleanup(lambda: child.poll() is None and child.kill())
        from datetime import datetime, timezone
        _ticket(self.root, "research-plans", "b" * 24, pid=child.pid,
                command=[sys.executable, "-c", "import time; time.sleep(30)"],
                started_at=datetime.now(timezone.utc).isoformat(timespec="microseconds"))
        self.assertEqual(len(running_tickets(self.root)), 1)
        child.terminate()
        child.wait(timeout=5)
        self.assertEqual(running_tickets(self.root), [])

    def test_identity_lookup_failure_is_conservative(self) -> None:
        _ticket(self.root, "research-plans", "c" * 24,
                command=["unreadable-child", "--run"])
        with mock.patch("dalton_core.launch_drain._process_command_matches", return_value=None):
            self.assertEqual(len(running_tickets(self.root)), 1)

    def test_same_command_with_start_before_ticket_is_reused(self) -> None:
        _ticket(self.root, "research-plans", "d" * 24,
                command=["python3", "child.py"], started_at="2026-09-11T01:00:00+00:00")
        with mock.patch("dalton_core.launch_drain._process_command_matches", return_value=True), \
             mock.patch("dalton_core.launch_drain._process_started_at", return_value=1_700_000_000.0):
            self.assertEqual(running_tickets(self.root), [])

    def test_same_command_with_start_after_ticket_is_reused(self) -> None:
        _ticket(self.root, "research-plans", "e" * 24,
                command=["python3", "child.py"], started_at="2026-09-11T01:00:00+00:00")
        with mock.patch("dalton_core.launch_drain._process_command_matches", return_value=True), \
             mock.patch("dalton_core.launch_drain._process_started_at", return_value=2_000_000_000.0):
            self.assertEqual(running_tickets(self.root), [])

    def test_start_time_precision_accepts_only_pre_ticket_allowance(self) -> None:
        from dalton_core.launch_drain import _ticket_process_matches
        from datetime import datetime
        record = {"pid": os.getpid(), "command": ["python3", "child.py"],
                  "started_at": "2026-09-11T01:00:00+00:00"}
        ticket_epoch = datetime.fromisoformat(record["started_at"]).timestamp()
        ticket_mtime = ticket_epoch + 10
        with mock.patch("dalton_core.launch_drain._process_command_matches", return_value=True), \
             mock.patch("dalton_core.launch_drain._process_started_at", return_value=ticket_epoch - 1):
            self.assertTrue(_ticket_process_matches(record, ticket_mtime))
        with mock.patch("dalton_core.launch_drain._process_command_matches", return_value=True), \
             mock.patch("dalton_core.launch_drain._process_started_at", return_value=ticket_epoch - 1.001):
            self.assertFalse(_ticket_process_matches(record, ticket_mtime))
        with mock.patch("dalton_core.launch_drain._process_command_matches", return_value=True), \
             mock.patch("dalton_core.launch_drain._process_started_at", return_value=ticket_epoch + 11.001):
            self.assertFalse(_ticket_process_matches(record, ticket_mtime))

    def test_delayed_launch_inside_ticket_write_interval_is_live(self) -> None:
        from dalton_core.launch_drain import _ticket_process_matches
        from datetime import datetime
        record = {"pid": os.getpid(), "command": ["python3", "child.py"],
                  "started_at": "2026-09-11T01:00:00+00:00"}
        started = datetime.fromisoformat(record["started_at"]).timestamp()
        ticket_mtime = started + 11
        with mock.patch("dalton_core.launch_drain._process_command_matches", return_value=True), \
             mock.patch("dalton_core.launch_drain._process_started_at", return_value=started + 10):
            self.assertTrue(_ticket_process_matches(record, ticket_mtime))
        with mock.patch("dalton_core.launch_drain._process_command_matches", return_value=None), \
             mock.patch("dalton_core.launch_drain._process_started_at", return_value=started + 10):
            self.assertIsNone(_ticket_process_matches(record, None))

    def test_changed_ticket_snapshot_and_malformed_interval_are_conservative(self) -> None:
        from dalton_core.launch_drain import _ticket_process_matches
        from datetime import datetime
        record = {"pid": os.getpid(), "command": ["python3", "child.py"],
                  "started_at": "2026-09-11T01:00:00+00:00"}
        started = datetime.fromisoformat(record["started_at"]).timestamp()
        with mock.patch("dalton_core.launch_drain._process_command_matches", return_value=None), \
             mock.patch("dalton_core.launch_drain._process_started_at", return_value=started + 100):
            self.assertIsNone(_ticket_process_matches(record, None))
            self.assertIsNone(_ticket_process_matches(record, started - 1))

    def test_running_ticket_changed_during_read_remains_blocking(self) -> None:
        from dalton_core.launch_drain import _read_ticket_snapshot
        path = _ticket(self.root, "research-plans", "9" * 24,
                       command=["python3", "child.py"])
        record = json.loads(path.read_text())
        with mock.patch("dalton_core.launch_drain._read_ticket_snapshot",
                        return_value=(record, None)), \
             mock.patch("dalton_core.launch_drain._process_command_matches", return_value=None):
            self.assertEqual(len(running_tickets(self.root)), 1)
        stable_record, stable_mtime = _read_ticket_snapshot(path)
        self.assertEqual(stable_record, record)
        self.assertIsInstance(stable_mtime, float)

    def test_fstat_unavailable_keeps_running_ticket_conservative(self) -> None:
        path = _ticket(self.root, "research-plans", "8" * 24,
                       command=["python3", "child.py"])
        with mock.patch("dalton_core.launch_drain.os.fstat",
                        side_effect=OSError("fstat unavailable")), \
             mock.patch("dalton_core.launch_drain._process_command_matches", return_value=None):
            self.assertEqual(len(running_tickets(self.root)), 1)

    def test_python_interpreter_alias_is_accepted_on_proc(self) -> None:
        from dalton_core.launch_drain import _process_command_matches
        with mock.patch.object(Path, "is_file", return_value=True), \
             mock.patch.object(Path, "read_bytes", return_value=b"/venv/bin/python3.14\0child.py\0"):
            self.assertTrue(_process_command_matches(os.getpid(), ["/venv/bin/python", "child.py"]))

    def test_ambiguous_bsd_ps_path_with_spaces_is_conservative(self) -> None:
        from dalton_core.launch_drain import _process_command_matches
        completed = mock.Mock(returncode=0, stdout="/Users/name/My Python/bin/python other.py\n")
        with mock.patch.object(Path, "is_file", return_value=False), \
             mock.patch("dalton_core.launch_drain.subprocess.run", return_value=completed):
            self.assertIsNone(_process_command_matches(
                os.getpid(), ["/Users/name/My Python/bin/python", "child.py"]))

    def test_legacy_ticket_can_use_birth_time_and_naive_time_is_conservative(self) -> None:
        old = _ticket(self.root, "research-plans", "f" * 24,
                      started_at="2026-09-11T01:00:00+00:00")
        with mock.patch("dalton_core.launch_drain._process_started_at", return_value=1_700_000_000.0):
            self.assertEqual(running_tickets(self.root), [])
        record = json.loads(old.read_text())
        record["started_at"] = "2026-09-11T01:00:00"
        old.write_text(json.dumps(record))
        with mock.patch("dalton_core.launch_drain._process_started_at", return_value=1_700_000_000.0):
            self.assertEqual(len(running_tickets(self.root)), 1)


if __name__ == "__main__":
    unittest.main()
