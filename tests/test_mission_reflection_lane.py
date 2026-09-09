"""Q2: the weekly lane -- when it fires, when it does not, and how it registers.

The lane's whole job is restraint: it must fire once a week and be silent the
other 2,015 ticks, and it must be silent for a reason it can name rather than
by holding a timer nobody can inspect after a restart.  So most of what is
tested here is what it does *not* do.

The registration test runs a fresh interpreter on purpose.  Every other test
module in this suite has already imported the lane by the time it runs, so an
assertion made in-process would pass even if the registry line were deleted --
it would be finding a registration some other import performed.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dalton_core.lane_child_launcher import LaneChildConflict, LaneChildRejected
from dalton_core.lane_registry import LaunchAgentContext, lane_argv, lane_for_operation
from dalton_core.mission_reflection_lane import (
    LAUNCHER_KWARG,
    MissionReflectionLaneCoordinator,
    ReflectionLauncher,
    may_write_reflection,
)
from dalton_core.research_cycle_reflection import WRITE_SCOPE, reflection_ref_for

ROOT = Path(__file__).resolve().parents[1]
MONDAY = datetime(2026, 9, 7, 9, 30, tzinfo=timezone.utc)

GRANTED = {
    "mission_ref": "coverage-mission:test",
    "id": "coverage-mission-version:test:1",
    "autonomy": {"may_write": ["claim", "deliverable"]},
}
UNGRANTED = {
    "mission_ref": "coverage-mission:test",
    "id": "coverage-mission-version:test:1",
    "autonomy": {"may_write": ["claim"]},
}


class FakeLauncher:
    def __init__(self, *, conflict: bool = False, reject: bool = False) -> None:
        self.started: list[tuple[str, str]] = []
        self.conflict = conflict
        self.reject = reject
        self.tickets: dict[str, dict] = {}

    def start(self, *, iso_week: str, inputs_hash: str) -> dict:
        if self.conflict:
            raise LaneChildConflict("a reflection child is already running")
        if self.reject:
            raise LaneChildRejected("no week")
        self.started.append((iso_week, inputs_hash))
        ticket = {"id": f"research-cycle-reflection:{inputs_hash[:24]:0<24}",
                  "iso_week": iso_week, "inputs_hash": inputs_hash}
        self.tickets[ticket["id"]] = {**ticket, "status": "running", "summary": None}
        return ticket

    def settle(self, ticket_ref: str, *, status: str, summary: dict | None = None) -> None:
        self.tickets[ticket_ref].update(status=status, summary=summary)

    def status(self, ticket_ref: str) -> dict:
        return self.tickets[ticket_ref]


def coordinator(launcher, *, mission=GRANTED, recorded=(), digest="hash-a", now=MONDAY):
    """A lane whose week reads as ``digest`` and whose Ledger holds ``recorded``."""

    known = {(week, item) for week, item in recorded}
    state = {"digest": digest}

    def week_state(mission_record, window):
        return {
            "inputs_hash": state["digest"],
            "already_recorded": (window["iso_week"], state["digest"]) in known,
        }

    lane = MissionReflectionLaneCoordinator(
        launcher=launcher, mission=lambda: mission, week_state=week_state,
        clock=lambda: now,
    )
    lane.set_digest = lambda value: state.__setitem__("digest", value)
    lane.remember = lambda week, value: known.add((week, value))
    return lane


class CadenceTests(unittest.TestCase):
    def test_the_first_tick_of_a_week_launches_one_child_for_the_closed_week(self):
        launcher = FakeLauncher()
        result = coordinator(launcher).dispatch_once()
        self.assertEqual(result["status"], "launched")
        self.assertEqual(result["iso_week"], "2026-W36")
        self.assertEqual(result["inputs_hash"], "hash-a")
        self.assertEqual(launcher.started, [("2026-W36", "hash-a")])

    def test_a_week_already_reflected_on_is_a_duplicate_not_an_idle(self):
        # "Idle" would say there was nothing to do. There was something to do
        # and it is done, and a restart on Wednesday should read as a no-op
        # rather than as a lane with no work.
        launcher = FakeLauncher()
        result = coordinator(launcher, recorded={("2026-W36", "hash-a")}).dispatch_once()
        self.assertEqual(result["status"], "duplicate")
        self.assertEqual(result["reflection_ref"],
                         reflection_ref_for("coverage-mission:test", "2026-W36"))
        self.assertEqual(launcher.started, [])

    def test_a_week_whose_inputs_moved_is_launched_again(self):
        # The documented second version: a cost row that lands on Tuesday about
        # Friday's work makes this week a different reading, and the lane has
        # to notice. Asking "is there a row for this week" never would.
        launcher = FakeLauncher()
        lane = coordinator(launcher, recorded={("2026-W36", "hash-a")})
        self.assertEqual(lane.dispatch_once()["status"], "duplicate")
        lane.set_digest("hash-b")
        again = lane.dispatch_once()
        self.assertEqual(again["status"], "launched")
        self.assertEqual(again["inputs_hash"], "hash-b")
        self.assertEqual(launcher.started, [("2026-W36", "hash-b")])

    def test_an_unreadable_week_is_reported_rather_than_raised(self):
        def explode(mission_record, window):
            raise sqlite3.OperationalError("database is locked")

        lane = MissionReflectionLaneCoordinator(
            launcher=FakeLauncher(), mission=lambda: GRANTED, week_state=explode,
            clock=lambda: MONDAY,
        )
        result = lane.dispatch_once()
        self.assertEqual(result["status"], "unavailable")
        self.assertIn("OperationalError", result["reason"])

    def test_every_tick_for_the_rest_of_the_week_stays_a_duplicate(self):
        launcher = FakeLauncher()
        for offset in range(0, 7):
            with self.subTest(day=offset):
                result = coordinator(
                    launcher, recorded={("2026-W36", "hash-a")},
                    now=MONDAY + timedelta(days=offset),
                ).dispatch_once()
                self.assertEqual(result["status"], "duplicate")
        self.assertEqual(launcher.started, [])

    def test_the_next_monday_is_a_different_week_and_launches_again(self):
        launcher = FakeLauncher()
        result = coordinator(
            launcher, recorded={("2026-W36", "hash-a")}, now=MONDAY + timedelta(days=7),
        ).dispatch_once()
        self.assertEqual(result["status"], "launched")
        self.assertEqual(result["iso_week"], "2026-W37")

    def test_a_second_child_is_never_started_while_one_runs(self):
        launcher = FakeLauncher()
        lane = coordinator(launcher)
        lane.dispatch_once()
        again = lane.dispatch_once()
        self.assertEqual(again["status"], "running")
        self.assertEqual(launcher.started, [("2026-W36", "hash-a")])

    def test_a_busy_launcher_is_reported_rather_than_raised(self):
        result = coordinator(FakeLauncher(conflict=True)).dispatch_once()
        self.assertEqual(result["status"], "busy")

    def test_a_rejected_launch_is_reported_rather_than_raised(self):
        result = coordinator(FakeLauncher(reject=True)).dispatch_once()
        self.assertEqual(result["status"], "rejected")


class GrantTests(unittest.TestCase):
    def test_the_scope_is_deliverable(self):
        self.assertEqual(WRITE_SCOPE, "deliverable")
        self.assertTrue(may_write_reflection(GRANTED))
        self.assertFalse(may_write_reflection(UNGRANTED))
        self.assertFalse(may_write_reflection(None))
        self.assertFalse(may_write_reflection({"autonomy": {"may_write": "deliverable"}}))

    def test_without_the_grant_the_lane_says_so_and_starts_nothing(self):
        launcher = FakeLauncher()
        result = coordinator(launcher, mission=UNGRANTED).dispatch_once()
        self.assertEqual(result["status"], "ungranted")
        self.assertIn("deliverable", result["reason"])
        self.assertEqual(launcher.started, [])

    def test_with_no_mission_at_all_the_lane_is_unconfigured(self):
        result = coordinator(FakeLauncher(), mission=None).dispatch_once()
        self.assertEqual(result["status"], "unconfigured")


class SettlementTests(unittest.TestCase):
    def test_a_finished_child_is_settled_on_the_next_tick(self):
        launcher = FakeLauncher()
        lane = coordinator(launcher)
        started = lane.dispatch_once()
        launcher.settle(started["ticket_ref"], status="succeeded", summary={
            "recorded": {"reflection_ref": "research-cycle-reflection:test:2026-W36",
                         "status": "fresh", "version": 1},
            "backlog_candidates": [{"question": "q", "because": "b", "refs": []}],
            "policy_suggestions": ["s"],
        })
        # The week is now written, so the same coordinator reports duplicate
        # and carries last run's settlement with it.
        lane.remember("2026-W36", "hash-a")
        result = lane.dispatch_once()
        self.assertEqual(result["status"], "duplicate")
        self.assertEqual(result["settled"]["reflection_status"], "fresh")
        self.assertEqual(result["settled"]["backlog_candidates"], 1)

    def test_a_failed_week_is_held_rather_than_retried_every_five_minutes(self):
        launcher = FakeLauncher()
        lane = coordinator(launcher)
        started = lane.dispatch_once()
        launcher.settle(started["ticket_ref"], status="failed", summary={"reason": "boom"})
        held = lane.dispatch_once()
        self.assertEqual(held["status"], "held")
        self.assertEqual(held["reason"], "boom")
        self.assertEqual(launcher.started, [("2026-W36", "hash-a")])

    def test_a_held_week_is_tried_again_once_its_numbers_move(self):
        # The hold is keyed by week *and* reading. A week held on one reading
        # would otherwise be dead until the process restarted, even though the
        # thing that broke may be exactly what changed.
        launcher = FakeLauncher()
        lane = coordinator(launcher)
        started = lane.dispatch_once()
        launcher.settle(started["ticket_ref"], status="failed", summary={"reason": "boom"})
        self.assertEqual(lane.dispatch_once()["status"], "held")
        lane.set_digest("hash-b")
        self.assertEqual(lane.dispatch_once()["status"], "launched")


class LauncherTests(unittest.TestCase):
    def test_the_child_command_is_the_reflection_cli_with_no_model_anything(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            launcher = ReflectionLauncher(state_dir=state, tick_summary_dir=state / "ticks")
            command = launcher._command(ticket_dir=state / "ticket")
            self.assertIn("dalton_core.research_cycle_reflection_cli", command)
            self.assertIn("run", command)
            self.assertIn("--tick-summary-dir", command)
            joined = " ".join(command)
            for forbidden in ("--model-config", "--scheduler-db", "--allow-network"):
                with self.subTest(argument=forbidden):
                    self.assertNotIn(forbidden, joined)

    def test_a_run_is_named_by_its_week_so_two_ticks_are_one_ticket(self):
        with tempfile.TemporaryDirectory() as directory:
            launcher = ReflectionLauncher(state_dir=Path(directory))
            with self.assertRaises(LaneChildRejected):
                launcher.start(iso_week="", inputs_hash="h")
            with self.assertRaises(LaneChildRejected):
                launcher.start(iso_week="2026-W36", inputs_hash="")


class LaunchAgentTests(unittest.TestCase):
    def test_a_state_directory_with_no_core_gets_no_argument(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            self.assertNotIn("--reflection-lane", lane_argv(LaunchAgentContext(state=state)))

    def test_a_state_directory_with_a_core_turns_the_lane_on(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            (state / "core.sqlite").write_bytes(b"")
            argv = lane_argv(LaunchAgentContext(state=state))
            self.assertIn("--reflection-lane", argv)
            self.assertNotIn("--reflection-tick-summary-dir", argv)

    def test_an_archive_of_tick_summaries_is_passed_through_when_one_exists(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            (state / "core.sqlite").write_bytes(b"")
            (state / "tick-summaries").mkdir()
            argv = lane_argv(LaunchAgentContext(state=state))
            self.assertIn("--reflection-tick-summary-dir", argv)
            self.assertIn(str(state / "tick-summaries"), argv)


class RegistrationTests(unittest.TestCase):
    def test_the_lane_is_in_the_registry_in_a_fresh_interpreter(self):
        # A subprocess, because in this process a dozen other test modules have
        # already imported the lane. An in-process assertion would be finding
        # somebody else's import rather than the LANE_MODULES entry.
        script = (
            "import json\n"
            "from dalton_core.lane_registry import registered_lanes, tick_lanes\n"
            "spec = next(s for s in registered_lanes()"
            " if s.operation == 'dispatch_mission_reflection')\n"
            "print(json.dumps({'driver_key': spec.driver_key, 'order': spec.order,"
            " 'core_discovery': spec.core_discovery,"
            " 'init_kwarg': spec.init_kwarg,"
            " 'fields': sorted(spec.param_fields),"
            " 'driven': [s.driver_key for s in tick_lanes()]}))\n"
        )
        completed = subprocess.run(
            [sys.executable, "-c", script], cwd=str(ROOT), capture_output=True, text=True,
            env={"PYTHONPATH": str(ROOT / "src"), "PATH": "/usr/bin:/bin"},
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        spec = json.loads(completed.stdout)
        self.assertEqual(spec["driver_key"], "mission_reflection")
        self.assertEqual(spec["order"], 120)
        self.assertTrue(spec["core_discovery"])
        self.assertEqual(spec["init_kwarg"], LAUNCHER_KWARG)
        self.assertEqual(spec["fields"], [])
        # Last, because it reads what every other lane did this week.
        self.assertEqual(spec["driven"][-1], "mission_reflection")

    def test_the_writer_knows_the_operation_and_takes_no_parameters(self):
        spec = lane_for_operation("dispatch_mission_reflection")
        self.assertIsNotNone(spec)
        self.assertEqual(spec.param_fields, frozenset())
        self.assertIsNotNone(spec.handler)
        self.assertIsNotNone(spec.launcher_factory)
        self.assertTrue(spec.note.strip())


if __name__ == "__main__":
    unittest.main()
