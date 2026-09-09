"""P14e lane: admit one, settle what finished, hold when nothing moved.

The lane is queueless on purpose -- the plan ranks the inquiries and the loop
authority knows which of them are already tasks -- so what is worth testing is
the four answers it can give a tick: launched, held, ``skipped:pool_exhausted``
and ``not_granted``, plus the settlement it reports alongside them.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dalton_core import research_task as rt
from dalton_core.lane_child_launcher import LaneChildConflict, LaneChildTicketNotFound
from dalton_core.lane_registry import lane_for_operation, registered_lanes
from dalton_core.mission_research_task_lane import (
    LANE,
    LANE_CONFIG,
    LAUNCHER_KWARG,
    ResearchTaskCoordinator,
    argv_fragment,
    build_launcher,
)
from dalton_core.research_task_launcher import ResearchTaskLauncher
from tests.test_research_task import ACN, DAY, ResearchTaskFixture, inquiry


class FakeLauncher:
    """Records what the coordinator asked for; runs nothing."""

    def __init__(self, tickets_dir: Path) -> None:
        self.tickets_dir = tickets_dir
        tickets_dir.mkdir(parents=True, exist_ok=True)
        self.started: list[tuple[str, str]] = []
        self.tickets: dict[str, dict] = {}
        self.conflict = False

    def start(self, *, plan_ref: str, signature: str) -> dict:
        if self.conflict:
            raise LaneChildConflict("research-task child is already running")
        self.started.append((plan_ref, signature))
        ticket = {
            "id": f"research-task:{len(self.started):024d}",
            "started_at": "2026-09-09T00:00:00.000000+00:00",
            "status": "running", "summary": None,
        }
        self.tickets[ticket["id"]] = ticket
        return ticket

    def status(self, ticket_ref: str) -> dict:
        try:
            return self.tickets[ticket_ref]
        except KeyError as exc:
            raise LaneChildTicketNotFound(ticket_ref) from exc

    def settle(self, ticket_ref: str, *, status: str, summary: dict | None) -> None:
        self.tickets[ticket_ref].update({"status": status, "summary": summary})


class LaneTests(ResearchTaskFixture):
    daily_cost_usd = 20.0

    def setUp(self) -> None:
        super().setUp()
        self.launcher = FakeLauncher(self.state_dir / "research-tasks")
        self.now = datetime(2026, 9, 9, 12, 0, tzinfo=timezone.utc)
        self.coordinator = ResearchTaskCoordinator(
            store=self.store, launcher=self.launcher, clock=lambda: self.now,
        )

    def test_no_launcher_is_unconfigured_rather_than_broken(self) -> None:
        lane = ResearchTaskCoordinator(store=self.store, launcher=None)
        self.assertEqual(lane.dispatch_once()["status"], "unconfigured")

    def test_a_plan_with_an_admissible_inquiry_launches_one_child(self) -> None:
        plan = self.record_plan([inquiry(question="Do ACN's margins reconcile?")])
        result = self.coordinator.dispatch_once()
        self.assertEqual(result["status"], "launched")
        self.assertEqual(self.launcher.started[0][0], plan["plan_id"])
        self.assertEqual(result["pool"]["name"], "adhoc")
        self.assertEqual(result["running"], 0)

    def test_a_running_child_makes_the_lane_busy_not_a_second_child(self) -> None:
        self.record_plan([inquiry(question="Do ACN's margins reconcile?")])
        first = self.coordinator.dispatch_once()
        self.assertEqual(self.coordinator.dispatch_once()["status"], "busy")
        self.assertEqual(len(self.launcher.started), 1)
        self.launcher.settle(first["ticket_ref"], status="succeeded", summary={
            "status": "succeeded", "admitted": 1, "refused": [],
        })
        held = self.coordinator.dispatch_once()
        self.assertEqual(held["status"], "held")
        self.assertEqual(held["last"]["admitted"], 1)

    def test_the_hold_lapses_after_an_hour(self) -> None:
        self.record_plan([inquiry(question="Do ACN's margins reconcile?")])
        first = self.coordinator.dispatch_once()
        self.launcher.settle(first["ticket_ref"], status="succeeded", summary={
            "status": "succeeded", "admitted": 0, "refused": [],
        })
        self.assertEqual(self.coordinator.dispatch_once()["status"], "held")
        self.now = self.now + timedelta(hours=2)
        self.assertEqual(self.coordinator.dispatch_once()["status"], "launched")

    def test_a_failed_child_is_held_rather_than_respawned_every_tick(self) -> None:
        self.record_plan([inquiry(question="Do ACN's margins reconcile?")])
        first = self.coordinator.dispatch_once()
        self.launcher.settle(first["ticket_ref"], status="failed", summary={
            "status": "failed", "failure_reason": "the mandate moved",
        })
        held = self.coordinator.dispatch_once()
        self.assertEqual(held["status"], "held")
        self.assertEqual(held["last"]["status"], "failed")
        self.assertEqual(len(self.launcher.started), 1)

    def test_an_exhausted_pool_is_a_skip_with_the_name_c2_will_generalise(self) -> None:
        wire = inquiry(question="Do ACN's margins reconcile?")
        plan = self.record_plan([wire])
        # Spend the whole day's ad-hoc share.
        for index in range(10):
            entry = self.admissions(
                self.record_plan(
                    [inquiry(question=f"ACN question {index}?")],
                    state_hash=f"{index:064d}",
                ),
            )[0]
            if not entry["admissible"]:
                break
            self.admit(plan, entry, inquiry(question=f"ACN question {index}?"))
        result = self.coordinator.dispatch_once()
        self.assertEqual(result["status"], "skipped:pool_exhausted")
        self.assertEqual(result["pool"]["remaining_micros"], 0)
        self.assertEqual(self.launcher.started, [])

    def test_settlement_reports_what_became_of_the_tasks(self) -> None:
        from dalton_core.bounded_planner_loop import BoundedPlannerControlPlane
        from dalton_core.observability import ObservabilityStore
        from dalton_core.scheduler import Scheduler

        wire = inquiry(question="Do ACN's margins reconcile?")
        plan = self.record_plan([wire])
        record = self.admit(plan, self.admissions(plan)[0], wire)
        self.assertEqual(self.coordinator.settle(), {"running": 1, "finished": []})
        control = BoundedPlannerControlPlane(
            self.authority, ObservabilityStore(self.store),
            Scheduler(connection=self.store.connection),
        )
        self.authority.issue_directive(
            record["loop_version_ref"], verbatim_text="drop it",
            control_effect="deprioritize", target_coverage_item_ref=None,
            actor_ref="human:p14e-test-owner",
        )
        proposal = self.authority.submit_proposal(
            record["loop_version_ref"],
            action={"kind": "terminate", "reason": "human_deprioritized"},
            rationale="dropped", actor_ref="automation:coverage-mission",
        )
        control.admit_proposal(proposal["id"])
        settled = self.coordinator.settle()
        self.assertEqual(settled["running"], 0)
        self.assertEqual(settled["finished"][0]["terminal_state"], "human_deprioritized")
        self.assertEqual(
            self.coordinator.dispatch_once()["finished"][0]["task_ref"],
            record["loop_ref"],
        )


class UngrantedLaneTests(ResearchTaskFixture):
    publishes = ()

    def test_an_ungranted_lane_says_which_owner_act_is_missing(self) -> None:
        launcher = FakeLauncher(self.state_dir / "research-tasks")
        coordinator = ResearchTaskCoordinator(store=self.store, launcher=launcher)
        self.record_plan([inquiry(question="Do ACN's margins reconcile?")])
        result = coordinator.dispatch_once()
        self.assertEqual(result["status"], "not_granted")
        self.assertEqual(result["reasons"], ["no_executable_adhoc_template_published"])
        self.assertEqual(launcher.started, [])


class WiringTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state = Path(self.temp.name)

    def test_the_lane_is_registered_once_and_findable(self) -> None:
        self.assertIs(lane_for_operation("dispatch_research_task"), LANE)
        self.assertEqual(LANE.driver_key, "research_task")
        self.assertEqual(LANE.init_kwarg, LAUNCHER_KWARG)
        orders = [spec.order for spec in registered_lanes()]
        self.assertEqual(len(orders), len(set(orders)))

    def test_the_lane_is_absent_until_the_owner_installs_its_configuration(self) -> None:
        class Args:
            db = str(self.state / "core.sqlite")
            research_task_lane = None

        self.assertIsNone(build_launcher(Args()))

        class Context:
            state = self.state

        self.assertEqual(argv_fragment(Context()), [])
        (self.state / LANE_CONFIG).write_text(
            json.dumps({"max_admissions_per_tick": 2}), encoding="utf-8")
        self.assertEqual(
            argv_fragment(Context()),
            ["--research-task-lane", str(self.state / LANE_CONFIG)],
        )
        Args.research_task_lane = self.state / LANE_CONFIG
        launcher = build_launcher(Args())
        self.addCleanup(launcher.close)
        self.assertIsInstance(launcher, ResearchTaskLauncher)
        self.assertEqual(launcher.max_admissions_per_tick, 2)

    def test_the_child_command_names_this_state_and_this_ticket(self) -> None:
        launcher = ResearchTaskLauncher(state_dir=self.state, max_admissions_per_tick=3)
        self.addCleanup(launcher.close)
        command = launcher._command(ticket_dir=self.state / "t")
        self.assertIn("dalton_core.research_task_cli", command)
        self.assertIn(str(self.state.resolve()), command)
        self.assertIn("--max-admissions", command)
        self.assertEqual(command[command.index("--max-admissions") + 1], "3")

    def test_the_same_plan_and_signature_is_the_same_ticket(self) -> None:
        launcher = ResearchTaskLauncher(state_dir=self.state)
        self.addCleanup(launcher.close)
        launcher.python_executable = "/usr/bin/true"
        first = launcher.start(plan_ref="plan:1", signature="0")
        launcher.wait(timeout=30)
        second = launcher.start(plan_ref="plan:1", signature="0")
        launcher.wait(timeout=30)
        self.assertEqual(first["id"], second["id"])
        third = launcher.start(plan_ref="plan:1", signature="1")
        launcher.wait(timeout=30)
        self.assertNotEqual(first["id"], third["id"])


if __name__ == "__main__":
    unittest.main()
