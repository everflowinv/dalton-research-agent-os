"""ws-7d's policy never carried ``research_plan_auto_start``; this signs it.

Every SEC company-facts run on ws-7d refused at the lane's governance
precondition, and each refusal was an attempt spent.  The script publishes
the missing block -- the SEC company-facts rules the legacy install's
``policy-18`` lists -- and cascades the constitution and the mission, the way
``sign_auto_commit_rules.py`` does for auto-commit rules.
"""

from __future__ import annotations

import contextlib
import io
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from dalton_core.coverage_mission import CoverageMissionAuthority
from dalton_core.research_plan import (
    PLAN_COMPANY_FACTS_ANNUAL_AUTO_START_RULE_REF,
    PLAN_COMPANY_FACTS_AUTO_START_RULE_REF,
)
from dalton_core.sec_company_facts_lane import LanePreconditionError, check_core_governance_rules
from dalton_core.store import DaltonStore
from scripts.sign_auto_commit_rules import read_current, rehearse as sign_commit_rules
from scripts.sign_research_plan_auto_start import (
    SEC_COMPANY_FACTS_AUTO_START_RULES,
    PlanError,
    main,
    plan,
    rehearse,
)
from tests.p9a_fixtures import bootstrap_method_authorities, mission_params

MISSION_REF = "coverage-mission:ws-fixture"
OWNER = "human:fixture-owner"
LEGACY_POLICY_18_RULES = [
    "research-plan-auto-start:sec-public-company-facts:v1",
    "research-plan-auto-start:sec-public-company-facts-annual:v1",
]


class PlanAutoStartSigningTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state_dir = Path(self.temp.name) / "state"
        self.state_dir.mkdir()
        store = DaltonStore(str(self.state_dir / "core.sqlite"))
        try:
            seeded = bootstrap_method_authorities(store)
            params = mission_params(seeded)
            params.pop("mission_ref")
            params["version_id"] = "coverage-mission-version:ws-fixture:1"
            params["idempotency_key"] = "ws-fixture:1"
            CoverageMissionAuthority(store).create_mission(MISSION_REF, **params)
        finally:
            store.close()
        # ws-7d's shape: the auto-commit rule the lane needs is signed, the
        # plan auto-start block is not there at all.
        self.base = Path(self.temp.name) / "ws7d-shape"
        sign_commit_rules(self.state_dir, self.base, actor=OWNER,
                          rules=["research-auto-commit:sec-public-company-facts-growth:v1"])

    def _policy(self, state_dir: Path) -> dict:
        connection = sqlite3.connect(f"file:{state_dir / 'core.sqlite'}?mode=ro", uri=True)
        try:
            current = read_current(connection)
        finally:
            connection.close()
        return current

    def test_the_default_matches_legacy_policy_18(self):
        self.assertEqual(list(SEC_COMPANY_FACTS_AUTO_START_RULES), LEGACY_POLICY_18_RULES)

    def test_the_fixture_reproduces_the_ws7d_refusal(self):
        store = DaltonStore(str(self.base / "core.sqlite"))
        try:
            with self.assertRaisesRegex(LanePreconditionError, "research_plan_auto_start"):
                check_core_governance_rules(store)
        finally:
            store.close()

    def test_dry_run_says_would_publish_and_what(self):
        result = plan(self.base, rules=[])
        self.assertEqual(result["status"], "would-publish")
        self.assertEqual(result["active_policy"], "policy-2")
        self.assertEqual(result["next_policy"], "policy-3")
        self.assertEqual(result["next_mission"], "coverage-mission-version:ws-fixture:3")
        self.assertEqual(len(result["publishes"]), 3)
        self.assertTrue(result["lane_precondition_now"].startswith("refused"))
        self.assertEqual(result["lane_precondition_after"], "ok")
        self.assertEqual(result["diff"]["policy.research_plan_auto_start"], {
            "before": None, "after": {"enabled": True, "rules": LEGACY_POLICY_18_RULES}})
        # Read-only: the Core is untouched.
        self.assertNotIn("research_plan_auto_start", self._policy(self.base)["policy"])

    def test_rehearsal_publishes_only_the_missing_block_and_the_lane_passes(self):
        before = self._policy(self.base)
        target = Path(self.temp.name) / "rehearsal"
        result = rehearse(self.base, target, actor=OWNER, rules=[])
        self.assertEqual(result["lane_precondition_after"], "ok")
        self.assertTrue(result["mission_binds_new_constitution"])
        self.assertEqual(result["chain"]["mandate"]["status"], "unchanged")
        self.assertEqual(result["active_research_plan_auto_start"],
                         {"enabled": True, "rules": LEGACY_POLICY_18_RULES})
        after = self._policy(target)
        # Nothing else in the policy moved.
        self.assertEqual(
            {k: v for k, v in after["policy"].items() if k != "research_plan_auto_start"},
            before["policy"])
        # Nor in the mission, other than its constitution binding.
        strip = lambda m: {k: v for k, v in m.items() if k not in {  # noqa: E731
            "id", "version", "content_hash", "bindings", "prior_version_ref",
            "created_at", "actor_ref", "idempotency_key", "status"}}
        self.assertEqual(strip(after["mission"]), strip(before["mission"]))
        self.assertEqual(after["mission"]["bindings"]["mandate_version"],
                         before["mission"]["bindings"]["mandate_version"])
        # A second run is a report, and publishing again refuses.
        again = plan(target, rules=[])
        self.assertEqual((again["status"], again["publishes"]), ("already-signed", []))
        self.assertEqual(again["lane_precondition_now"], "ok")
        with self.assertRaisesRegex(PlanError, "already lists"):
            rehearse(target, Path(self.temp.name) / "second", actor=OWNER, rules=[])

    def test_only_the_missing_rule_is_added(self):
        target = Path(self.temp.name) / "quarterly-only"
        rehearse(self.base, target, actor=OWNER,
                 rules=[PLAN_COMPANY_FACTS_AUTO_START_RULE_REF])
        result = plan(target, rules=[])
        self.assertEqual(result["active_rules"], [PLAN_COMPANY_FACTS_AUTO_START_RULE_REF])
        self.assertEqual(result["rules_to_add"], [PLAN_COMPANY_FACTS_ANNUAL_AUTO_START_RULE_REF])
        self.assertEqual(result["diff"]["policy.research_plan_auto_start"]["after"]["rules"],
                         LEGACY_POLICY_18_RULES)

    def test_an_unknown_rule_is_refused(self):
        with self.assertRaisesRegex(PlanError, "does not implement"):
            plan(self.base, rules=["research-plan-auto-start:whatever:v1"])

    def test_cli_dry_run_writes_nothing_and_apply_needs_a_human(self):
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            self.assertEqual(main(["--state-dir", str(self.base)]), 0)
        self.assertEqual(json.loads(stdout.getvalue())["status"], "would-publish")
        self.assertNotIn("research_plan_auto_start", self._policy(self.base)["policy"])
        with self.assertRaises(PlanError):
            main(["--state-dir", str(self.base), "--apply"])
        with self.assertRaises(PlanError):
            main(["--state-dir", str(self.base), "--apply", "--actor", "automation:x"])


if __name__ == "__main__":
    unittest.main()
