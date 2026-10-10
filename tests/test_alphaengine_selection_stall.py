"""2026-09-29: AlphaEngine acquisitions stalled behind candidate selection.

Both environments' heartbeats said ``selection_pending: CandidateSelectionError:
candidate companies is invalid`` for days (legacy 783 discovered documents,
ws-7d 1,600), while the coordinator state, the tick ledger and the parity
report all said ``idle``.  And ws-7d's SEC filings index failed every search
from 09-27 without the parity report noticing.

These are the reporting and quarantine halves; the truncation itself is in
``test_discovery_candidate_selection``, the SEC receipt in
``test_sec_filings_index_core``, and the whole loop in the new-workspace
canary.
"""

from __future__ import annotations

import contextlib
import io
import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dalton_core.discovery_selection_quarantine import (
    DiscoverySelectionQuarantine, QuarantineError, main as quarantine_main,
)
from dalton_core.mission_source_discovery import discovery_coordinator_state
from dalton_core.tick_ledger import TickLedger, blocked_coordinators, bounded_counts

KEYS = ("alphaengine", "sec_filings_index", "web_search")
PENDING_REASON = "CandidateSelectionError: candidate companies is invalid"
LEASE_REASON = "LeaseNotUsable: capability approval changed after lease issuance"


class QuarantineStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.state = Path(temp.name)
        self.store = DiscoverySelectionQuarantine(
            self.state / "discovery-selections" / "quarantine")

    def test_a_quarantine_is_idempotent_audited_and_bound_to_the_rules(self) -> None:
        first = self.store.quarantine(discovery_ref="mission-source-discovery:a",
                                      reason=PENDING_REASON, error_type="CandidateSelectionError",
                                      rules_version="rules:1", detail={"spec_ref": "sell-side"})
        again = self.store.quarantine(discovery_ref="mission-source-discovery:a",
                                      reason=PENDING_REASON, error_type="CandidateSelectionError",
                                      rules_version="rules:1")
        self.assertEqual(first, again)
        self.assertEqual(len(first["events"]), 1)
        self.assertEqual(self.store.active(rules_version="rules:1"), ["mission-source-discovery:a"])
        # A rule fix bumps the version and the quarantine lapses by itself.
        self.assertEqual(self.store.active(rules_version="rules:2"), [])
        path = self.store.path("mission-source-discovery:a")
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)
        # Failing the same way under the new rules is a new, appended event.
        third = self.store.quarantine(discovery_ref="mission-source-discovery:a",
                                      reason=PENDING_REASON, error_type="CandidateSelectionError",
                                      rules_version="rules:2")
        self.assertEqual([e["rules_version"] for e in third["events"]], ["rules:1", "rules:2"])

    def test_release_is_a_human_decision_and_keeps_the_history(self) -> None:
        self.store.quarantine(discovery_ref="d:1", reason="x", error_type="E", rules_version="r")
        with self.assertRaises(QuarantineError):
            self.store.release("d:1", actor_ref="automation:coverage-mission", reason="no")
        with self.assertRaises(QuarantineError):
            self.store.release("d:1", actor_ref="human:owner", reason=" ")
        released = self.store.release("d:1", actor_ref="human:owner", reason="rule fixed")
        self.assertEqual(released["status"], "released")
        self.assertEqual([e["event"] for e in released["events"]], ["quarantined", "released"])
        self.assertEqual(self.store.active(rules_version="r"), [])
        with self.assertRaises(QuarantineError):
            self.store.release("d:1", actor_ref="human:owner", reason="twice")

    def test_an_edited_record_is_refused_not_believed(self) -> None:
        self.store.quarantine(discovery_ref="d:1", reason="x", error_type="E", rules_version="r")
        path = self.store.path("d:1")
        value = json.loads(path.read_text())
        value["status"] = "released"
        path.write_text(json.dumps(value))
        with self.assertRaises(QuarantineError):
            self.store.record("d:1")
        self.assertEqual(self.store.records(), [])

    def test_the_cli_lists_and_releases_only_with_apply(self) -> None:
        self.store.quarantine(discovery_ref="d:1", reason="x", error_type="E",
                              rules_version="discovery-candidate-view-rules:2")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(quarantine_main(["--state-dir", str(self.state), "list"]), 0)
        self.assertEqual(json.loads(out.getvalue())["active"], ["d:1"])
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(quarantine_main([
                "--state-dir", str(self.state), "release", "--discovery-ref", "d:1",
                "--actor", "human:owner", "--reason", "check"]), 0)
        self.assertEqual(self.store.record("d:1")["status"], "quarantined")
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(quarantine_main([
                "--state-dir", str(self.state), "release", "--discovery-ref", "d:1",
                "--actor", "human:owner", "--reason", "check", "--apply"]), 0)
        self.assertEqual(self.store.record("d:1")["status"], "released")

    def test_the_selection_launcher_carries_a_quarantine_its_globs_ignore(self) -> None:
        from dalton_core.discovery_selection_launcher import DiscoverySelectionLauncher

        config = self.state / "model.json"
        config.write_text(json.dumps({"capacity_retry": {}}))
        launcher = DiscoverySelectionLauncher(state_dir=self.state, model_config_path=config,
                                              scheduler_db=self.state / "scheduler.sqlite")
        launcher.quarantine.quarantine(discovery_ref="d:1", reason="x", error_type="E",
                                       rules_version="r")
        self.assertEqual(launcher.quarantine.directory,
                         self.state.resolve() / "discovery-selections" / "quarantine")
        self.assertIsNone(launcher.latest("d:1"))
        self.assertEqual(launcher.successful_selections(), {})


def _lane(states: dict[str, dict]) -> dict:
    return {"status": "idle", "source_ref": "source:alphaengine",
            "discovery": {"status": "idle"}, "acquisitions_launched": 0,
            "coordinators": {key: states.get(key, {"status": "idle", "deferred_streak": 0})
                             for key in KEYS}}


class CoordinatorStateTests(unittest.TestCase):
    def test_a_selection_hold_is_in_the_state_of_an_idle_coordinator(self) -> None:
        state = discovery_coordinator_state({
            "status": "idle", "discovery": {"status": "idle"}, "acquisitions_launched": 0,
            "acquisition": {"status": "selection_pending", "reason": PENDING_REASON,
                            "discovery_ref": "d:1"},
            "selection_quarantined": 0, "settled_dispatches": []})
        self.assertEqual(state["status"], "idle")
        self.assertEqual(state["acquisition_status"], "selection_pending")
        self.assertEqual(state["acquisition_reason"], PENDING_REASON)
        self.assertNotIn("selection_quarantined", state)

    def test_quarantines_and_failed_searches_are_counted(self) -> None:
        state = discovery_coordinator_state({
            "status": "idle", "discovery": {"status": "idle"},
            "acquisition": {"status": "idle"}, "selection_quarantined": 2,
            "settled_dispatches": [
                {"status": "failed", "reason": LEASE_REASON},
                {"status": "succeeded"}]})
        self.assertNotIn("acquisition_status", state)
        self.assertEqual(state["selection_quarantined"], 2)
        self.assertEqual(state["discovery_failures"], 1)
        self.assertEqual(state["discovery_failure_reason"], LEASE_REASON)

    def test_a_healthy_tick_says_nothing_new(self) -> None:
        state = discovery_coordinator_state({
            "status": "launched", "discovery": {"status": "launched"},
            "acquisition": {"status": "launched"}, "acquisitions_launched": 1,
            "settled_dispatches": [{"status": "succeeded"}]})
        self.assertEqual(state, {"status": "launched", "acquisitions_launched": 1})


class LedgerAndParityTests(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.state = Path(temp.name)

    def test_a_held_coordinator_is_kept_and_the_lane_is_not_idle(self) -> None:
        held = _lane({"alphaengine": {"status": "idle", "acquisition_status": "selection_pending",
                                      "acquisition_reason": PENDING_REASON}})
        self.assertEqual(blocked_coordinators(held), ["alphaengine"])
        counts = bounded_counts(held)
        self.assertEqual(counts["coordinators"]["alphaengine"]["acquisition_status"],
                         "selection_pending")
        failing = _lane({"sec_filings_index": {"status": "idle", "discovery_failures": 1,
                                               "discovery_failure_reason": LEASE_REASON}})
        self.assertEqual(blocked_coordinators(failing), ["sec_filings_index"])
        with TickLedger(self.state / "tick-ledger.sqlite") as ledger:
            for minute, lane in enumerate((held, failing, _lane({}))):
                at = datetime(2026, 9, 29, 0, minute * 5, tzinfo=timezone.utc)
                # ended_at pins the ledger day; without it the day is today's and the
                # fixed 09-29 window below finds nothing once the real date moves on.
                ledger.append_tick({"status": "ok", "mission_source_discovery": lane},
                                   started_at=at, ended_at=at)
            ticks = ledger.ticks(None, now=datetime(2026, 9, 29, 1, tzinfo=timezone.utc))["ticks"]
        self.assertEqual([tick["lanes"][0]["idle"] for tick in ticks], [False, False, True])

    def _selection_rows(self, held: list[dict]) -> dict[str, dict]:
        from dalton_core.workspace_health_parity import Environment, _discovery_coordinator_rows

        with TickLedger(self.state / "tick-ledger.sqlite") as ledger:
            start = datetime(2026, 9, 29, tzinfo=timezone.utc)
            for index, alpha in enumerate(held):
                ledger.append_tick({"status": "ok",
                                    "mission_source_discovery": _lane({"alphaengine": alpha})},
                                   started_at=start + timedelta(minutes=5 * index))
        env = Environment("test", self.state)
        self.addCleanup(env.close)
        return {row["check"]: row for row in _discovery_coordinator_rows(env)}

    def test_parity_warns_when_selection_holds_the_queue_three_ticks_running(self) -> None:
        pending = {"status": "idle", "acquisition_status": "selection_pending",
                   "acquisition_reason": PENDING_REASON}
        rows = self._selection_rows([{"status": "idle"}, pending, pending, pending])
        row = rows["mission_source_discovery.alphaengine.selection"]
        self.assertEqual((row["status"], row["selection_held_ticks"]), ("warn", 3))
        self.assertIn(PENDING_REASON, row["detail"])
        self.assertIn("discovery_selection_quarantine", row["fix"])
        self.assertNotIn("mission_source_discovery.sec_filings_index.selection", rows)

    def test_parity_warns_on_a_standing_quarantine_and_not_on_a_short_hold(self) -> None:
        quarantined = {"status": "launched", "selection_quarantined": 1}
        rows = self._selection_rows([quarantined] * 3)
        self.assertEqual(rows["mission_source_discovery.alphaengine.selection"]["status"], "warn")
        self.assertIn("1 discovery(ies) quarantined",
                      rows["mission_source_discovery.alphaengine.selection"]["detail"])

    def test_parity_does_not_warn_below_the_threshold(self) -> None:
        pending = {"status": "idle", "acquisition_status": "selection_pending"}
        rows = self._selection_rows([pending, {"status": "idle"}, pending])
        self.assertEqual(rows["mission_source_discovery.alphaengine.selection"]["status"], "ok")

    def _core(self, statuses: list[tuple[str, str, str | None]]) -> None:
        connection = sqlite3.connect(self.state / "core.sqlite")
        connection.execute(
            "CREATE TABLE coverage_mission_discovery_dispatches (dispatch_id TEXT, source_ref TEXT, "
            "status TEXT, failure_reason TEXT, created_at TEXT, updated_at TEXT)")
        start = datetime(2026, 9, 25, tzinfo=timezone.utc)
        for index, (source_ref, status, reason) in enumerate(statuses):
            at = (start + timedelta(hours=index)).isoformat()
            connection.execute(
                "INSERT INTO coverage_mission_discovery_dispatches VALUES (?,?,?,?,?,?)",
                (f"d:{index}", source_ref, status, reason, at, at))
        connection.commit()
        connection.close()

    def test_parity_warns_when_a_sources_searches_keep_failing(self) -> None:
        from dalton_core.workspace_health_parity import Environment, _discovery_search_failure_rows

        self._core([("source:sec-edgar", "succeeded", None)]
                   + [("source:sec-edgar", "failed", LEASE_REASON)] * 5
                   + [("source:alphaengine", "failed", "provider timeout"),
                      ("source:alphaengine", "succeeded", None),
                      ("source:web-search", "running", None)])
        env = Environment("test", self.state)
        self.addCleanup(env.close)
        rows = {row["check"]: row for row in _discovery_search_failure_rows(env)}
        sec = rows["mission_source_discovery.sec_filings_index.searches"]
        self.assertEqual((sec["status"], sec["failed_searches"]), ("warn", 5))
        self.assertIn(LEASE_REASON, sec["detail"])
        self.assertIn("check_capability_approval_receipts", sec["fix"])
        alpha = rows["mission_source_discovery.alphaengine.searches"]
        self.assertEqual((alpha["status"], alpha["failed_searches"]), ("ok", 0))
        # A source with no settled search yet has nothing to report.
        self.assertNotIn("mission_source_discovery.web_search.searches", rows)


if __name__ == "__main__":
    unittest.main()
