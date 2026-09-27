"""The patrol check: read-only, and it names every wall ws-7d hit before it is hit."""

from __future__ import annotations

import io
import json
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from dalton_core.coverage_mission import CoverageMissionAuthority
from dalton_core.store import DaltonStore
from dalton_core.workspace_health_parity import (
    Environment,
    check_environment,
    check_governance,
    check_lanes,
    check_mission_reviews,
)
from dalton_core.workspace_mission_setup import ensure_first_mission_auto_commit_policy
from tests import test_mission_version_carry as _carry
from tests.p9a_fixtures import bootstrap_method_authorities, mission_params

ROOT = Path(__file__).resolve().parents[1]


def _statuses(rows, section=None):
    return {row["check"]: row["status"] for row in rows
            if section is None or row["section"] == section}


def _snapshot(directory: Path) -> dict[str, tuple[int, int]]:
    """Every file's size and mtime, bar SQLite's WAL index.

    A reader of a WAL database maps ``-shm`` (and SQLite may create an empty
    ``-wal`` beside it) even through ``mode=ro``; a live Core always has both
    because its writer holds them.  Neither is content.
    """

    return {str(path.relative_to(directory)): (path.stat().st_size, path.stat().st_mtime_ns)
            for path in sorted(directory.rglob("*"))
            if path.is_file() and not path.name.endswith(("-shm", "-wal"))}


class ParityTests(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.state = Path(temp.name) / "state" / "dalton-core"
        self.state.mkdir(parents=True)
        self.store = DaltonStore(str(self.state / "core.sqlite"))
        self.addCleanup(self.store.close)

    def _mission(self):
        method = bootstrap_method_authorities(self.store)
        params = mission_params(method)
        return CoverageMissionAuthority(self.store).create_mission(params.pop("mission_ref"), **params)

    def _env(self) -> Environment:
        env = Environment("test", self.state)
        self.addCleanup(env.close)
        return env

    def test_a_blank_core_is_pending_not_broken(self):
        rows = check_governance(self._env())
        self.assertNotIn("gap", {row["status"] for row in rows})
        self.assertIn("pending", {row["status"] for row in rows})

    def test_a_mission_on_the_bootstrap_policy_has_every_ws7d_gap(self):
        self._mission()
        statuses = _statuses(check_governance(self._env()))
        for check in (
                "policy.research_plan_auto_start:research-plan-auto-start:sec-public-company-facts:v1",
                "policy.research_candidate_auto_commit:research-auto-commit:mission-document-qualitative:v1",
                "policy.research_budget"):
            self.assertEqual(statuses[check], "gap", check)
        self.assertEqual(statuses["policy.independence_predicates"], "ok")

    def test_a_policy_the_constitution_does_not_bind_is_a_gap(self):
        mission = self._mission()
        ensure_first_mission_auto_commit_policy(
            self.store, actor_ref="human:owner", mission_budget=mission["budget"])
        statuses = _statuses(check_governance(self._env()))
        self.assertEqual(statuses["policy.research_budget"], "ok")
        self.assertEqual(statuses["policy.research_plan_auto_start:"
                                  "research-plan-auto-start:sec-public-company-facts:v1"], "ok")
        # The rules are signed, but nothing rebound the constitution: document
        # extraction would refuse "does not bind current governance policy".
        self.assertEqual(statuses["constitution.binds_active_policy"], "gap")

    def test_a_lane_refusing_at_its_precondition_is_a_gap(self):
        self._mission()
        ledger = sqlite3.connect(self.state / "tick-ledger.sqlite")
        ledger.executescript((ROOT / "src/dalton_core/tick_ledger_schema.sql").read_text())
        ledger.execute(
            "INSERT INTO tick_ledger_ticks VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("tick:1", "2026-09-27", "2026-09-27T00:00:00+00:00", "2026-09-27T00:00:01+00:00",
             "ok", 0, 0, 0, 0, 1, 0, "{}", "{}", None, "0" * 64, "2026-09-27T00:00:01+00:00"))
        ledger.execute(
            "INSERT INTO tick_ledger_lanes VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("tick:1", "2026-09-27", "2026-09-27T00:00:00+00:00", "mission_sec_quarters",
             "dispatch_mission_sec_quarters", "coverage", "held", "held", 0, 0,
             json.dumps({"reason": "lane precondition: LanePreconditionError: active Core "
                                   "governance policy 'policy-1' does not authorize"}), None,
             "2026-09-27T00:00:01+00:00"))
        ledger.commit(); ledger.close()
        rows = check_lanes(self._env())
        statuses = _statuses(rows)
        self.assertEqual(statuses["mission_sec_quarters"], "gap")
        self.assertEqual(statuses["mission_sec_quarters.precondition"], "gap")
        self.assertIn("sign_research_plan_auto_start.py",
                      next(row for row in rows if row["check"] == "mission_sec_quarters.precondition")["fix"])

    def _tick(self, lanes):
        ledger = sqlite3.connect(self.state / "tick-ledger.sqlite")
        ledger.executescript((ROOT / "src/dalton_core/tick_ledger_schema.sql").read_text())
        ledger.execute(
            "INSERT INTO tick_ledger_ticks VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("tick:2", "2026-09-27", "2026-09-27T01:00:00+00:00", "2026-09-27T01:00:01+00:00",
             "ok", 0, 0, 0, 0, len(lanes), 0, "{}", "{}", None, "0" * 64,
             "2026-09-27T01:00:01+00:00"))
        for key, status, counts in lanes:
            ledger.execute(
                "INSERT INTO tick_ledger_lanes VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                ("tick:2", "2026-09-27", "2026-09-27T01:00:00+00:00", key, "dispatch_" + key,
                 "coverage", status, status, 0, 0, json.dumps(counts), None,
                 "2026-09-27T01:00:01+00:00"))
        ledger.commit(); ledger.close()

    def test_an_empty_queue_without_maintenance_is_flagged(self):
        self._mission()
        self._tick([("document_extraction", "idle", {"awaiting": 0})])
        statuses = _statuses(check_lanes(self._env()))
        self.assertEqual(statuses["document_extraction.maintenance_when_queue_empty"], "warn")

    def test_an_empty_queue_with_a_support_only_child_is_fine(self):
        self._mission()
        self._tick([("document_extraction", "idle",
                     {"awaiting": 0, "support": "held", "hold_seconds": 3600})])
        statuses = _statuses(check_lanes(self._env()))
        self.assertEqual(statuses["document_extraction.maintenance_when_queue_empty"], "ok")

    def test_the_check_writes_nothing_and_the_cli_reports(self):
        self._mission()
        self.store.close()
        before = _snapshot(self.state)
        from scripts.check_workspace_parity import main

        out = io.StringIO()
        with redirect_stdout(out):
            code = main(["--state-dir", str(self.state), "--json", "--no-host"])
        self.assertEqual(code, 0)
        report = json.loads(out.getvalue())["environments"][0]
        self.assertGreater(report["counts"]["gap"], 0)
        with redirect_stdout(io.StringIO()):
            self.assertEqual(main(["--state-dir", str(self.state), "--no-host",
                                   "--fail-on-gap"]), 1)
        self.assertEqual(_snapshot(self.state), before)


@_carry._own_tests_only
class StrandedReviewParityTests(_carry._VersionHarness):
    def test_an_open_review_left_behind_is_a_gap_until_it_is_carried(self):
        self._publish(3, carry=False)
        path = self.h.h.core.connection.execute("PRAGMA database_list").fetchone()[2]
        env = Environment("test", Path(path).parent)
        try:
            [row] = [row for row in check_mission_reviews(env)
                     if row["check"] == "reviews.stranded_on_superseded_version"]
        finally:
            env.close()
        self.assertEqual(row["status"], "gap")
        self.assertIn("source:alphaengine v2->v3: 1", row["detail"])
        self.m.carry_forward_awaiting_reviews(_carry.REF)
        env = Environment("test", Path(path).parent)
        try:
            report = check_environment(env, include_host=False)
        finally:
            env.close()
        [row] = [row for row in report["rows"]
                 if row["check"] == "reviews.stranded_on_superseded_version"]
        self.assertEqual(row["status"], "ok")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
