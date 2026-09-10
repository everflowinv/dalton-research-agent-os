"""F14 Batch A: durable failure state without turning healthy silence into failure."""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from dalton_core.lane_failure_ledger import LaneFailureLedger, default_path
from dalton_core.mission_deep_insight_lane import MissionDeepInsightLaneCoordinator
from dalton_core.mission_dossier_lane import MissionDossierLaneCoordinator
from dalton_core.mission_industry_framework_lane import MissionIndustryFrameworkLaneCoordinator

NOW = datetime(2026, 9, 10, tzinfo=timezone.utc)


class Launcher:
    def __init__(self, state_dir: Path, summary_key: str, quiet: str, success: str):
        self.state_dir = state_dir
        self.summary_key = summary_key
        self.quiet = quiet
        self.success = success
        self.started = []
        self.tickets = {}

    def start(self, *, signature, **kwargs):
        ticket = {"id": f"ticket:{len(self.started) + 1}", "signature": signature}
        self.started.append(ticket)
        self.tickets[ticket["id"]] = {**ticket, "status": "running"}
        return ticket

    def status(self, ticket_ref):
        return self.tickets[ticket_ref]

    def settle(self, ticket_ref, *, status="failed", reason=None, outcome=None):
        summary = {self.summary_key: outcome or self.success}
        if reason:
            summary["failure_reason"] = reason
        self.tickets[ticket_ref] = {
            **self.tickets[ticket_ref], "status": status, "summary": summary,
        }


class WholeLedgerFailureTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state = Path(self.temp.name)

    def connection(self, kind):
        db = sqlite3.connect(":memory:")
        db.row_factory = sqlite3.Row
        db.execute("CREATE TABLE claim_versions (claim_version_id TEXT, created_at TEXT)")
        if kind == "deep":
            db.execute("CREATE TABLE company_dossier_versions "
                       "(dossier_ref TEXT, version_number INTEGER)")
        if kind == "industry":
            db.execute("CREATE TABLE statement_ingest_lines (id INTEGER)")
        self.addCleanup(db.close)
        return db

    def cases(self):
        return (
            ("dossier", MissionDossierLaneCoordinator, "dossier_status",
             "nothing_new", "published"),
            ("deep", MissionDeepInsightLaneCoordinator, "gate_status",
             "no_eligible_company", "submitted"),
            ("industry", MissionIndustryFrameworkLaneCoordinator, "framework_status",
             "nothing_new", "published"),
        )

    def coordinator(self, kind, cls, launcher, connection):
        kwargs = dict(connection=connection, launcher=launcher,
                      failure_ledger_dir=self.state, failure_clock=lambda: NOW)
        if kind == "industry":
            kwargs.update(clock=lambda: 0.0, min_interval_seconds=0)
        return cls(**kwargs)

    def test_dependency_parks_without_attempts_survives_restart_and_probe_clears(self):
        for kind, cls, summary_key, quiet, success in self.cases():
            with self.subTest(kind=kind):
                connection = self.connection(kind)
                launcher = Launcher(self.state, summary_key, quiet, success)
                first = self.coordinator(kind, cls, launcher, connection)
                launched = first.dispatch_once()
                launcher.settle(launched["ticket_ref"],
                                reason="model_unavailable: provider is down")
                first._settle_open()
                self.assertEqual(first.budget.attempts(launched["signature"]), 0)

                restarted = self.coordinator(kind, cls, launcher, connection)
                self.assertEqual(len(restarted.budget.parked_items()), 1)
                probe = restarted.dispatch_once()
                self.assertEqual(probe["status"], "launched")
                launcher.settle(probe["ticket_ref"], status="succeeded", outcome=success)
                restarted._settle_open()
                self.assertEqual(restarted.budget.parked_items(), [])

    def test_content_refusal_is_terminal_only_for_the_current_signature(self):
        for kind, cls, summary_key, quiet, success in self.cases():
            with self.subTest(kind=kind):
                connection = self.connection(kind)
                launcher = Launcher(self.state, summary_key, quiet, success)
                coordinator = self.coordinator(kind, cls, launcher, connection)
                launched = coordinator.dispatch_once()
                launcher.settle(launched["ticket_ref"], reason="content_refused: bad bytes")
                coordinator._settle_open()
                self.assertEqual(coordinator.dispatch_once()["status"], "terminal")
                if kind == "deep":
                    connection.execute(
                        "INSERT INTO company_dossier_versions VALUES ('dossier:new', 1)")
                else:
                    connection.execute(
                        "INSERT INTO claim_versions VALUES ('new', '2026-09-10')")
                self.assertEqual(coordinator.dispatch_once()["status"], "launched")

    def test_quiet_result_writes_no_failure_event(self):
        for kind, cls, summary_key, quiet, success in self.cases():
            with self.subTest(kind=kind):
                connection = self.connection(kind)
                launcher = Launcher(self.state, summary_key, quiet, success)
                coordinator = self.coordinator(kind, cls, launcher, connection)
                launched = coordinator.dispatch_once()
                launcher.settle(launched["ticket_ref"], status="succeeded", outcome=quiet)
                self.assertEqual(coordinator.dispatch_once()["status"], "idle")
        if default_path(self.state).is_file():
            with LaneFailureLedger(default_path(self.state), read_only=True) as ledger:
                self.assertEqual(ledger.events(), [])

    def test_quiet_dependency_probe_clears_the_persisted_ops_item(self):
        for kind, cls, summary_key, quiet, success in self.cases():
            with self.subTest(kind=kind):
                connection = self.connection(kind)
                lane_state = self.state / kind
                lane_state.mkdir()
                launcher = Launcher(lane_state, summary_key, quiet, success)
                kwargs = dict(connection=connection, launcher=launcher,
                              failure_ledger_dir=lane_state,
                              failure_clock=lambda: NOW)
                if kind == "industry":
                    kwargs.update(clock=lambda: 0.0, min_interval_seconds=0)
                coordinator = cls(**kwargs)
                first = coordinator.dispatch_once()
                launcher.settle(first["ticket_ref"],
                                reason="model_unavailable: provider is down")
                coordinator._settle_open()
                probe = coordinator.dispatch_once()
                launcher.settle(probe["ticket_ref"], status="succeeded", outcome=quiet)
                settled = coordinator._settle_open()
                self.assertEqual(settled["resumed"], [first["signature"]])
                self.assertEqual(coordinator.budget.parked_items(), [])

    def test_policy_pointer_change_releases_persisted_permission_hold(self):
        connection = self.connection("dossier")
        connection.execute("CREATE TABLE coverage_mission_pointer "
                           "(mission_ref TEXT, mission_version_id TEXT)")
        connection.execute("INSERT INTO coverage_mission_pointer VALUES ('m', 'mv1')")
        connection.execute("CREATE TABLE governance_policy_pointer "
                           "(pointer_id INTEGER, policy_version_id TEXT)")
        connection.execute("INSERT INTO governance_policy_pointer VALUES (1, 'policy:v1')")
        launcher = Launcher(self.state, "dossier_status", "nothing_new", "published")
        first = self.coordinator(
            "dossier", MissionDossierLaneCoordinator, launcher, connection)
        launched = first.dispatch_once()
        launcher.settle(launched["ticket_ref"], status="succeeded",
                        outcome="not_authorized")
        first._settle_open()

        restarted = self.coordinator(
            "dossier", MissionDossierLaneCoordinator, launcher, connection)
        self.assertEqual(len(restarted.budget.permission_items()), 1)
        self.assertEqual(restarted.dispatch_once()["status"], "not_permitted")

        connection.execute(
            "UPDATE governance_policy_pointer SET policy_version_id='policy:v2'")
        retry = restarted.dispatch_once()
        self.assertEqual(retry["status"], "launched")
        launcher.settle(retry["ticket_ref"], status="succeeded", outcome="published")
        restarted._settle_open()
        self.assertEqual(restarted.budget.permission_items(), [])
        with LaneFailureLedger(default_path(self.state), read_only=True) as ledger:
            self.assertEqual(ledger.parked_by_dependency(now=NOW)["permission_count"], 0)

    def test_dossier_mission_change_before_settlement_does_not_poison_new_grant(self):
        from dalton_core.company_dossier_launcher import run_digest
        connection = self.connection("dossier")
        connection.execute("CREATE TABLE coverage_mission_pointer "
                           "(mission_ref TEXT, mission_version_id TEXT)")
        connection.execute("INSERT INTO coverage_mission_pointer VALUES ('m', 'mv1')")
        launcher = Launcher(self.state, "dossier_status", "nothing_new", "published")
        lane = self.coordinator("dossier", MissionDossierLaneCoordinator,
                                launcher, connection)
        old = lane.dispatch_once()
        connection.execute("UPDATE coverage_mission_pointer SET mission_version_id='mv2'")
        launcher.settle(old["ticket_ref"], status="succeeded", outcome="not_authorized")
        retry = lane.dispatch_once()
        self.assertEqual(retry["status"], "launched")
        self.assertNotEqual(old["signature"], retry["signature"])
        self.assertNotEqual(run_digest(None, old["signature"]),
                            run_digest(None, retry["signature"]))
        self.assertEqual(lane.budget.permission_items(), [])
        launcher.settle(retry["ticket_ref"], status="succeeded", outcome="not_authorized")
        self.assertEqual(lane.dispatch_once()["status"], "not_permitted")
        self.assertEqual(lane.budget.permission_items()[0]["item_key"], retry["signature"])

    def test_dossier_old_unbound_permission_is_retired_after_upgrade(self):
        from dalton_core.mission_dossier_lane import ledger_signature, permission_key
        connection = self.connection("dossier")
        launcher = Launcher(self.state, "dossier_status", "nothing_new", "published")
        lane = self.coordinator("dossier", MissionDossierLaneCoordinator,
                                launcher, connection)
        old_key = permission_key(connection, launcher, ledger_signature(connection)).replace(
            "|permission:v2:", "|permission:")
        lane.budget.record(old_key, status="gated:not permitted",
                           reason="gated:mission does not grant dossier")
        self.assertEqual(lane.dispatch_once()["status"], "launched")
        self.assertEqual(lane.budget.permission_items(), [])
        with LaneFailureLedger(default_path(self.state), read_only=True) as ledger:
            events = ledger.events()
        self.assertTrue(any(row["item_key"] == old_key for row in events))

    def test_new_business_signature_retires_old_permission_projection(self):
        for kind, cls, summary_key, quiet, success in self.cases():
            with self.subTest(kind=kind):
                connection = self.connection(kind)
                lane_state = self.state / f"permission-{kind}"
                lane_state.mkdir()
                launcher = Launcher(lane_state, summary_key, quiet, success)
                kwargs = dict(connection=connection, launcher=launcher,
                              failure_ledger_dir=lane_state,
                              failure_clock=lambda: NOW)
                if kind == "industry":
                    kwargs.update(clock=lambda: 0.0, min_interval_seconds=0)
                coordinator = cls(**kwargs)
                launched = coordinator.dispatch_once()
                denied = ("no_checkpoint" if kind == "deep"
                          else "not_authorized")
                launcher.settle(
                    launched["ticket_ref"], status="succeeded", outcome=denied)
                coordinator._settle_open()
                self.assertEqual(len(coordinator.budget.permission_items()), 1)
                if kind == "deep":
                    connection.execute(
                        "INSERT INTO company_dossier_versions VALUES ('d:new', 1)")
                else:
                    connection.execute(
                        "INSERT INTO claim_versions VALUES ('new', '2026-09-10')")
                self.assertEqual(coordinator.dispatch_once()["status"], "launched")
                self.assertEqual(coordinator.budget.permission_items(), [])


if __name__ == "__main__":
    unittest.main()
