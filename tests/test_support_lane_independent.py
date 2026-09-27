"""2026-09-27: the support checks do not stop when the extraction queue does.

ws-7d, 2026-09-27: policy-5 published mission v5 at 08:57:50 over five open v4
reviews (four sales notes, one wiki page).  No coordinator carries a feed
lane's documents across a version bump, so the queue of the version in force
read ``awaiting: 0`` from 09:00 on; the coordinator returned ``idle`` without a
child, and the support recheck, the support backfill and its re-review --
which ran only inside that child -- stopped with it, an hour before the owner
resumed the backfill.

* open reviews a superseded version left behind are carried into the current
  one (newest row speaks, the current grant decides, ``created_at`` kept);
* with the queue empty the coordinator starts a support-only child, paced by
  what the last run's support checks got done;
* the support-only child reads no window and runs only the support checks.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from dalton_core import document_extraction_cli
from dalton_core.coverage_mission import CoverageMissionAuthority
from dalton_core.document_extraction_cli import build_parser, run_extraction
from dalton_core.document_extraction_launcher import (
    BUSY_HOLD,
    IDLE_HOLD,
    DocumentExtractionCoordinator,
    DocumentExtractionLauncher,
    support_progress,
)
from dalton_core.store import DaltonStore
from tests import test_document_extraction_automation as _automation
from tests import test_document_extraction_launcher as _launcher_tests
from tests import test_mission_version_carry as _carry
from tests.p9a_fixtures import bootstrap_method_authorities, mission_params
from tests.test_mission_source_discovery import Clock

REF = _carry.REF


def _own_tests_only(cls):
    for base in cls.__mro__[1:]:
        for name in dir(base):
            if name.startswith("test") and name not in cls.__dict__:
                setattr(cls, name, None)
    return cls


class SupportFakeLauncher(_launcher_tests.FakeLauncher):
    def start(self, *, requested_by=None, max_windows=2, max_numeric_windows=0,
              max_discovery_windows=0, support_only=False):
        ticket = super().start(requested_by=requested_by, max_windows=max_windows,
                               max_numeric_windows=max_numeric_windows,
                               max_discovery_windows=max_discovery_windows)
        if support_only:
            self.starts[-1]["support_only"] = True
        return ticket


class SupportProgressTests(unittest.TestCase):
    def test_what_counts_as_progress(self) -> None:
        self.assertFalse(support_progress({}))
        self.assertFalse(support_progress({"support_backfill": {"status": "disabled"}}))
        self.assertTrue(support_progress({"support_recheck": {"asked": 3, "deferred": None}}))
        # Deferred -- a ceiling or the hourly retry -- is not something a
        # next tick changes.
        self.assertFalse(support_progress({"support_recheck": {"asked": 3, "deferred": "cap"}}))
        self.assertTrue(support_progress({"support_backfill": {
            "examined": 0, "rereview": {"asked": 4, "deferred": None}}}))
        self.assertTrue(support_progress({"support_backfill": {"examined": 2}}))
        self.assertFalse(support_progress({"support_backfill": {
            "examined": 0, "rereview": {"asked": 0, "candidates": 0}}}))


class SupportOnlyCoordinatorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.core = DaltonStore(str(self.root / "core.sqlite")); self.addCleanup(self.core.close)
        self.state = bootstrap_method_authorities(self.core)
        self.missions = CoverageMissionAuthority(self.core)
        self.clock = Clock()
        self.launcher = SupportFakeLauncher(self.root)
        self.coordinator = DocumentExtractionCoordinator(
            missions=self.missions, launcher=self.launcher, clock=self.clock)

    def _mission(self) -> None:
        params = mission_params(self.state); ref = params.pop("mission_ref")
        self.missions.create_mission(ref, **params)

    def test_no_mission_is_still_idle_without_a_child(self) -> None:
        self.assertEqual(self.coordinator.dispatch_once(), {"awaiting": 0, "status": "idle"})
        self.assertEqual(self.launcher.starts, [])

    def test_an_empty_queue_starts_a_support_only_child_paced_by_its_progress(self) -> None:
        self._mission()
        tick = self.coordinator.dispatch_once()
        self.assertEqual((tick["status"], tick["mode"], tick["awaiting"]),
                         ("launched", "support_only", 0))
        self.assertEqual(self.launcher.starts, [{
            "requested_by": None, "max_windows": 1, "max_numeric_windows": 0,
            "max_discovery_windows": 0, "support_only": True}])
        latest = json.loads((self.launcher.tickets_dir / "latest.json").read_text())
        self.assertTrue(latest["support_only"])
        # One slot: a running child is never doubled.
        self.assertEqual(self.coordinator.dispatch_once()["status"], "busy")
        # Progress: the next support-only child comes a tick later ...
        self.launcher.finish({"status": "succeeded", "stop_reason": "support_only", "drafted": [],
                              "support_recheck": {"asked": 24, "deferred": None}},
                             completed_at=self.clock().isoformat())
        tick = self.coordinator.dispatch_once()
        self.assertEqual((tick["status"], tick["support"], tick["hold_seconds"]),
                         ("idle", "held", int(BUSY_HOLD.total_seconds())))
        self.assertTrue(tick["last"]["support_progress"])
        self.clock.advance(minutes=6)
        self.assertEqual(self.coordinator.dispatch_once()["mode"], "support_only")
        # ... nothing done (or deferred): it rests an hour, as a drained queue always has.
        self.launcher.finish({"status": "succeeded", "stop_reason": "support_only", "drafted": [],
                              "support_recheck": {"asked": 0},
                              "support_backfill": {"status": "idle", "examined": 0}},
                             completed_at=self.clock().isoformat())
        tick = self.coordinator.dispatch_once()
        self.assertEqual((tick["status"], tick["hold_seconds"]),
                         ("idle", int(IDLE_HOLD.total_seconds())))
        self.clock.advance(minutes=30)
        self.assertEqual(self.coordinator.dispatch_once()["status"], "idle")
        self.assertEqual(len(self.launcher.starts), 2)
        self.clock.advance(minutes=31)
        self.assertEqual(self.coordinator.dispatch_once()["mode"], "support_only")
        self.assertEqual(len(self.launcher.starts), 3)

    def test_a_drafting_run_that_emptied_the_queue_paces_the_first_support_only_run(self) -> None:
        self._mission()
        # A drafting child that already ran the support checks with nothing to do.
        self.launcher.start(max_windows=4)
        (self.launcher.tickets_dir / "latest.json").write_text(json.dumps({
            "ticket": list(self.launcher.tickets)[-1], "settled": False,
            "awaiting_at_launch": 1, "model_config_fingerprint": "x"}), encoding="utf-8")
        self.launcher.finish({"status": "succeeded", "stop_reason": "drained", "drafted": [{}],
                              "support_backfill": {"status": "disabled"}},
                             completed_at=self.clock().isoformat())
        tick = self.coordinator.dispatch_once()
        self.assertEqual((tick["status"], tick["support"]), ("idle", "held"))
        self.assertEqual(len(self.launcher.starts), 1)

    def test_the_real_launcher_passes_the_flag_and_the_child_accepts_it(self) -> None:
        launcher = DocumentExtractionLauncher(state_dir=self.root / "state",
                                              model_config_path=self.root / "m.json")
        command = launcher._command(requested_by=None, max_windows=1, ticket_dir=self.root,
                                    support_only=True)
        self.assertIn("--support-only", command)
        self.assertNotIn("--support-only", launcher._command(
            requested_by=None, max_windows=1, ticket_dir=self.root))
        args = build_parser().parse_args(
            ["--state-dir", "s", "--model-config", "m", *command[command.index("--max-windows"):]])
        self.assertTrue(args.support_only)


@_own_tests_only
class AwaitingCarryTests(_carry._VersionHarness):
    """The five open v4 reviews policy-5's v5 left behind."""

    def test_an_open_review_left_on_a_superseded_version_is_carried(self) -> None:
        old = self._review()
        # A feed lane's document: no plan-driven carry-forward runs for it.
        v3 = self._publish(3, carry=False)
        self.assertEqual(self._owed_reads(), [])
        [carried] = self.m.carry_forward_awaiting_reviews(REF)
        self.assertEqual((carried["status"], carried["review_id"], carried["from_version_ref"]),
                         ("carried", self.review_id, self.v2["id"]))
        self.assertEqual(self._owed_reads(), [(v3["id"], old["document_ref"])])
        new = self._review(carried["carried_to"]["review_id"])
        self.assertEqual((new["state"], new["created_at"], new["company_ref"], new["source_ref"]),
                         ("awaiting_human_extraction", old["created_at"], old["company_ref"],
                          old["source_ref"]))
        [row] = self.m.discovered_documents(v3["id"])
        old_row = self.m.discovered_documents(self.v2["id"])[0]
        self.assertEqual((row["status"], row["ticket_ref"], row["discovery_ref"]),
                         ("acquired", old_row["ticket_ref"], old_row["discovery_ref"]))
        # Idempotent, and the other carries agree it is v3's now.
        self.assertEqual(self.m.carry_forward_awaiting_reviews(REF), [])
        self.assertEqual(self.m.carry_forward_superseded_documents(REF), [])
        self.assertEqual(self._owed_reads(), [(v3["id"], old["document_ref"])])

    def test_a_decided_document_is_never_reopened_by_the_carry(self) -> None:
        self._decide()
        self._publish(3, carry=False)
        self.assertEqual(self.m.carry_forward_awaiting_reviews(REF), [])
        self.assertEqual(self._owed_reads(), [])

    def test_only_the_newest_row_speaks(self) -> None:
        # v3 carries the document the ordinary way and decides it; v2's open
        # row is older and must not come back into v4.
        v3 = self._publish(3)
        [review] = [r for r in self.m.document_reviews(v3["id"])
                    if r["state"] == "awaiting_human_extraction"]
        self.review_id = review["review_id"]
        self._decide()
        self._publish(4, carry=False)
        self.assertEqual(self.m.carry_forward_awaiting_reviews(REF), [])
        self.assertEqual(self._owed_reads(), [])

    def test_the_current_grant_decides(self) -> None:
        v3 = self._publish(3, carry=False)
        with patch.object(self.m, "authorize_source_discovery",
                          side_effect=_carry.CoverageMissionConflict("source is not connected")):
            [skipped] = self.m.carry_forward_awaiting_reviews(REF)
        self.assertEqual(skipped["status"], "skipped")
        self.assertIn("not connected", skipped["reason"])
        self.assertEqual(self.m.discovered_documents(v3["id"]), [])

    def test_the_coordinator_carries_before_it_counts_the_queue(self) -> None:
        self._publish(3, carry=False)
        (self.root / "coordinator").mkdir()
        launcher = SupportFakeLauncher(self.root / "coordinator")
        coordinator = DocumentExtractionCoordinator(missions=self.m, launcher=launcher)
        tick = coordinator.dispatch_once()
        self.assertEqual((tick["status"], tick["awaiting"], tick["carried_awaiting"]["carried"]),
                         ("launched", 1, 1))
        self.assertNotIn("support_only", launcher.starts[0])


@_own_tests_only
class SupportOnlyChildTests(_automation.AutomationDraftingTests):
    def test_a_support_only_run_reads_nothing_and_runs_the_support_checks(self) -> None:
        self._grant_automation()
        fixture = self.root / "fixture.json"
        fixture.write_text('{"schema_version":"0.1","suggestions":[]}', encoding="utf-8")
        base = document_extraction_cli.ExtractionHost

        class Host(base):
            def __init__(inner, **kwargs):
                super().__init__(**kwargs)
                inner._claim_support_verifier = object()

        calls: list[str] = []
        with patch.object(document_extraction_cli, "ExtractionHost", Host), \
                patch.object(document_extraction_cli, "_run_claim_support_recheck",
                             lambda host, db: calls.append("recheck") or {"status": "idle"}), \
                patch.object(document_extraction_cli, "_secondary_sweep",
                             side_effect=AssertionError("no secondary pass")):
            summary = run_extraction(
                state_dir=self.root, model_config_path=self._model_config(),
                summary_dir=self.root / "support-only", spool_dir=self.root / "spool",
                scheduler_db=self.root / "scheduler.sqlite", requested_by=None,
                max_windows=1, max_numeric_windows=0, max_discovery_windows=0,
                connector_governance=None, web_fetch_governance=None,
                hermetic_fixture=fixture, candidate_staging=self.root / "staging.sqlite",
                support_only=True)
        self.assertEqual((summary["status"], summary["stop_reason"], summary["support_only"]),
                         ("succeeded", "support_only", True))
        # One review is open, and none was read.
        self.assertEqual((summary["reviews_scanned"], summary["drafted"], summary["admitted"]),
                         (0, [], []))
        self.assertEqual(calls, ["recheck"])
        self.assertEqual(summary["support_recheck"], {"status": "idle"})

    def test_the_backfill_runs_in_a_support_only_run_too(self) -> None:
        self._grant_automation()
        calls: list[str] = []
        with patch.object(document_extraction_cli, "_install_claim_support_verifier",
                          lambda host, config, db: {"status": "unavailable"}), \
                patch.object(document_extraction_cli, "_run_claim_support_backfill",
                             lambda host, config, db: calls.append("backfill") or {"status": "idle"}):
            summary = run_extraction(
                state_dir=self.root, model_config_path=self._model_config(),
                summary_dir=self.root / "support-only-broker", spool_dir=self.root / "spool",
                scheduler_db=self.root / "scheduler.sqlite", requested_by=None,
                max_windows=1, max_numeric_windows=0, max_discovery_windows=0,
                connector_governance=None, web_fetch_governance=None,
                hermetic_fixture=None, support_only=True)
        self.assertEqual((summary["stop_reason"], summary["reviews_scanned"]), ("support_only", 0))
        self.assertEqual(calls, ["backfill"])


if __name__ == "__main__":
    unittest.main()
