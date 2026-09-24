"""Switching the weekly brief from plan v3 to plan v4 in one owner-run script.

The legacy install runs plan v3 (0.1, pinned pack) and needs plan v4 (0.2,
Ledger refresh).  Governance must authorize the exact v4 hash before the
controller sends it, the constitution and mission must follow the policy, and
service.json must be replaced without losing a byte of anything else.  These
tests run the script against a throwaway Core shaped like legacy: dry-run,
rehearsal, and ``--apply`` through a real writer process.
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import plistlib
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path

from dalton_core.coverage_mission import CoverageMissionAuthority
from dalton_core.research_constitution import ResearchConstitutionAuthority
from dalton_core.store import DaltonStore
from dalton_core.weekly_brief_coordinator import (
    WEEKLY_BRIEF_AUTO_PUBLISH_RULE_REF,
    WeeklyBriefSchedulePlan,
    run_weekly_brief_cycle,
)
from dalton_core.writer_server import (
    CORE_OPERATIONS, Principal, load_principals, write_token_config,
)
from scripts.sign_auto_commit_rules import PlanError
from scripts.switch_weekly_brief_plan import (
    DEFAULT_PLAN,
    DEFAULT_PLAN_HASH,
    main,
    plan_switch,
    predict_cycle,
    rehearse,
)
from tests import test_weekly_brief_coordinator as coordinator_fixture
from tests.p9a_fixtures import bootstrap_method_authorities, mission_params

ROOT = Path(__file__).resolve().parents[1]
V3 = ROOT / "deploy/phase1/weekly-brief-schedule-us-it-services-v3.json"
V3_HASH = "75153819af1180829c62aa45526a688914132eb427fa59ee31e083bd83e9c8a1"
OWNER = "human:fixture-owner"
MISSION_REF = "coverage-mission:us-it-services"
# After v4's effective_from and before its first slot (10-01 11:00Z).
AS_OF = "2026-09-24T13:00:00+00:00"


def _run(argv: list[str]) -> dict:
    stdout = io.StringIO()
    with contextlib.redirect_stdout(stdout):
        if main(argv) != 0:  # pragma: no cover - main raises instead
            raise AssertionError("script failed")
    return json.loads(stdout.getvalue())


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class SwitchWeeklyBriefPlanTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="wbsw-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.state_dir = self.root / "state"
        (self.state_dir / "run").mkdir(parents=True)
        self.v3 = json.loads(V3.read_text())
        store = DaltonStore(str(self.state_dir / "core.sqlite"))
        try:
            seeded = bootstrap_method_authorities(store)
            # Legacy shape: the active policy authorizes exactly v3 ...
            active = store.active_policy()
            body = dict(active["policy"])
            body["weekly_brief_auto_publish"] = {
                "enabled": True, "rule_ref": WEEKLY_BRIEF_AUTO_PUBLISH_RULE_REF,
                "allowed_plan_bindings": [
                    {"plan_ref": self.v3["plan_ref"], "plan_hash": V3_HASH}],
                "max_issues_per_week": 1,
            }
            policy = store.create_policy(
                body, policy_version_id="policy-2", version_number=2,
                prior_version_ref=active["policy_version_id"], actor_ref=OWNER,
                effective_from="2026-09-01T00:00:00+00:00",
                change_reason="fixture: authorize weekly brief plan v3")
            # ... and the constitution binds that policy and records v3.
            prior = seeded["constitution"]
            bindings = dict(prior["bindings"])
            bindings["governance_policy_version"] = {
                "ref": policy["policy_version_id"], "hash": policy["content_hash"]}
            bindings["weekly_brief_plan"] = {"ref": self.v3["plan_ref"], "hash": V3_HASH}
            seeded["constitution"] = ResearchConstitutionAuthority(store).publish_constitution(
                "constitution:us-it-services", industry_ref=prior["industry_ref"],
                title=prior["title"], bindings=bindings, method=prior["method"],
                actor_ref=OWNER, version_id="constitution-version:us-it-services:2",
                prior_version_ref=prior["id"], idempotency_key="fixture-constitution:2")
            params = mission_params(seeded)
            params.pop("mission_ref")
            params["version_id"] = "coverage-mission-version:us-it-services:1"
            params["idempotency_key"] = "fixture-mission:1"
            CoverageMissionAuthority(store).create_mission(MISSION_REF, **params)
        finally:
            store.close()
        self.tokens = self.state_dir / "writer-tokens.json"
        write_token_config(self.tokens, [
            Principal("core", "core-secret-token", CORE_OPERATIONS, unrestricted=True)])
        self.config_path = self.root / "config" / "service.json"
        self.config_path.parent.mkdir()
        self.heartbeat = self.state_dir / "run" / "heartbeat.json"
        self.config = {
            "schema_version": "0.1",
            "heartbeat_path": str(self.heartbeat),
            "tick_seconds": 2,
            "outbox": {"enabled": True, "interval_seconds": 30,
                       "config": {"endpoint_ref": self.v3["destination_ref"]}},
            "weekly_brief": {
                "enabled": True, "interval_seconds": 300,
                "config": {
                    "plan": self.v3,
                    "token_config": str(self.tokens),
                    "writer_socket": str(self.state_dir / "run" / "writer.sock"),
                },
            },
            "zz_unrelated": {"note": "日本語 must survive", "n": [1, 2.5, None]},
        }
        self.config_path.write_text(json.dumps(self.config, indent=2, sort_keys=True) + "\n")
        os.chmod(self.config_path, 0o600)
        self.good_plist = self._plist("good", {"PYTHONPATH": str(ROOT / "src")})
        stale = self.root / "stale-release"
        (stale / "dalton_core").mkdir(parents=True)
        (stale / "dalton_core" / "__init__.py").write_text("")
        (stale / "dalton_core" / "weekly_brief_coordinator.py").write_text(
            "class WeeklyBriefSchedulePlan:\n"
            "    @classmethod\n"
            "    def from_mapping(cls, raw):\n"
            "        raise ValueError('weekly brief schedule plan has an invalid closed shape')\n")
        self.stale_plist = self._plist("stale", {"PYTHONPATH": str(stale)})

    def _plist(self, name: str, env: dict[str, str]) -> Path:
        path = self.root / f"{name}.plist"
        with path.open("wb") as handle:
            plistlib.dump({"Label": f"test.dalton.{name}",
                           "ProgramArguments": [sys.executable, "-m", "dalton_core.service"],
                           "EnvironmentVariables": env}, handle)
        return path

    def _args(self, *extra: str, plist: Path | None = None) -> list[str]:
        plist = plist or self.good_plist
        return ["--state-dir", str(self.state_dir), "--service-config", str(self.config_path),
                "--controller-plist", str(plist), "--writer-plist", str(plist), *extra]

    def _governance(self, state_dir: Path | None = None) -> dict:
        connection = sqlite3.connect(
            f"file:{(state_dir or self.state_dir) / 'core.sqlite'}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        try:
            policy = connection.execute(
                "SELECT v.policy_version_id, v.policy_json, v.actor_ref FROM governance_policy_pointer p "
                "JOIN governance_policy_versions v USING(policy_version_id)").fetchone()
            mission = json.loads(connection.execute(
                "SELECT v.record_json FROM coverage_mission_pointer p JOIN coverage_mission_versions v "
                "USING(mission_version_id)").fetchone()[0])
            constitution = json.loads(connection.execute(
                "SELECT record_json FROM research_constitution_versions "
                "WHERE constitution_version_id=?",
                (mission["bindings"]["constitution_version"]["ref"],)).fetchone()[0])
            body = json.loads(policy["policy_json"])
            body = body.get("policy", body)
            return {"policy": policy["policy_version_id"], "actor": policy["actor_ref"],
                    "bindings": body["weekly_brief_auto_publish"]["allowed_plan_bindings"],
                    "rule": body["weekly_brief_auto_publish"],
                    "constitution": constitution, "mission": mission}
        finally:
            connection.close()

    def _v4_binding(self) -> dict:
        return {"plan_ref": "weekly-brief-plan:us-it-services:v4",
                "plan_hash": DEFAULT_PLAN_HASH}

    def _assert_config_switched(self, path: Path) -> None:
        after = json.loads(path.read_text())
        self.assertEqual(after["weekly_brief"]["config"]["plan"],
                         json.loads(DEFAULT_PLAN.read_text()))
        self.assertEqual(WeeklyBriefSchedulePlan.from_mapping(
            after["weekly_brief"]["config"]["plan"]).content_hash, DEFAULT_PLAN_HASH)
        # Every other field is exactly what it was.
        expected = json.loads(json.dumps(self.config))
        expected["weekly_brief"]["config"]["plan"] = after["weekly_brief"]["config"]["plan"]
        self.assertEqual(after, expected)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        backups = sorted(path.parent.glob("service.pre-weekly-brief-v4-*.json"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(json.loads(backups[0].read_text()), self.config)
        self.assertEqual(stat.S_IMODE(backups[0].stat().st_mode), 0o600)
        self.assertEqual(list(path.parent.glob(".service.json.*.tmp")), [])

    # ------------------------------------------------------------------ dry-run

    def test_dry_run_reports_the_cascade_and_writes_nothing(self) -> None:
        db_before, config_before = _sha(self.state_dir / "core.sqlite"), _sha(self.config_path)
        result = _run(self._args("--as-of", AS_OF))
        self.assertEqual(result["mode"], "dry-run")
        self.assertEqual(result["status"], "would-publish")
        self.assertEqual(result["publishes"], [
            "policy-3", "constitution-version:us-it-services:3",
            "coverage-mission-version:us-it-services:2"])
        bindings = result["diff"]["policy.weekly_brief_auto_publish.allowed_plan_bindings"]
        self.assertEqual(bindings["after"], [*bindings["before"], self._v4_binding()])
        self.assertEqual(result["diff"]["constitution.bindings.weekly_brief_plan"]["after"],
                         {"ref": "weekly-brief-plan:us-it-services:v4", "hash": DEFAULT_PLAN_HASH})
        self.assertEqual(result["configured_plan"]["plan_hash"], V3_HASH)
        self.assertEqual(result["first_cycle"]["status"], "waiting")
        self.assertEqual(result["first_cycle"]["next_slot"], "2026-10-01T11:00:00.000000+00:00")
        self.assertTrue(result["runtime_ready"])
        self.assertTrue(result["restart"]["controller"]["needed"])
        self.assertIn("launchctl kickstart -k gui/", result["restart"]["controller"]["command"])
        self.assertTrue(result["restart"]["controller"]["command"].endswith("/test.dalton.good"))
        self.assertFalse(result["restart"]["writer"]["needed"])
        self.assertEqual(_sha(self.state_dir / "core.sqlite"), db_before)
        self.assertEqual(_sha(self.config_path), config_before)
        self.assertEqual(list(self.config_path.parent.iterdir()), [self.config_path])

    def test_dry_run_flags_a_runtime_that_cannot_parse_plan_02(self) -> None:
        result = _run(self._args(plist=self.stale_plist))
        self.assertFalse(result["runtime_ready"])
        self.assertFalse(result["runtime"]["controller"]["supports_plan"])
        self.assertIn("closed shape", result["runtime"]["controller"]["reason"])

    def test_a_plan_file_that_is_not_the_reviewed_hash_is_refused(self) -> None:
        edited = json.loads(DEFAULT_PLAN.read_text())
        edited["evidence_refresh"]["claim_window_days"] = 90
        path = self.root / "edited.json"
        path.write_text(json.dumps(edited))
        with self.assertRaisesRegex(PlanError, "not the reviewed"):
            main(self._args("--plan", str(path), "--expect-plan-hash", DEFAULT_PLAN_HASH))

    def test_a_config_from_another_environment_is_refused(self) -> None:
        other = json.loads(json.dumps(self.config))
        other["weekly_brief"]["config"]["token_config"] = "/elsewhere/writer-tokens.json"
        self.config_path.write_text(json.dumps(other))
        with self.assertRaisesRegex(PlanError, "same environment"):
            plan_switch(self.state_dir, self.config_path, controller_plist=None,
                        writer_plist=None)

    # ---------------------------------------------------------------- rehearse

    def test_rehearsal_switches_copies_and_leaves_the_originals(self) -> None:
        db_before, config_before = _sha(self.state_dir / "core.sqlite"), _sha(self.config_path)
        target = self.root / "rehearsal"
        result = _run(self._args("--rehearse", str(target), "--actor", OWNER,
                                 "--as-of", AS_OF))
        self.assertEqual(result["mode"], "rehearsal")
        self.assertEqual(result["governance"]["status"], "published")
        chain = result["governance"]["chain"]
        self.assertEqual(chain["policy"]["ref"], "policy-3")
        self.assertEqual(chain["mandate"]["status"], "unchanged")
        self.assertEqual(chain["mission"]["ref"], "coverage-mission-version:us-it-services:2")
        self.assertTrue(all(result["governance_state"].values()))
        self.assertEqual(result["service_config"]["status"], "rewritten")
        self.assertEqual(result["configured_plan"]["plan_hash"], DEFAULT_PLAN_HASH)
        # The real cycle on the copy agrees with the prediction.
        self.assertEqual(result["cycle_probe"]["status"], "waiting")
        self.assertEqual(result["cycle_probe"]["plan_hash"], DEFAULT_PLAN_HASH)
        self.assertTrue(result["cycle_probe_matches_prediction"])
        governance = self._governance(target)
        self.assertEqual(governance["bindings"], [
            {"plan_ref": self.v3["plan_ref"], "plan_hash": V3_HASH}, self._v4_binding()])
        self.assertEqual(governance["actor"], OWNER)
        self.assertEqual(governance["rule"]["max_issues_per_week"], 1)
        self.assertEqual(governance["constitution"]["bindings"]["weekly_brief_plan"],
                         {"ref": "weekly-brief-plan:us-it-services:v4", "hash": DEFAULT_PLAN_HASH})
        self.assertEqual(governance["constitution"]["bindings"]["governance_policy_version"]["ref"],
                         "policy-3")
        self._assert_config_switched(target / "service.json")
        # Originals untouched.
        self.assertEqual(_sha(self.state_dir / "core.sqlite"), db_before)
        self.assertEqual(_sha(self.config_path), config_before)
        self.assertEqual(self._governance()["policy"], "policy-2")

    def test_rehearsal_finishes_a_cascade_interrupted_after_the_policy(self) -> None:
        store = DaltonStore(str(self.state_dir / "core.sqlite"))
        try:
            active = store.active_policy()
            body = dict(active["policy"])
            rule = dict(body["weekly_brief_auto_publish"])
            rule["allowed_plan_bindings"] = [*rule["allowed_plan_bindings"], self._v4_binding()]
            body["weekly_brief_auto_publish"] = rule
            store.create_policy(body, policy_version_id="policy-3", version_number=3,
                                prior_version_ref="policy-2", actor_ref=OWNER,
                                effective_from="2026-09-24T12:30:00+00:00",
                                change_reason="fixture: crashed after the policy step")
        finally:
            store.close()
        planned = plan_switch(self.state_dir, self.config_path, as_of=datetime.fromisoformat(AS_OF),
                              controller_plist=None, writer_plist=None)
        self.assertEqual(planned["publishes"], [
            "constitution-version:us-it-services:3", "coverage-mission-version:us-it-services:2"])
        result = rehearse(self.state_dir, self.config_path, self.root / "resume", actor=OWNER,
                          as_of=datetime.fromisoformat(AS_OF))
        self.assertEqual(result["governance"]["chain"]["policy"]["status"], "unchanged")
        self.assertEqual(result["active_policy"], "policy-3")
        self.assertTrue(all(result["governance_state"].values()))

    # ------------------------------------------------------------------- apply

    def _start_writer(self) -> None:
        socket = self.state_dir / "run" / "writer.sock"
        env = {**os.environ, "PYTHONPATH": str(ROOT / "src")}
        process = subprocess.Popen(
            [sys.executable, "-m", "dalton_core.writer_server",
             "--db", str(self.state_dir / "core.sqlite"),
             "--socket", str(socket), "--token-config", str(self.tokens)],
            cwd=str(ROOT), env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        def stop() -> None:
            process.terminate()
            process.wait(timeout=5)

        self.addCleanup(stop)
        deadline = time.time() + 10
        while time.time() < deadline and not socket.exists():
            if process.poll() is not None:
                self.fail("writer server exited")
            time.sleep(0.02)
        self.assertTrue(socket.exists())

    def test_apply_publishes_through_the_writer_then_swaps_the_config(self) -> None:
        self._start_writer()
        result = _run(self._args("--apply", "--actor", OWNER))
        self.assertEqual(result["mode"], "live")
        self.assertEqual(result["governance"]["status"], "published")
        self.assertEqual(result["governance"]["chain"]["policy"]["ref"], "policy-3")
        self.assertEqual(result["service_config"]["status"], "rewritten")
        self.assertEqual(result["service_config"]["mode"], "0o600")
        self.assertTrue(result["restart"]["controller"]["needed"])
        governance = self._governance()
        self.assertEqual(governance["policy"], "policy-3")
        self.assertEqual(governance["actor"], OWNER)
        self.assertIn(self._v4_binding(), governance["bindings"])
        self.assertEqual(governance["mission"]["bindings"]["constitution_version"]["ref"],
                         "constitution-version:us-it-services:3")
        self._assert_config_switched(self.config_path)
        # The ephemeral human principal is gone again.
        self.assertEqual(set(load_principals(self.tokens)), {"core"})

        # A second run is a report, not a second publish.
        again = _run(self._args("--apply", "--actor", OWNER))
        self.assertEqual(again["governance"]["status"], "already-published")
        self.assertEqual(again["service_config"]["status"], "already-switched")
        self.assertEqual(self._governance()["policy"], "policy-3")
        self.assertEqual(_run(self._args())["status"], "already-switched")

        # Verify: incomplete until the restarted controller reports v4 ...
        verified = _run(self._args("--verify"))
        self.assertEqual(verified["status"], "incomplete")
        self.assertFalse(verified["checks"]["controller_runs_plan"])
        self.assertIn("kickstart", verified["hint"])
        # ... and switched once it does -- already_issued included.
        for status in ("waiting", "already_issued"):
            self.heartbeat.write_text(json.dumps({"started_at": "2026-09-24T13:05:00+00:00",
                "weekly_brief": {"state": status, "last_error": None, "last_result": {
                    "status": status, "plan_ref": "weekly-brief-plan:us-it-services:v4",
                    "plan_hash": DEFAULT_PLAN_HASH}}}))
            verified = _run(self._args("--verify"))
            self.assertEqual(verified["status"], "switched", verified["checks"])

    def test_apply_refuses_a_runtime_that_cannot_run_plan_02(self) -> None:
        config_before = _sha(self.config_path)
        with self.assertRaisesRegex(PlanError, "deploy the release"):
            main(self._args("--apply", "--actor", OWNER, plist=self.stale_plist))
        self.assertEqual(self._governance()["policy"], "policy-2")
        self.assertEqual(_sha(self.config_path), config_before)

    def test_apply_and_rehearse_require_a_human_actor(self) -> None:
        with self.assertRaisesRegex(PlanError, "--actor human:"):
            main(self._args("--apply", "--actor", "core"))
        with self.assertRaisesRegex(PlanError, "--actor human:"):
            main(self._args("--rehearse", str(self.root / "r")))


class AlreadyIssuedSlotTests(unittest.TestCase):
    """A switch that lands on an issued slot must not publish that week twice."""

    WEEK = "2026-12-03T12:30:00+00:00"  # a Thursday, after 07:00 New York

    def setUp(self) -> None:
        fixture = coordinator_fixture.WeeklyBriefCoordinatorTests(
            methodName="test_exact_plan_publishes_and_replays_without_duplicates")
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.fx = fixture
        self.store = fixture.fixture.store
        self.pinned = fixture.plan()
        self.refreshing = {
            "schema_version": "0.2",
            "plan_ref": "weekly-brief-plan:us-it-services:v4",
            "brief_ref": "weekly-brief:us-it-services",
            "timezone": "America/New_York", "weekday": 3, "hour": 7, "minute": 0,
            "effective_from": "2026-11-26T00:00:00+00:00",
            "evidence_refresh": {
                "evidence_pack_ref": "industry-evidence-pack:us-it-services",
                "company_overlay_refs": ["company-overlay:acn"],
                "claim_window_days": 365,
            },
            "company_thesis_refs": {},
            "destination_ref": "openclaw:discord:test:channel:weekly",
        }
        active = self.store.active_policy()
        policy = dict(active["policy"])
        policy["weekly_brief_auto_publish"] = {
            "enabled": True, "rule_ref": WEEKLY_BRIEF_AUTO_PUBLISH_RULE_REF,
            "allowed_plan_bindings": [
                {"plan_ref": item["plan_ref"],
                 "plan_hash": WeeklyBriefSchedulePlan.from_mapping(item).content_hash}
                for item in (self.pinned, self.refreshing)],
            "max_issues_per_week": 1,
        }
        self.store.create_policy(
            policy, policy_version_id="policy:switch-test:v2", version_number=2,
            prior_version_ref=active["policy_version_id"], actor_ref=OWNER,
            effective_from="2026-08-20T00:00:00+00:00", change_reason="both plans")

    def _cycle(self, plan: dict, as_of: str) -> dict:
        return run_weekly_brief_cycle(self.store, self.fx.weekly, self.fx.agenda,
                                      plan=plan, as_of=as_of, actor_ref="core")

    def _issues(self) -> int:
        return self.store.connection.execute(
            "SELECT COUNT(*) FROM weekly_brief_issue_versions").fetchone()[0]

    def test_prediction_matches_the_writer_across_the_switch(self) -> None:
        v4 = WeeklyBriefSchedulePlan.from_mapping(self.refreshing)
        clock = datetime.fromisoformat(self.WEEK)
        early = predict_cycle(self.store.connection, v4,
                              datetime(2026, 11, 26, 1, tzinfo=timezone.utc))
        self.assertEqual(early["status"], "waiting")
        self.assertEqual(early["next_slot"], "2026-11-26T12:00:00.000000+00:00")
        self.assertEqual(predict_cycle(self.store.connection, v4, clock)["status"],
                         "would_issue")
        issued = self._cycle(self.pinned, self.WEEK)
        self.assertEqual(issued["status"], "ready")
        self.assertEqual(self._issues(), 1)
        predicted = predict_cycle(self.store.connection, v4, clock)
        self.assertEqual(predicted["status"], "already_issued")
        self.assertEqual(predicted["issued_cycle_ref"], issued["cycle_ref"])
        self.assertEqual(predicted["issued_plan_ref"], self.pinned["plan_ref"])
        switched = self._cycle(self.refreshing, self.WEEK)
        self.assertEqual(switched["status"], "already_issued")
        self.assertEqual(switched["issued_cycle_ref"], issued["cycle_ref"])
        # Polled again, still terminal, still one issue and one outbox message.
        self.assertEqual(self._cycle(self.refreshing, self.WEEK)["status"], "already_issued")
        self.assertEqual(self._issues(), 1)
        self.assertEqual(len(self.fx.agenda.pending_outbox()), 1)


if __name__ == "__main__":
    unittest.main()
