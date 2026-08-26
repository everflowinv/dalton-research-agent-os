"""S7d: writer-owned launcher for out-of-process SEC company-facts lane runs.

These tests exercise the launcher's fail-closed checks, ticket lifecycle and
single-slot rule with a stub child executable, so they do not depend on the
lane CLI or the generic governance module landing in the sibling slices.
"""

from __future__ import annotations

import json
import os
import stat
import tempfile
import textwrap
import unittest
from pathlib import Path
from types import SimpleNamespace

from dalton_core.sec_lane_launcher import (
    CLI_MODULE,
    LaneLaunchConflict,
    LaneLaunchRejected,
    LaneTicketNotFound,
    SecLaneLauncher,
)
from dalton_core.store import DaltonStore


OWNER = "human:lumos"
ISSUERS = ["ACN", "CTSH", "EPAM", "IBM"]


def _governance(*, approved: bool = True, approved_by: str = OWNER) -> SimpleNamespace:
    return SimpleNamespace(
        id="connector-governance:sec-company-facts:v1",
        content_hash="a" * 64,
        status="approved" if approved else "proposed",
        approved=approved,
        approved_by=approved_by,
        capability_id="capability:dalton:connector:sec-edgar",
    )


def _stub_child(root: Path, *, exit_code: int = 0, sleep_seconds: float = 0.0) -> Path:
    """A stand-in for ``python -m dalton_core.sec_lane_cli``.

    It records the argv it received, writes ``summary.json`` into
    ``--summary-dir`` and exits with the requested code.
    """

    stub = root / "stub-python"
    stub.write_text(textwrap.dedent(f"""\
        #!/usr/bin/env python3
        import json, os, sys, time
        argv = sys.argv[1:]
        summary_dir = argv[argv.index("--summary-dir") + 1]
        time.sleep({sleep_seconds!r})
        with open(os.path.join(summary_dir, "argv.json"), "w") as handle:
            json.dump(argv, handle)
        with open(os.path.join(summary_dir, "summary.json"), "w") as handle:
            json.dump({{"schema_version": "0.1", "status": "stub", "argv_count": len(argv)}}, handle)
        os.chmod(os.path.join(summary_dir, "summary.json"), 0o600)
        sys.exit({exit_code})
        """), encoding="utf-8")
    stub.chmod(stub.stat().st_mode | stat.S_IXUSR)
    return stub


class SecLaneLauncherTests(unittest.TestCase):
    def _launcher(self, root: Path, **kwargs) -> SecLaneLauncher:
        state = root / "state"
        state.mkdir(exist_ok=True)
        DaltonStore(str(state / "core.sqlite")).close()
        staging = root / "review" / "candidate-staging.sqlite"
        staging.parent.mkdir(exist_ok=True)
        gov = root / "sec-gov.json"
        gov.write_text("{}\n", encoding="utf-8")
        kwargs.setdefault("governance_loader", lambda _path: _governance())
        if "python_executable" not in kwargs:
            # Only build the default stub when the test did not supply one:
            # ``_stub_child`` rewrites the same file.
            kwargs["python_executable"] = str(_stub_child(root))
        kwargs.setdefault("mode_args", ("--fixture-company-facts", str(root / "facts.json")))
        launcher = SecLaneLauncher(
            state_dir=state, governance_path=gov, staging_path=staging, **kwargs
        )
        self.addCleanup(launcher.close)
        return launcher

    def test_refuses_before_spawning(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            proposed = self._launcher(root, governance_loader=lambda _p: _governance(approved=False))
            with self.assertRaises(LaneLaunchRejected):
                proposed.start(issuers=ISSUERS, filed_from="2026-06-01", filed_to="2026-08-26", actor_ref=OWNER)
            bot = self._launcher(root, governance_loader=lambda _p: _governance(approved_by="automation:bot"))
            with self.assertRaises(LaneLaunchRejected):
                bot.start(issuers=ISSUERS, filed_from="2026-06-01", filed_to="2026-08-26", actor_ref=OWNER)
            broken = self._launcher(root, governance_loader=lambda _p: (_ for _ in ()).throw(ValueError("hash")))
            with self.assertRaises(LaneLaunchRejected):
                broken.start(issuers=ISSUERS, filed_from="2026-06-01", filed_to="2026-08-26", actor_ref=OWNER)
            launcher = self._launcher(root)
            bad_requests = [
                dict(issuers=ISSUERS, filed_from="2026-06-01", filed_to="2026-08-26", actor_ref="bridge:tailscale-dashboard"),
                dict(issuers=[], filed_from="2026-06-01", filed_to="2026-08-26", actor_ref=OWNER),
                dict(issuers="ACN", filed_from="2026-06-01", filed_to="2026-08-26", actor_ref=OWNER),
                dict(issuers=["acn"], filed_from="2026-06-01", filed_to="2026-08-26", actor_ref=OWNER),
                dict(issuers=["ACN", "ACN"], filed_from="2026-06-01", filed_to="2026-08-26", actor_ref=OWNER),
                dict(issuers=["../x"], filed_from="2026-06-01", filed_to="2026-08-26", actor_ref=OWNER),
                dict(issuers=ISSUERS, filed_from="2026/06/01", filed_to="2026-08-26", actor_ref=OWNER),
                dict(issuers=ISSUERS, filed_from="2026-02-30", filed_to="2026-08-26", actor_ref=OWNER),
                dict(issuers=ISSUERS, filed_from="2026-09-01", filed_to="2026-08-26", actor_ref=OWNER),
                dict(issuers=[f"T{i}" for i in range(9)], filed_from="2026-06-01", filed_to="2026-08-26", actor_ref=OWNER),
            ]
            for request in bad_requests:
                with self.assertRaises(LaneLaunchRejected, msg=request):
                    launcher.start(**request)
            with self.assertRaises(LaneLaunchRejected):
                launcher.status("alphaengine-acquisition:" + "0" * 24)
            with self.assertRaises(LaneTicketNotFound):
                launcher.status("sec-lane-run:" + "0" * 24)
            self.assertEqual(list((root / "state" / "sec-lane-runs").iterdir()), [])

    def test_ticket_lifecycle_command_shape_and_single_slot(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            launcher = self._launcher(
                root,
                python_executable=str(_stub_child(root, sleep_seconds=0.6)),
                spool_dir=root / "state" / "transcript-spool",
                user_agent="Dalton test lane",
            )
            ticket = launcher.start(
                issuers=ISSUERS, filed_from="2026-06-01", filed_to="2026-08-26", actor_ref=OWNER,
            )
            self.assertEqual(ticket["status"], "running")
            self.assertEqual(ticket["transport"], "rehearsal")
            self.assertEqual(ticket["issuers"], ISSUERS)
            self.assertEqual(ticket["governance_ref"], "connector-governance:sec-company-facts:v1")
            self.assertTrue(ticket["id"].startswith("sec-lane-run:"))
            with self.assertRaises(LaneLaunchConflict):
                launcher.start(issuers=["IBM"], filed_from="2026-06-01", filed_to="2026-08-26", actor_ref=OWNER)
            running = launcher.status(ticket["id"])
            self.assertEqual(running["status"], "running")
            self.assertIsNone(running["summary"])
            self.assertEqual(launcher.wait(timeout=30), 0)
            done = launcher.status(ticket["id"])
            self.assertEqual(done["status"], "succeeded")
            self.assertEqual(done["exit_code"], 0)
            self.assertEqual(done["summary"]["status"], "stub")
            ticket_dir = root / "state" / "sec-lane-runs" / ticket["id"].split(":", 1)[1]
            for name in ("ticket.json", "summary.json", "run.log"):
                self.assertEqual((ticket_dir / name).stat().st_mode & 0o777, 0o600, name)
            self.assertEqual(ticket_dir.stat().st_mode & 0o777, 0o700)
            argv = json.loads((ticket_dir / "argv.json").read_text(encoding="utf-8"))
            self.assertEqual(argv[:2], ["-m", CLI_MODULE])
            self.assertEqual(argv[argv.index("--state-dir") + 1], str((root / "state").resolve()))
            self.assertEqual(argv[argv.index("--staging") + 1], str((root / "review" / "candidate-staging.sqlite").resolve()))
            self.assertEqual(argv[argv.index("--governance") + 1], str((root / "sec-gov.json").resolve()))
            self.assertEqual(argv[argv.index("--actor") + 1], OWNER)
            self.assertEqual(argv[argv.index("--filed-from") + 1], "2026-06-01")
            self.assertEqual(argv[argv.index("--filed-to") + 1], "2026-08-26")
            self.assertEqual([argv[i + 1] for i, item in enumerate(argv) if item == "--issuer"], ISSUERS)
            self.assertEqual(argv[argv.index("--spool-dir") + 1], str((root / "state" / "transcript-spool").resolve()))
            self.assertEqual(argv[argv.index("--user-agent") + 1], "Dalton test lane")
            self.assertIn("--quiet", argv)
            self.assertEqual(argv[-2:], ["--fixture-company-facts", str(root / "facts.json")])
            self.assertNotIn("--allow-network", argv)
            # Slot frees once the child exits; a second run gets a new ticket.
            again = launcher.start(issuers=["IBM"], filed_from="2026-06-01", filed_to="2026-08-26", actor_ref=OWNER)
            self.assertNotEqual(again["id"], ticket["id"])
            self.assertEqual(launcher.wait(timeout=30), 0)

    def test_failed_child_and_orphaned_ticket_are_reported_truthfully(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            launcher = self._launcher(root, python_executable=str(_stub_child(root, exit_code=3)))
            ticket = launcher.start(issuers=["ACN"], filed_from="2026-06-01", filed_to="2026-08-26", actor_ref=OWNER)
            self.assertEqual(launcher.wait(timeout=30), 3)
            status = launcher.status(ticket["id"])
            self.assertEqual(status["status"], "failed")
            self.assertEqual(status["exit_code"], 3)
            # A writer restart loses the process handle; a dead pid must not be
            # promoted to success from the summary file on disk.
            ticket_path = root / "state" / "sec-lane-runs" / ticket["id"].split(":", 1)[1] / "ticket.json"
            record = json.loads(ticket_path.read_text(encoding="utf-8"))
            record["status"] = "running"
            record["exit_code"] = None
            record["completed_at"] = None
            record["pid"] = 2**22 - 1
            ticket_path.write_text(json.dumps(record), encoding="utf-8")
            fresh = SecLaneLauncher(
                state_dir=root / "state", governance_path=root / "sec-gov.json",
                staging_path=root / "review" / "candidate-staging.sqlite",
                governance_loader=lambda _p: _governance(),
            )
            self.addCleanup(fresh.close)
            orphan = fresh.status(ticket["id"])
            self.assertEqual(orphan["status"], "orphaned")
            self.assertIsNotNone(orphan["completed_at"])

    def test_live_mode_passes_allow_network_and_default_governance_loader_is_lazy(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            launcher = self._launcher(root, mode_args=("--allow-network",))
            self.assertTrue(launcher.networked)
            ticket = launcher.start(issuers=["ACN"], filed_from="2026-06-01", filed_to="2026-08-26", actor_ref=OWNER)
            self.assertEqual(ticket["transport"], "sec-public-https")
            self.assertEqual(launcher.wait(timeout=30), 0)
            argv = json.loads(
                (root / "state" / "sec-lane-runs" / ticket["id"].split(":", 1)[1] / "argv.json").read_text()
            )
            self.assertEqual(argv[-1], "--allow-network")
            # Without an injected loader the launcher goes through the generic
            # governance module; an unreadable record is a rejection, never a crash.
            state = root / "state"
            default = SecLaneLauncher(
                state_dir=state, governance_path=root / "missing.json",
                staging_path=root / "review" / "candidate-staging.sqlite",
            )
            self.addCleanup(default.close)
            with self.assertRaises(LaneLaunchRejected):
                default.start(issuers=["ACN"], filed_from="2026-06-01", filed_to="2026-08-26", actor_ref=OWNER)
            self.assertEqual(os.listdir(state / "sec-lane-runs"), [ticket["id"].split(":", 1)[1]])


if __name__ == "__main__":
    unittest.main()
