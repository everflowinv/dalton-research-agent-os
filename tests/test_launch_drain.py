"""P9d-11: the deploy drain waits for in-flight lane children, read-only."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

import time

from dalton_core.launch_drain import TICKET_DIRECTORIES, _pid_alive, drain, main, running_tickets


def _ticket(root: Path, lane: str, digest: str, **fields) -> Path:
    directory = root / lane / digest
    directory.mkdir(parents=True)
    record = {"id": f"{lane}:{digest}", "status": "running", "pid": os.getpid(),
              "started_at": "2026-09-07T06:00:00.000000+00:00", **fields}
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


if __name__ == "__main__":
    unittest.main()
