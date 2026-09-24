"""Q3: the quality scoring lane -- installed by itself, scheduled, and rate limited.

Live 2026-09-24: ``research_quality_score_versions`` held zero rows in every
environment.  The only thing that could create the verifier's configuration was
an ``install.sh`` block behind an environment variable nobody set, and nothing
on the tick ever called the scorer.
"""

from __future__ import annotations

import json
import os
import sqlite3
import stat
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from dalton_core import mission_quality_score_lane as lane
from dalton_core.lane_registry import lane_for_operation, load_lanes
from dalton_core.budget_pools import pool_for_operation, pool_for_purpose

NOW = datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)

TEMPLATE = {
    "broker_auth_key": "/tmp/key",
    "broker_client_id": "client:dalton-core",
    "broker_socket": "/tmp/broker.sock",
    "budget_db": "/tmp/state/thesis-impact-budget.sqlite",
    "budget_policy_ref": "thesis-impact-day-budget-policy:configured:abc",
    "credential_slot_refs": ["credential-slot:openclaw:google"],
    "expected_agent_id": "chem",
    "model_router_db": "/tmp/state/model-router.sqlite",
    "provider_retry": {"max_same_profile_retries": 1, "retry_backoff_seconds": 2},
    "purpose_call_budgets": {"dossier_verifier": {"max_cost_usd": 1.0},
                             "investment_memo_verifier": {"max_cost_usd": 1.0}},
    "call_budget": {"max_cost_usd": 1.0, "max_input_tokens": 64000,
                    "max_output_tokens": 4096, "timeout_seconds": 600},
    "routing_policy_ref": "model-routing-policy-version:dalton-openclaw-dossier-verifier:58",
    "transport_retry": {"max_definitely_not_sent_retries": 1,
                        "queue_wait_seconds": 600, "retry_backoff_seconds": 2},
}


class VerifierConfigTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.state = Path(self.temp.name).resolve()

    def tearDown(self):
        self.temp.cleanup()

    def test_derived_from_the_dossier_verifier_with_one_capped_purpose(self):
        derived = lane.derive_quality_verifier_config(TEMPLATE)
        self.assertEqual(derived["routing_policy_ref"], TEMPLATE["routing_policy_ref"])
        for key in ("broker_socket", "budget_db", "budget_policy_ref",
                    "credential_slot_refs", "model_router_db", "transport_retry"):
            self.assertEqual(derived[key], TEMPLATE[key])
        self.assertEqual(derived["purpose_call_budgets"],
                         {"quality_verifier": {"max_cost_usd": 1.0}})
        self.assertNotIn("call_budget", derived)
        self.assertEqual(lane.QUALITY_VERIFIER_MAX_COST_USD, 1.0)

    def test_the_shared_call_budget_policy_binding_is_kept(self):
        from dalton_core.call_budget import resolve_call_budget
        from dalton_core.store import content_hash

        policy = {"schema_version": "dalton-shared-call-budget-policy-0.1",
                  "default_max_cost_usd": 1.0, "purpose_max_cost_usd": {"plan": 1.5},
                  "revision": 1, "prior_hash": None,
                  "updated_at": "2026-09-16T00:00:00+00:00", "actor_ref": "human:owner"}
        path = self.state / "model-call-budget-policy.json"
        path.write_text(json.dumps({**policy, "content_hash": content_hash(policy)}))
        derived = lane.derive_quality_verifier_config(
            {**TEMPLATE, "shared_call_budget_policy_path": str(path)})
        self.assertEqual(derived["shared_call_budget_policy_path"], str(path))
        # The shared policy is what the cap resolves to at call time: $1.00.
        budget = resolve_call_budget(derived, "quality_verifier", defaults={
            "max_input_tokens": 1, "max_output_tokens": 1, "max_cost_usd": 9.0,
            "timeout_seconds": 1})
        self.assertEqual(budget["max_cost_usd"], 1.0)

    def test_installed_once_owner_only_and_never_overwritten(self):
        (self.state / lane.VERIFIER_TEMPLATE_CONFIG).write_text(json.dumps(TEMPLATE))
        first = lane.ensure_quality_verifier_config(self.state)
        self.assertEqual(first["status"], "installed")
        target = self.state / lane.VERIFIER_MODEL_CONFIG
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
        self.assertEqual(json.loads(target.read_text())["purpose_call_budgets"],
                         {"quality_verifier": {"max_cost_usd": 1.0}})
        # The owner edits it on the models page; a restart must not undo that.
        owned = {**json.loads(target.read_text()),
                 "purpose_call_budgets": {"quality_verifier": {"max_cost_usd": 0.25}}}
        target.write_text(json.dumps(owned))
        self.assertEqual(lane.ensure_quality_verifier_config(self.state)["status"], "present")
        self.assertEqual(json.loads(target.read_text()), owned)
        self.assertEqual([p.name for p in self.state.iterdir() if p.name.startswith(".")], [])

    def test_unavailable_without_a_dossier_verifier(self):
        result = lane.ensure_quality_verifier_config(self.state)
        self.assertEqual(result["status"], "unavailable")
        self.assertFalse((self.state / lane.VERIFIER_MODEL_CONFIG).exists())
        (self.state / lane.VERIFIER_TEMPLATE_CONFIG).write_text(json.dumps({"x": 1}))
        self.assertEqual(lane.ensure_quality_verifier_config(self.state)["status"],
                         "unavailable")
        self.assertFalse((self.state / lane.VERIFIER_MODEL_CONFIG).exists())


def _core(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.executescript(
        "CREATE TABLE mission_deliverable_versions(version_id TEXT PRIMARY KEY, "
        "record_json TEXT, created_at TEXT);"
        "CREATE TABLE mission_deliverable_pointer(deliverable_ref TEXT PRIMARY KEY, "
        "version_id TEXT);"
        "CREATE TABLE research_quality_score_versions(version_id TEXT PRIMARY KEY, "
        "target_ref TEXT, target_hash TEXT, rubric_ref TEXT);")
    return connection


def _publish(connection, deliverable_ref: str, version: str, digest: str, created: str):
    connection.execute("INSERT INTO mission_deliverable_versions VALUES(?,?,?)", (
        version, json.dumps({"id": version, "content_hash": digest}), created))
    connection.execute("INSERT OR REPLACE INTO mission_deliverable_pointer VALUES(?,?)",
                       (deliverable_ref, version))


class PendingTargetTests(unittest.TestCase):
    def test_latest_initial_screens_not_yet_scored_under_the_rubric(self):
        from dalton_core.research_quality_rubrics import rubric

        rubric_ref = rubric("initial_screen").rubric_ref
        with tempfile.TemporaryDirectory() as directory:
            core = _core(Path(directory) / "core.sqlite")
            _publish(core, "mission-deliverable:initial_screen:1", "v1", "h1", "2026-09-14")
            _publish(core, "mission-deliverable:initial_screen:1", "v1b", "h1b", "2026-09-15")
            _publish(core, "mission-deliverable:initial_screen:2", "v2", "h2", "2026-09-16")
            _publish(core, "mission-deliverable:event_note:1", "e1", "he", "2026-09-17")
            _publish(core, "mission-deliverable:initial_screen:3", "v3", "h3", "2026-09-13")
            core.execute("INSERT INTO research_quality_score_versions VALUES(?,?,?,?)",
                         ("s1", "v2", "h2", rubric_ref))
            # Scored under another rubric is not scored under this one.
            core.execute("INSERT INTO research_quality_score_versions VALUES(?,?,?,?)",
                         ("s2", "v3", "h3", "rubric:weekly-brief"))
            pending = lane.pending_targets(core)
            core.close()
        self.assertEqual([item["target_ref"] for item in pending], ["v3", "v1b"])
        self.assertEqual(pending[1], {"deliverable_ref": "mission-deliverable:initial_screen:1",
                                      "target_ref": "v1b", "target_hash": "h1b"})


class FakeLauncher:
    def __init__(self):
        self.written: list[dict] = []
        self.statuses: dict[str, dict] = {}
        self.started: list[dict] = []

    def tickets(self):
        return sorted(self.written, key=lambda row: row["started_at"], reverse=True)

    def start(self, **target):
        ticket = {"id": f"quality-score-run:{len(self.started):024x}",
                  "status": "running", **target,
                  "started_at": self.clock().isoformat()}
        self.started.append(target)
        self.written.append(ticket)
        self.statuses[ticket["id"]] = ticket
        return ticket

    def status(self, ticket_ref):
        return self.statuses[ticket_ref]


class CoordinatorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.core = _core(Path(self.temp.name) / "core.sqlite")
        for index in range(10):
            _publish(self.core, f"mission-deliverable:initial_screen:{index}",
                     f"v{index}", f"h{index}", f"2026-09-{10 + index:02d}")
        self.now = NOW
        self.launcher = FakeLauncher()
        self.launcher.clock = lambda: self.now
        self.coordinator = lane.QualityScoreLaneCoordinator(
            connection=lambda: self.core, launcher=self.launcher, clock=lambda: self.now)

    def tearDown(self):
        self.core.close()
        self.temp.cleanup()

    def _finish(self, ticket_ref, *, scored: bool):
        ticket = self.launcher.statuses[ticket_ref]
        ticket["status"] = "succeeded" if scored else "failed"
        if scored:
            from dalton_core.research_quality_rubrics import rubric

            self.core.execute("INSERT INTO research_quality_score_versions VALUES(?,?,?,?)", (
                f"s-{ticket['target_ref']}", ticket["target_ref"], ticket["target_hash"],
                rubric("initial_screen").rubric_ref))

    def test_one_at_a_time_and_never_faster_than_the_interval(self):
        first = self.coordinator.dispatch_once()
        self.assertEqual(first["status"], "launched")
        self.assertEqual(self.coordinator.dispatch_once()["status"], "running")
        self._finish(first["ticket_ref"], scored=True)
        self.now += timedelta(minutes=5)
        held = self.coordinator.dispatch_once()
        self.assertEqual((held["status"], held["reason"]), ("held", "minimum launch interval"))
        self.assertEqual(held["settled"]["status"], "succeeded")
        self.now += timedelta(seconds=lane.MIN_LAUNCH_INTERVAL_SECONDS)
        self.assertEqual(self.coordinator.dispatch_once()["status"], "launched")

    def test_daily_cap_bounds_a_backlog(self):
        launched = 0
        for _ in range(20):  # ten hours of ticks, every half hour
            result = self.coordinator.dispatch_once()
            if result["status"] == "launched":
                launched += 1
                self._finish(result["ticket_ref"], scored=True)
            self.now += timedelta(seconds=lane.MIN_LAUNCH_INTERVAL_SECONDS)
        self.assertEqual(launched, lane.MAX_LAUNCHES_PER_DAY)
        held = self.coordinator.dispatch_once()
        self.assertEqual((held["status"], held["reason"]), ("held", "daily launch cap reached"))
        self.assertEqual(held["pending"], 10 - lane.MAX_LAUNCHES_PER_DAY)
        self.now = NOW + timedelta(days=1, seconds=1)
        self.assertEqual(self.coordinator.dispatch_once()["status"], "launched")

    def test_a_failed_target_waits_a_day_and_does_not_block_the_rest(self):
        first = self.coordinator.dispatch_once()
        self._finish(first["ticket_ref"], scored=False)
        self.now += timedelta(seconds=lane.MIN_LAUNCH_INTERVAL_SECONDS)
        second = self.coordinator.dispatch_once()
        self.assertEqual(second["status"], "launched")
        self.assertNotEqual(second["target_ref"], first["target_ref"])
        self.assertEqual(second["held_after_failure"], 1)
        self._finish(second["ticket_ref"], scored=True)
        self.now = NOW + timedelta(seconds=lane.FAILED_RETRY_SECONDS + 1)
        retried = self.coordinator.dispatch_once()
        self.assertEqual(retried["status"], "launched")
        self.assertEqual(retried["target_ref"], first["target_ref"])

    def test_idle_when_everything_is_scored(self):
        for row in lane.pending_targets(self.core):
            ticket = self.launcher.start(**row)
            self._finish(ticket["id"], scored=True)
        self.launcher.written.clear()
        result = self.coordinator.dispatch_once()
        self.assertEqual(result["status"], "idle")
        self.assertEqual(result["pending"], 0)


class RegistrationTests(unittest.TestCase):
    def test_registered_on_the_tick_in_the_maintenance_pool_without_a_flag(self):
        load_lanes()
        spec = lane_for_operation("dispatch_quality_scoring")
        self.assertIsNotNone(spec)
        self.assertEqual(spec.order, 112)
        self.assertEqual(spec.driver_key, "quality_scoring")
        # No writer argument: a release switch never rewrites plist arguments.
        self.assertIsNone(spec.argparse)
        self.assertIsNone(spec.argv_fragment)
        self.assertEqual(pool_for_operation("dispatch_quality_scoring"), "maintenance")
        self.assertEqual(pool_for_purpose("quality"), "maintenance")
        self.assertEqual(pool_for_purpose("quality_verifier"), "maintenance")

    def test_build_launcher_installs_the_verifier_and_never_raises(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory).resolve()
            args = SimpleNamespace(db=str(state / "core.sqlite"),
                                   scheduler=str(state / "scheduler.sqlite"),
                                   initial_screen_model_config=None)
            self.assertIsNone(lane.build_launcher(args))  # no judge configuration
            (state / lane.JUDGE_MODEL_CONFIG).write_text(json.dumps(TEMPLATE))
            self.assertIsNone(lane.build_launcher(args))  # nothing to derive from
            (state / lane.VERIFIER_TEMPLATE_CONFIG).write_text(json.dumps(TEMPLATE))
            launcher = lane.build_launcher(args)
            try:
                self.assertIsInstance(launcher, lane.QualityScoreLauncher)
                self.assertTrue((state / lane.VERIFIER_MODEL_CONFIG).is_file())
                command = launcher._command(ticket_dir=state / "t", target_ref="v1")
            finally:
                launcher.close()
            broken = SimpleNamespace(db=None, scheduler=None)
            self.assertIsNone(lane.build_launcher(broken))
        self.assertEqual(command[1:4], ["-m", "dalton_core.research_quality_cli", "score"])
        from dalton_core.research_quality_cli import build_parser
        parsed = build_parser().parse_args(command[3:])
        self.assertEqual(parsed.rubric, "initial_screen")
        self.assertEqual(parsed.target, "v1")
        self.assertEqual(parsed.verifier_model_config,
                         str(state / lane.VERIFIER_MODEL_CONFIG))
        self.assertEqual(parsed.actor_ref, "automation:quality-scoring")
        self.assertEqual(parsed.summary_dir, str(state / "t"))

    def test_cli_writes_its_refusal_where_the_lane_reads_it(self):
        from dalton_core.research_quality_cli import main

        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory).resolve()
            summary = state / "ticket"
            with self.assertRaises(sqlite3.OperationalError):
                main(["score", "--state-dir", str(state), "--rubric", "initial_screen",
                      "--target", "mission-deliverable-version:missing",
                      "--summary-dir", str(summary)])
            written = json.loads((summary / "summary.json").read_text())
        self.assertEqual(written["status"], "failed")
        self.assertIn("mission_deliverable_versions", written["reason"])


class InstallerTests(unittest.TestCase):
    def test_install_script_no_longer_needs_the_environment_variable(self):
        script = (Path(__file__).parents[1] / "deploy/macos/install.sh").read_text("utf-8")
        block = script.split("# Q3: the quality verifier.", 1)[1].split("\nfi\n", 1)[0]
        self.assertIn("ensure_quality_verifier_config", block)
        self.assertNotIn("note: set DALTON_QUALITY_VERIFIER_MODEL_TIER", script)


if __name__ == "__main__":
    unittest.main()
