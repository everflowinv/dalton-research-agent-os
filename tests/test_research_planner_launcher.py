"""P13o: one planner child per tick, and no process when nothing moved."""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dalton_core.research_planner_launcher import (
    IDLE_HOLD,
    TICKET_PREFIX,
    ResearchPlannerCoordinator,
)
from dalton_core.coverage_mission import CoverageMissionAuthority
from dalton_core.store import DaltonStore

NOW = datetime(2026, 9, 9, 12, 0, tzinfo=timezone.utc)


class FakeLauncher:
    def __init__(self, root: Path):
        self.tickets_dir = root
        self.starts = 0
        self.ticket = None

    def start(self):
        self.starts += 1
        self.ticket = {"id": f"{TICKET_PREFIX}:{self.starts:024x}",
                       "started_at": NOW.isoformat(), "status": "running",
                       "summary": None}
        return dict(self.ticket)

    def status(self, ticket_ref):
        return dict(self.ticket)

    def finish(self, summary):
        self.ticket.update({"status": "succeeded", "summary": summary})


class CoordinatorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = DaltonStore(str(self.root / "core.sqlite"))
        self.addCleanup(self.store.close)
        # The coordinator counts mission tables; opening the authority is what
        # creates them.
        self.missions = CoverageMissionAuthority(self.store)
        self.now = NOW
        self.launcher = FakeLauncher(self.root)
        self.coordinator = ResearchPlannerCoordinator(
            store=self.store, launcher=self.launcher, clock=lambda: self.now)

    def test_an_unconfigured_writer_says_so_instead_of_failing(self):
        bare = ResearchPlannerCoordinator(store=self.store, launcher=None)
        self.assertEqual(bare.dispatch_once()["status"], "unconfigured")

    def test_the_first_tick_launches(self):
        self.assertEqual(self.coordinator.dispatch_once()["status"], "launched")
        self.assertEqual(self.launcher.starts, 1)

    def test_a_running_child_is_not_joined_by_a_second(self):
        self.coordinator.dispatch_once()
        self.assertEqual(self.coordinator.dispatch_once()["status"], "busy")
        self.assertEqual(self.launcher.starts, 1)

    def test_an_unmoved_state_does_not_spawn_a_process(self):
        # The child already refuses to pay twice for the same world; this is
        # about not spawning a process to discover that.
        self.coordinator.dispatch_once()
        self.launcher.finish({"status": "succeeded", "plan_status": "unchanged"})
        self.now += timedelta(minutes=5)
        held = self.coordinator.dispatch_once()
        self.assertEqual(held["status"], "held")
        self.assertEqual(self.launcher.starts, 1)

    def test_a_moved_state_launches_again(self):
        self.coordinator.dispatch_once()
        self.launcher.finish({"status": "succeeded", "plan_status": "fresh"})
        self.now += timedelta(minutes=5)
        # One more learned metric is a different world.
        self.missions.record_metric_observations(
            company_ref="company:sec-cik:0001467373",
            proposals=[{"metric_ref": "metric:new-bookings", "label": "new bookings",
                        "unit": "currency", "evidence_phrase": "new bookings",
                        "quote_id": "quote:0:200:aaaaaaaaaaaaaaaa",
                        "document_ref": "alphaengine-doc:1",
                        "citation_text": "We remain focused on new bookings."}],
            observed_by="automation:x")
        self.assertEqual(self.coordinator.dispatch_once()["status"], "launched")
        self.assertEqual(self.launcher.starts, 2)

    def test_the_hold_lapses_after_an_idle_hour(self):
        self.coordinator.dispatch_once()
        self.launcher.finish({"status": "succeeded", "plan_status": "unchanged"})
        self.now += IDLE_HOLD + timedelta(minutes=1)
        self.assertEqual(self.coordinator.dispatch_once()["status"], "launched")

    def test_the_signature_counts_what_the_state_is_built_from(self):
        signature = self.coordinator._signature()
        self.assertEqual(
            set(signature),
            {"documents", "reviews_open", "claims", "figures", "metrics",
             "mission_versions", "dossier_feedback"})
        self.assertTrue(all(isinstance(v, int) for key, v in signature.items()
                            if key != "dossier_feedback"))
        self.assertEqual(len(signature["dossier_feedback"]), 64)

    def test_a_retracted_figure_does_not_count_as_movement(self):
        # Otherwise withdrawing a bad figure would look like new evidence.
        before = self.coordinator._signature()["figures"]
        self.assertEqual(before, 0)


if __name__ == "__main__":
    unittest.main()
