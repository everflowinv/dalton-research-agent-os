"""P9d-17a: the extraction coordinator launches, holds and settles truthfully."""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dalton_core.coverage_mission import CoverageMissionAuthority
from dalton_core.document_extraction_launcher import (
    DocumentExtractionCoordinator,
    DocumentExtractionLauncher,
    ExtractionLaunchConflict,
    ExtractionLaunchRejected,
    ExtractionTicketNotFound,
)
from dalton_core.store import DaltonStore
from tests.p9a_fixtures import bootstrap_method_authorities, mission_params
from tests.test_mission_source_discovery import Clock


class FakeLauncher:
    def __init__(self, root: Path) -> None:
        self.tickets_dir = root / "extractions"; self.tickets_dir.mkdir()
        self.starts: list[dict] = []
        self.tickets: dict[str, dict] = {}
        self.next_summary: dict | None = None

    def start(self, *, requested_by=None, max_windows=2, max_numeric_windows=0,
              max_discovery_windows=0):
        ticket = f"document-extraction:{len(self.starts) + 1:024x}"
        self.starts.append({"requested_by": requested_by, "max_windows": max_windows,
                            "max_numeric_windows": max_numeric_windows,
                            "max_discovery_windows": max_discovery_windows})
        self.tickets[ticket] = {"id": ticket, "status": "running", "summary": None, "completed_at": None}
        return {"id": ticket, "status": "running"}

    def finish(self, summary: dict, *, completed_at: str) -> None:
        ticket = self.tickets[self.starts and list(self.tickets)[-1]]
        ticket.update({"status": "succeeded" if summary.get("status") == "succeeded" else "failed",
                       "summary": summary, "completed_at": completed_at})

    def status(self, ticket_ref):
        try:
            return dict(self.tickets[ticket_ref])
        except KeyError as exc:
            raise ExtractionTicketNotFound(ticket_ref) from exc


class CoordinatorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.core = DaltonStore(str(self.root / "core.sqlite")); self.addCleanup(self.core.close)
        self.state = bootstrap_method_authorities(self.core)
        self.missions = CoverageMissionAuthority(self.core)
        self.clock = Clock()
        self.launcher = FakeLauncher(self.root)
        self.coordinator = DocumentExtractionCoordinator(missions=self.missions, launcher=self.launcher, clock=self.clock)

    def _awaiting_review(self) -> None:
        params = mission_params(self.state); ref = params.pop("mission_ref")
        mission = self.missions.create_mission(ref, **params)
        # A review row is the queue; register one directly through the ledger.
        with self.missions._transaction() as cur:
            cur.execute(
                "INSERT INTO coverage_mission_source_discoveries(record_id,mission_version_ref,mission_version_hash,company_ref,source_ref,"
                "discovery_plan_ref,discovery_plan_hash,spec_ref,query_hash,connector_invocation_ref,source_envelope_ref,source_envelope_hash,"
                "actor_ref,requested_by,record_json,content_hash,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                ("mission-source-discovery:" + "a" * 32, mission["id"], mission["content_hash"], "company:sec-cik:0001467373",
                 "source:alphaengine", "p", "h", "s", "q", "i", "e", "eh", "human:x", "human:x", "{}", "c", "2026-09-07T00:00:00+00:00"))
            cur.execute(
                "INSERT INTO coverage_mission_discovered_documents(record_id,mission_version_ref,company_ref,source_ref,document_ref,"
                "discovery_ref,status,ticket_ref,failure_reason,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                ("mission-discovered-document:" + "b" * 32, mission["id"], "company:sec-cik:0001467373", "source:alphaengine",
                 "alphaengine-doc:1", "mission-source-discovery:" + "a" * 32, "acquired", None, None,
                 "2026-09-07T00:00:00+00:00", "2026-09-07T00:00:00+00:00"))
            cur.execute(
                "INSERT INTO coverage_mission_document_reviews(review_id,mission_version_ref,company_ref,source_ref,document_ref,"
                "discovered_document_ref,state,registered_by,created_at,updated_at) VALUES(?,?,?,?,?,?,'awaiting_human_extraction',?,?,?)",
                ("mission-document-review:" + "c" * 32, mission["id"], "company:sec-cik:0001467373", "source:alphaengine",
                 "alphaengine-doc:1", "mission-discovered-document:" + "b" * 32, "human:x",
                 "2026-09-07T00:00:00+00:00", "2026-09-07T00:00:00+00:00"))

    def test_idle_without_reviews_then_launch_busy_settle_and_hold(self) -> None:
        self.assertEqual(self.coordinator.dispatch_once(), {"awaiting": 0, "status": "idle"})
        self._awaiting_review()
        tick = self.coordinator.dispatch_once()
        self.assertEqual((tick["status"], tick["awaiting"], tick["max_windows"]), ("launched", 1, 4))
        self.assertEqual(self.launcher.starts, [{"requested_by": None, "max_windows": 4,
                                                 "max_numeric_windows": 0,
                                                 "max_discovery_windows": 0}])
        self.assertEqual(self.coordinator.dispatch_once()["status"], "busy")
        # A child that drafted something is followed by another child next tick.
        self.launcher.finish({"status": "succeeded", "drafted": [{"offset": 0}], "stop_reason": "max_windows",
                              "reviews_complete": 0}, completed_at=self.clock().isoformat())
        tick = self.coordinator.dispatch_once()
        self.assertEqual((tick["status"], tick["last"]["drafted"], tick["last"]["stop_reason"]), ("launched", 1, "max_windows"))
        # A child that found nothing to draft holds the lane while the queue is unchanged...
        self.launcher.finish({"status": "succeeded", "drafted": [], "stop_reason": "nothing_to_draft", "reviews_complete": 1},
                             completed_at=self.clock().isoformat())
        tick = self.coordinator.dispatch_once()
        self.assertEqual(tick["status"], "held")
        self.assertEqual(len(self.launcher.starts), 2)
        # ...and resumes once an hour has passed.
        self.clock.advance(hours=1, minutes=1)
        self.assertEqual(self.coordinator.dispatch_once()["status"], "launched")
        # A gated child (no model configuration or budget) is held the same way, and says why.
        self.launcher.finish({"status": "succeeded", "drafted": [], "stop_reason": "gated:document_extraction_model_config_not_installed",
                              "reviews_complete": 0}, completed_at=self.clock().isoformat())
        tick = self.coordinator.dispatch_once()
        self.assertEqual((tick["status"], tick["reason"]), ("held", "gated:document_extraction_model_config_not_installed"))
        # A failed child is reported and retried next tick.
        self.clock.advance(hours=2)
        self.coordinator.dispatch_once()
        self.launcher.finish({"status": "failed", "drafted": [], "failure_reason": "unexpected X: boom"}, completed_at=self.clock().isoformat())
        tick = self.coordinator.dispatch_once()
        self.assertEqual((tick["status"], tick["last"]["status"], tick["last"]["failure_reason"]), ("launched", "failed", "unexpected X: boom"))


class LauncherTests(unittest.TestCase):
    def test_launcher_refuses_before_spawning(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); (root / "cfg.json").write_text("{}")
            launcher = DocumentExtractionLauncher(state_dir=root, model_config_path=root / "cfg.json")
            self.addCleanup(launcher.close)
            for bad in ({"requested_by": "bridge:x"}, {"max_windows": 0}, {"max_windows": 51}):
                with self.assertRaises(ExtractionLaunchRejected):
                    launcher.start(**bad)
            with self.assertRaises(ExtractionTicketNotFound):
                launcher.status("document-extraction:" + "0" * 24)
            with self.assertRaises(ExtractionLaunchRejected):
                launcher.status("public-web-fetch:" + "0" * 24)
            staged = DocumentExtractionLauncher(state_dir=root, model_config_path=root / "cfg.json", candidate_staging=root / "s.sqlite")
            command = staged._command(requested_by=None, max_windows=3, ticket_dir=root)
            self.assertIn("--candidate-staging", command)
            self.assertEqual(command[command.index("--max-windows") + 1], "3")
            missing = DocumentExtractionLauncher(state_dir=root, model_config_path=root / "absent.json")
            with self.assertRaises(ExtractionLaunchRejected):
                missing.start()
            self.assertEqual(list((root / "extractions").iterdir()), [])


if __name__ == "__main__":
    unittest.main()


class _OneAwaiting:
    """A mission store with exactly one review awaiting, always."""

    class connection:
        @staticmethod
        def execute(*_args):
            class Row:
                @staticmethod
                def fetchone():
                    return [1]
            return Row()


class SecondaryWorkHoldTests(unittest.TestCase):
    """P11z: a drained prose queue is not an idle lane.

    Live, the prose pass drafted everything open, reported nothing_to_draft,
    and the coordinator held the whole lane for an hour -- while the figures
    and discovery passes still had held documents to read and had been reading
    them. The hold has to mean "nothing left to do", not "nothing left to
    draft".
    """

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.now = datetime(2026, 9, 8, 16, 0, tzinfo=timezone.utc)
        self.launcher = FakeLauncher(self.root)
        self.coordinator = DocumentExtractionCoordinator(
            missions=_OneAwaiting(), launcher=self.launcher, clock=lambda: self.now,
        )

    def settle(self, summary: dict) -> None:
        # Launch and finish one child; the next dispatch both settles it and
        # decides, which is the decision under test.
        self.coordinator.dispatch_once()
        self.launcher.finish(summary, completed_at=self.now.isoformat())

    def test_a_lane_whose_secondary_passes_are_reading_is_launched_again(self):
        self.settle({"status": "succeeded", "drafted": [], "stop_reason": "nothing_to_draft",
                     "reviews_complete": 0, "numeric_fresh": 4, "discovery_fresh": 0})
        self.now += timedelta(minutes=5)
        self.assertEqual(self.coordinator.dispatch_once()["status"], "launched")

    def test_a_lane_with_nothing_left_anywhere_still_holds(self):
        self.settle({"status": "succeeded", "drafted": [], "stop_reason": "nothing_to_draft",
                     "reviews_complete": 0, "numeric_fresh": 0, "discovery_fresh": 0})
        self.now += timedelta(minutes=5)
        held = self.coordinator.dispatch_once()
        self.assertEqual(held["status"], "held")
        self.assertIn("nothing to draft or read", held["reason"])

    def test_discovery_work_alone_is_enough_to_keep_going(self):
        self.settle({"status": "succeeded", "drafted": [], "stop_reason": "nothing_to_draft",
                     "reviews_complete": 0, "numeric_fresh": 0, "discovery_fresh": 2})
        self.now += timedelta(minutes=5)
        self.assertEqual(self.coordinator.dispatch_once()["status"], "launched")

    def test_a_summary_from_before_this_existed_holds_as_it_used_to(self):
        # An older child writes no counts; absent must read as "no secondary
        # work", not as "unknown, keep launching forever".
        self.settle({"status": "succeeded", "drafted": [], "stop_reason": "nothing_to_draft",
                     "reviews_complete": 0})
        self.now += timedelta(minutes=5)
        self.assertEqual(self.coordinator.dispatch_once()["status"], "held")

    def test_the_hold_lapses_after_an_idle_hour_either_way(self):
        self.settle({"status": "succeeded", "drafted": [], "stop_reason": "nothing_to_draft",
                     "reviews_complete": 0, "numeric_fresh": 0, "discovery_fresh": 0})
        self.now += timedelta(hours=2)
        self.assertEqual(self.coordinator.dispatch_once()["status"], "launched")
