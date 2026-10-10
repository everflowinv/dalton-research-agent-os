"""P12e: the lane registers, watches filings, and stays quiet the rest of the week.

The signature test is the one that matters. Every other cognition lane watches
Claims; this deliverable's central table is computed from filed statements, so
a lane whose signature only counted Claims would have gone silent through every
earnings season -- which is the only time this framework has anything new to
say while the industry Claim path is still on another branch.
"""

from __future__ import annotations

import sqlite3
import unittest
from pathlib import Path

from dalton_core.industry_framework_launcher import (
    IndustryFrameworkLauncher,
    run_digest,
)
from dalton_core.lane_registry import (
    LaunchAgentContext,
    lane_argv,
    lane_for_operation,
    lane_init_kwargs,
    lane_operation_fields,
    registered_lanes,
)
from dalton_core.mission_industry_framework_lane import (
    FRAMEWORK_MODEL_CONFIG,
    FRAMEWORK_POLICY,
    FRAMEWORK_VERIFIER_MODEL_CONFIG,
    LEGACY_FRAMEWORK_VERIFIER_MODEL_CONFIG,
    LAUNCHER_KWARG,
    MIN_INTERVAL_SECONDS,
    QUIET_STATUSES,
    MissionIndustryFrameworkLaneCoordinator,
    argv_fragment,
    build_launcher,
    dispatch,
    ledger_signature,
)

OPERATION = "dispatch_industry_framework"


class RegistrationTests(unittest.TestCase):
    def test_the_lane_is_registered_at_its_declared_order(self):
        spec = lane_for_operation(OPERATION)
        self.assertIsNotNone(spec)
        self.assertEqual(spec.order, 139)
        self.assertEqual(spec.driver_key, "industry_framework")
        self.assertEqual(spec.init_kwarg, LAUNCHER_KWARG)

    def test_it_runs_after_the_dossier_it_reads(self):
        order = {spec.driver_key: spec.order for spec in registered_lanes()}
        self.assertLess(order["company_dossier"], order["industry_framework"])

    def test_the_registry_carries_it_into_the_three_derived_places(self):
        self.assertIn(OPERATION, lane_operation_fields())
        self.assertIn(LAUNCHER_KWARG, lane_init_kwargs())
        self.assertIn(OPERATION, [spec.operation for spec in registered_lanes()])

    def test_the_lane_has_a_name_in_the_owners_words(self):
        from dalton_core.cockpit_plane import REGISTRY_LANE_LABELS

        self.assertTrue(REGISTRY_LANE_LABELS.get("industry_framework"))


class ArgvTests(unittest.TestCase):
    def context(self, *names):
        import tempfile

        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        state = Path(directory.name)
        for name in names:
            (state / name).write_text("{}", encoding="utf-8")
        return LaunchAgentContext(state=state)

    def test_no_policy_means_no_lane(self):
        self.assertEqual(argv_fragment(self.context()), [])

    def test_the_policy_alone_turns_the_lane_on(self):
        # This lane's product is half deterministic: with the policy and no
        # model it still computes and reports the comparison table, which is
        # the state a Core is in before the deliverable scope is granted.
        argv = argv_fragment(self.context(FRAMEWORK_POLICY))
        self.assertIn("--industry-framework-policy", argv)
        self.assertNotIn("--industry-framework-model-config", argv)

    def test_the_model_and_verifier_are_added_when_present(self):
        argv = argv_fragment(self.context(
            FRAMEWORK_POLICY, FRAMEWORK_MODEL_CONFIG, FRAMEWORK_VERIFIER_MODEL_CONFIG))
        self.assertIn("--industry-framework-model-config", argv)
        self.assertIn("--industry-framework-verifier-model-config", argv)

    def test_the_paired_dossier_verifier_precedes_the_legacy_fallback(self):
        context = self.context(FRAMEWORK_POLICY, FRAMEWORK_VERIFIER_MODEL_CONFIG,
                               LEGACY_FRAMEWORK_VERIFIER_MODEL_CONFIG)
        argv = argv_fragment(context)
        selected = argv[argv.index("--industry-framework-verifier-model-config") + 1]
        self.assertEqual(Path(selected).name, FRAMEWORK_VERIFIER_MODEL_CONFIG)

    def test_the_legacy_verifier_remains_a_fallback(self):
        argv = argv_fragment(self.context(FRAMEWORK_POLICY,
                                          LEGACY_FRAMEWORK_VERIFIER_MODEL_CONFIG))
        selected = argv[argv.index("--industry-framework-verifier-model-config") + 1]
        self.assertEqual(Path(selected).name, LEGACY_FRAMEWORK_VERIFIER_MODEL_CONFIG)

    def test_the_fragment_reaches_the_launchagent_argv(self):
        argv = lane_argv(self.context(FRAMEWORK_POLICY))
        self.assertIn("--industry-framework-policy", argv)

    def test_no_policy_means_no_launcher(self):
        class Args:
            industry_framework_policy = None

        self.assertIsNone(build_launcher(Args()))


class SignatureTests(unittest.TestCase):
    def connection(self):
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        connection.execute(
            "CREATE TABLE claim_versions (claim_version_id TEXT, created_at TEXT)")
        connection.execute("CREATE TABLE statement_ingest_lines (id INTEGER)")
        self.addCleanup(connection.close)
        return connection

    def test_a_newly_filed_statement_line_moves_the_signature(self):
        connection = self.connection()
        before = ledger_signature(connection)
        connection.execute("INSERT INTO statement_ingest_lines VALUES (1)")
        self.assertNotEqual(before, ledger_signature(connection))

    def test_a_new_claim_moves_the_signature_too(self):
        connection = self.connection()
        before = ledger_signature(connection)
        connection.execute("INSERT INTO claim_versions VALUES ('c1', '2026-09-10')")
        self.assertNotEqual(before, ledger_signature(connection))

    def test_an_absent_authority_is_a_valid_state_rather_than_a_crash(self):
        # No dossier table, no debate table, no framework table: a Core that
        # has not opened them yet still gets a signature.
        self.assertEqual(len(ledger_signature(self.connection())), 32)

    def test_the_signature_is_stable_when_nothing_moved(self):
        connection = self.connection()
        self.assertEqual(ledger_signature(connection), ledger_signature(connection))


class FakeLauncher:
    def __init__(self, tickets=None):
        self.tickets = dict(tickets or {})
        self.started = []
        self.next_id = 1

    def start(self, *, signature, industry_ref=None):
        ticket = {"id": f"ticket:{self.next_id}", "signature": signature}
        self.next_id += 1
        self.started.append(signature)
        self.tickets[ticket["id"]] = {"status": "running", "signature": signature}
        return ticket

    def status(self, ticket_ref):
        return self.tickets[ticket_ref]

    def settle(self, ticket_ref, *, status="succeeded", summary=None):
        self.tickets[ticket_ref] = {
            "status": status, "signature": self.tickets[ticket_ref]["signature"],
            "summary": summary or {},
        }


class CoordinatorTests(unittest.TestCase):
    def connection(self):
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        connection.execute(
            "CREATE TABLE claim_versions (claim_version_id TEXT, created_at TEXT)")
        connection.execute("CREATE TABLE statement_ingest_lines (id INTEGER)")
        self.addCleanup(connection.close)
        return connection

    def coordinator(self, launcher, *, now=None):
        clock = now or (lambda: 0.0)
        return MissionIndustryFrameworkLaneCoordinator(
            connection=self.connection(), launcher=launcher, clock=clock)

    def test_the_first_tick_launches_and_the_second_reports_running(self):
        launcher = FakeLauncher()
        coordinator = self.coordinator(launcher)
        first = coordinator.dispatch_once()
        self.assertEqual(first["status"], "launched")
        self.assertEqual(coordinator.dispatch_once()["status"], "running")

    def test_an_unchanged_signature_after_a_quiet_run_stays_idle(self):
        launcher = FakeLauncher()
        coordinator = self.coordinator(launcher)
        launched = coordinator.dispatch_once()
        launcher.settle(launched["ticket_ref"],
                        summary={"framework_status": "nothing_new"})
        result = coordinator.dispatch_once()
        self.assertEqual(result["status"], "idle")
        # 2026-10-07: with no framework version yet the idle reason names
        # what the run was missing instead of "nothing has moved".
        self.assertIn("还没有第一个版本", result["reason"])
        self.assertEqual(result["missing_input"], "还没有可写进框架的新证据")
        self.assertEqual(len(launcher.started), 1)

    def test_after_a_published_version_the_weekly_cadence_applies(self):
        launcher = FakeLauncher()
        coordinator = self.coordinator(launcher, now=lambda: 0.0)
        launched = coordinator.dispatch_once()
        launcher.settle(launched["ticket_ref"],
                        summary={"framework_status": "published", "version_ref": "v:1"})
        coordinator.dispatch_once()
        coordinator._quiet_signature = None
        coordinator.connection.execute("INSERT INTO statement_ingest_lines VALUES (7)")
        waiting = coordinator.dispatch_once()
        self.assertEqual(waiting["status"], "waiting")
        self.assertIn("weekly deliverable", waiting["reason"])

    def test_a_first_run_that_published_nothing_retries_soon_and_names_the_input(self):
        # ws-7d 2026-10-07: the first run crashed after the verifier passed,
        # and the lane then waited a whole week from that launch.
        from dalton_core.mission_industry_framework_lane import (
            FIRST_DRAFT_RETRY_SECONDS,
        )

        launcher = FakeLauncher()
        clock = {"now": 0.0}
        coordinator = self.coordinator(launcher, now=lambda: clock["now"])
        launched = coordinator.dispatch_once()
        launcher.settle(launched["ticket_ref"], status="succeeded",
                        summary={"framework_status": "verification_failed"})
        coordinator.connection.execute("INSERT INTO statement_ingest_lines VALUES (8)")
        clock["now"] = 60.0
        waiting = coordinator.dispatch_once()
        self.assertEqual(waiting["status"], "waiting")
        self.assertNotIn("weekly", waiting["reason"])
        self.assertIn("独立核验没有通过", waiting["reason"])
        self.assertEqual(waiting["missing_input"], "独立核验没有通过")
        clock["now"] = FIRST_DRAFT_RETRY_SECONDS + 1
        self.assertEqual(coordinator.dispatch_once()["status"], "launched")
        self.assertEqual(len(launcher.started), 2)

    def test_a_restart_does_not_reset_the_week_once_a_version_exists(self):
        import time as _time
        from datetime import datetime, timezone

        connection = self.connection()
        connection.execute(
            "CREATE TABLE IF NOT EXISTS industry_framework_versions "
            "(framework_ref TEXT, version_number INTEGER, created_at TEXT)")
        connection.execute(
            "INSERT INTO industry_framework_versions VALUES ('f', 1, ?)",
            (datetime.now(timezone.utc).isoformat(),))
        launcher = FakeLauncher()
        coordinator = MissionIndustryFrameworkLaneCoordinator(
            connection=connection, launcher=launcher, clock=_time.time)
        waiting = coordinator.dispatch_once()
        self.assertEqual(waiting["status"], "waiting")
        self.assertIn("weekly deliverable", waiting["reason"])
        self.assertEqual(launcher.started, [])

    def test_every_quiet_status_is_a_reason_to_stay_quiet(self):
        for status in QUIET_STATUSES:
            with self.subTest(status=status):
                launcher = FakeLauncher()
                coordinator = self.coordinator(launcher)
                launched = coordinator.dispatch_once()
                launcher.settle(launched["ticket_ref"],
                                summary={"framework_status": status})
                self.assertEqual(coordinator.dispatch_once()["status"], "idle")

    def test_a_failed_child_with_a_declared_quiet_outcome_is_not_a_failure(self):
        launcher = FakeLauncher()
        coordinator = self.coordinator(launcher)
        launched = coordinator.dispatch_once()
        launcher.settle(launched["ticket_ref"], status="failed",
                        summary={"framework_status": "no_driver_pack"})
        self.assertEqual(coordinator.dispatch_once()["status"], "idle")
        self.assertEqual(coordinator.budget.summary()["terminal"], 0)
        self.assertEqual(coordinator.budget.summary()["held"], 0)

    def test_three_transient_failures_hold_that_signature_with_its_reason(self):
        launcher = FakeLauncher()
        coordinator = MissionIndustryFrameworkLaneCoordinator(
            connection=self.connection(), launcher=launcher,
            clock=lambda: 0.0, min_interval_seconds=0)
        launched = coordinator.dispatch_once()
        for _ in range(3):
            launcher.settle(launched["ticket_ref"], status="failed",
                            summary={"failure_reason": "the policy could not be read"})
            result = coordinator.dispatch_once()
            if result["status"] == "launched":
                launched = result
        self.assertEqual(result["status"], "held")
        self.assertIn("policy could not be read", result["reason"])

    def test_the_weekly_interval_holds_a_second_launch_and_says_so(self):
        launcher = FakeLauncher()
        clock = {"now": 0.0}
        coordinator = self.coordinator(launcher, now=lambda: clock["now"])
        launched = coordinator.dispatch_once()
        launcher.settle(launched["ticket_ref"],
                        summary={"framework_status": "published"})
        # A filing lands, so the signature moves and there is something to do.
        coordinator.connection.execute("INSERT INTO statement_ingest_lines VALUES (1)")
        clock["now"] = 3600.0
        waiting = coordinator.dispatch_once()
        self.assertEqual(waiting["status"], "waiting")
        self.assertIn("weekly deliverable", waiting["reason"])
        self.assertEqual(len(launcher.started), 1)

    def test_after_the_interval_a_moved_signature_launches_again(self):
        launcher = FakeLauncher()
        clock = {"now": 0.0}
        coordinator = self.coordinator(launcher, now=lambda: clock["now"])
        launched = coordinator.dispatch_once()
        launcher.settle(launched["ticket_ref"],
                        summary={"framework_status": "published"})
        coordinator.connection.execute("INSERT INTO statement_ingest_lines VALUES (1)")
        clock["now"] = MIN_INTERVAL_SECONDS + 1
        self.assertEqual(coordinator.dispatch_once()["status"], "launched")
        self.assertEqual(len(launcher.started), 2)

    def test_the_settled_summary_carries_the_table_and_the_open_gaps(self):
        launcher = FakeLauncher()
        coordinator = self.coordinator(launcher)
        launched = coordinator.dispatch_once()
        launcher.settle(launched["ticket_ref"], summary={
            "framework_status": "published", "version_ref": "v:1",
            "open_gaps": ["gap:tam-and-share"],
            "comparison": {"status": "computed", "computed_cells": 12},
        })
        settled = coordinator.dispatch_once()["settled"]
        self.assertEqual(settled["open_gaps"], ["gap:tam-and-share"])
        self.assertEqual(settled["comparison"]["computed_cells"], 12)


class DispatchTests(unittest.TestCase):
    def test_a_writer_without_the_lane_reports_unconfigured(self):
        class Server:
            lane_state: dict = {}

            def lane_launcher(self, name):
                return None

        result = dispatch(Server(), {})
        self.assertEqual(result["status"], "unconfigured")
        self.assertIn("no industry-framework lane", result["reason"])

    def test_the_coordinator_is_created_once_and_kept(self):
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        connection.execute(
            "CREATE TABLE claim_versions (claim_version_id TEXT, created_at TEXT)")
        self.addCleanup(connection.close)
        launcher = FakeLauncher()

        class Store:
            pass

        store = Store()
        store.connection = connection

        class Server:
            def __init__(self):
                self.lane_state = {}
                self.store = store

            def lane_launcher(self, name):
                return launcher

        server = Server()
        dispatch(server, {})
        held = server.lane_state[LAUNCHER_KWARG]
        dispatch(server, {})
        self.assertIs(server.lane_state[LAUNCHER_KWARG], held)


class LauncherTests(unittest.TestCase):
    def launcher(self, **kwargs):
        import tempfile

        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        return IndustryFrameworkLauncher(state_dir=Path(directory.name), **kwargs)

    def test_the_run_digest_is_the_signature_so_a_double_tick_is_one_ticket(self):
        self.assertEqual(run_digest(None, "sig"), run_digest(None, "sig"))
        self.assertNotEqual(run_digest(None, "sig"), run_digest(None, "other"))

    def test_a_run_needs_a_signature(self):
        from dalton_core.lane_child_launcher import LaneChildRejected

        with self.assertRaises(LaneChildRejected):
            self.launcher().start(signature="  ")

    def test_configured_is_about_the_model_and_not_the_policy(self):
        self.assertFalse(self.launcher(policy_path="/tmp/p.json").configured)
        self.assertTrue(self.launcher(model_config_path="/tmp/m.json").configured)

    def test_the_command_passes_every_wired_path_through(self):
        launcher = self.launcher(model_config_path="/tmp/m.json",
                                 verifier_model_config_path="/tmp/v.json",
                                 policy_path="/tmp/p.json", max_units=2)
        command = launcher._command(ticket_dir=Path("/tmp/t"))
        self.assertIn("dalton_core.industry_framework_cli", command)
        self.assertIn("--framework-policy", command)
        self.assertIn("--verifier-model-config", command)
        self.assertIn("--max-units", command)


class MaterialProbeCoordinatorTests(unittest.TestCase):
    @staticmethod
    def probe(fingerprint, *, urgent=False):
        return {"fingerprint": fingerprint, "urgent": urgent, "pending_count": 1,
                "next_review_at": "2026-10-24T00:00:00+00:00", "label": "必需材料出了新版本"}

    def coordinator(self, now):
        connection = CoordinatorTests.connection(self)
        launcher = FakeLauncher()
        coordinator = MissionIndustryFrameworkLaneCoordinator(
            connection=connection, launcher=launcher, clock=lambda: now[0])
        return coordinator, launcher

    def test_document_probe_wakes_quiet_even_without_new_claims(self):
        from unittest.mock import patch
        now = [0.0]
        coordinator, launcher = self.coordinator(now)
        with patch.object(coordinator, "_material_probe", return_value=self.probe("old")):
            first = coordinator.dispatch_once()
            launcher.settle(first["ticket_ref"], summary={"framework_status": "nothing_new"})
            self.assertEqual(coordinator.dispatch_once()["status"], "idle")
        now[0] = 7 * 24 * 3600.0
        with patch.object(coordinator, "_material_probe", return_value=self.probe("new", urgent=True)):
            final = coordinator.dispatch_once()
        self.assertEqual(final["status"], "launched")
        self.assertNotEqual(first["signature"], final["signature"])

    def test_due_edge_not_wall_clock_breaks_quiet_without_ledger_write(self):
        from unittest.mock import patch
        now = [13 * 24 * 3600.0]
        coordinator, launcher = self.coordinator(now)
        coordinator._published = True
        before = coordinator.connection.total_changes
        def probe(moment, **kwargs):
            return self.probe("due" if moment >= 14 * 24 * 3600 else "pending")
        with patch.object(coordinator, "_material_probe", side_effect=probe):
            first = coordinator.dispatch_once()
            launcher.settle(first["ticket_ref"], summary={"framework_status": "nothing_new"})
            now[0] += 3600
            self.assertEqual(coordinator.dispatch_once()["status"], "idle")
            now[0] = 14 * 24 * 3600.0
            waiting = coordinator.dispatch_once()
            self.assertEqual(waiting["status"], "waiting")  # ordinary stale still observes week
            self.assertNotEqual(first["signature"], waiting["signature"])
            now[0] = 20 * 24 * 3600.0
            self.assertEqual(coordinator.dispatch_once()["status"], "launched")
        self.assertEqual(coordinator.connection.total_changes, before)

    def test_only_proven_urgent_probe_bypasses_weekly_cadence(self):
        from unittest.mock import patch
        now = [100.0]
        coordinator, launcher = self.coordinator(now)
        coordinator._published = True
        coordinator._last_launch = 0.0
        with patch.object(coordinator, "_material_probe", return_value=self.probe("broker")):
            self.assertEqual(coordinator.dispatch_once()["status"], "waiting")
        with patch.object(coordinator, "_material_probe", return_value=self.probe("required", urgent=True)):
            result = coordinator.dispatch_once()
        self.assertEqual(result["status"], "launched")
        self.assertEqual(result["trigger"], "material_policy")
        self.assertEqual(len(launcher.started), 1)

    def test_urgent_probe_keeps_permission_and_failure_budget(self):
        from unittest.mock import patch
        now = [100.0]
        coordinator, launcher = self.coordinator(now)
        coordinator._published = True
        coordinator._last_launch = 0.0
        with patch.object(coordinator, "_material_probe", return_value=self.probe("required", urgent=True)):
            first = coordinator.dispatch_once()
            launcher.settle(first["ticket_ref"], summary={"framework_status": "not_authorized"})
            result = coordinator.dispatch_once()
        self.assertNotEqual(result["status"], "launched")
        self.assertEqual(len(launcher.started), 1)

    def test_urgent_probe_does_not_change_first_draft_retry(self):
        from unittest.mock import patch
        now = [100.0]
        coordinator, launcher = self.coordinator(now)
        coordinator._last_launch = 0.0
        with patch.object(coordinator, "_material_probe", return_value=self.probe("required", urgent=True)):
            result = coordinator.dispatch_once()
        self.assertEqual(result["status"], "waiting")
        self.assertEqual(launcher.started, [])

    def test_actual_probe_uses_verified_active_mission_and_utc(self):
        from datetime import timedelta
        from unittest.mock import patch
        from tests.test_dossier_lane import Harness
        harness = Harness()
        self.addCleanup(harness.close)
        launcher = FakeLauncher()
        launcher.state_dir = harness.state_dir
        coordinator = MissionIndustryFrameworkLaneCoordinator(
            connection=harness.store.connection, launcher=launcher)
        harness.store.connection.execute("PRAGMA query_only=ON")
        with patch("dalton_core.redraft_policy.lane_probe", return_value=self.probe("first"), create=True) as probe:
            coordinator._material_probe(100.0)
        args, kwargs = probe.call_args
        self.assertEqual(args, (harness.state_dir, harness.store.connection))
        self.assertEqual(kwargs["product"], "industry_framework")
        self.assertEqual(kwargs["target"], harness.mission["industry_ref"])
        self.assertEqual(kwargs["company_refs"], [member["company_ref"] for member in harness.mission["universe"]])
        self.assertIsNone(kwargs["prior"])
        self.assertEqual(kwargs["now"].timestamp(), 100.0)
        self.assertEqual(kwargs["now"].utcoffset(), timedelta(0))

    def test_heavy_probe_cache_rechecks_arrivals_core_changes_and_thirty_minute_deadline(self):
        from unittest.mock import patch
        from tests.test_dossier_lane import Harness
        harness = Harness()
        self.addCleanup(harness.close)
        launcher = FakeLauncher()
        launcher.state_dir = harness.state_dir
        coordinator = MissionIndustryFrameworkLaneCoordinator(
            connection=harness.store.connection, launcher=launcher)
        light = ["old"]
        heavy = []
        def probe(*args, **kwargs):
            if kwargs.get("light_only"):
                return {"fingerprint": light[0]}
            heavy.append(kwargs["now"])
            return self.probe("policy:" + light[0])
        with patch("dalton_core.redraft_policy.lane_probe", side_effect=probe):
            coordinator._material_probe(100.0, source_signature="core-old")
            coordinator._material_probe(101.0, source_signature="core-old")
            self.assertEqual(len(heavy), 1)
            coordinator._material_probe(1900.0, source_signature="core-old")
            self.assertEqual(len(heavy), 2)
            coordinator._material_probe(1901.0, source_signature="core-new")
            self.assertEqual(len(heavy), 3)
            light[0] = "new"
            coordinator._material_probe(1902.0, source_signature="core-new")
            self.assertEqual(len(heavy), 4)

    def test_real_framework_due_edge_reaches_inner_gate_and_still_waits_week(self):
        from datetime import datetime, timedelta, timezone
        from dalton_core.industry_framework import IndustryFrameworkAuthority
        from tests.test_dossier_lane import Harness
        from tests.test_industry_framework import record, simple_comparison
        from tests.test_material_pack import Corpus
        harness = Harness()
        self.addCleanup(harness.close)
        drafted = datetime.now(timezone.utc)
        body = record(comparison=simple_comparison())
        body["drafted_at"] = {"characteristics": drafted.isoformat()}
        prior = IndustryFrameworkAuthority(harness.store).publish(body)
        arrival = drafted + timedelta(minutes=1)
        company = harness.mission["universe"][0]["company_ref"]
        Corpus(harness.state_dir).add(
            "sales_note", "Synthetic company operating check", "Synthetic sales discussion; no new claim.",
            date=arrival.date().isoformat(), company=company,
            now=arrival.isoformat(), acquired=arrival.isoformat())
        now = [(drafted + timedelta(days=13, hours=23)).timestamp()]
        first_moment = now[0]
        launcher = FakeLauncher()
        launcher.state_dir = harness.state_dir
        coordinator = MissionIndustryFrameworkLaneCoordinator(
            connection=harness.store.connection, launcher=launcher, clock=lambda: now[0])
        before = harness.store.connection.total_changes
        first = coordinator.dispatch_once()
        self.assertEqual(first["status"], "launched")
        self.assertEqual(first["material_probe"]["pending_count"], 1)
        self.assertFalse(first["material_probe"]["urgent"])
        launcher.settle(first["ticket_ref"], summary={"framework_status": "nothing_new"})
        self.assertEqual(coordinator.dispatch_once()["status"], "idle")
        now[0] = (drafted + timedelta(days=14, seconds=1)).timestamp()
        due = coordinator.dispatch_once()
        self.assertEqual(due["status"], "waiting")
        self.assertNotEqual(first["signature"], due["signature"])
        now[0] = first_moment + MIN_INTERVAL_SECONDS
        self.assertEqual(coordinator.dispatch_once()["status"], "launched")
        self.assertEqual(harness.store.connection.total_changes, before)
        self.assertEqual(IndustryFrameworkAuthority(harness.store).framework(prior["id"])["content_hash"], prior["content_hash"])

    def test_unreadable_probe_refuses_without_launch(self):
        from unittest.mock import patch
        coordinator, launcher = self.coordinator([100.0])
        with patch.object(coordinator, "_material_probe", side_effect=ValueError("source drift")):
            self.assertEqual(coordinator.dispatch_once()["status"], "unavailable")
        self.assertEqual(launcher.started, [])


if __name__ == "__main__":
    unittest.main()
