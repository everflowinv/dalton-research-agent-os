"""Signing an auto-commit rule must work in the environment that needs it.

The workspace install had 26 finished document reviews held because its first
governance policy carries no ``research_candidate_auto_commit`` block at all,
and the script that fixed exactly this on the legacy install could not be
pointed at it: mission ref, constitution ref and state directory were all
constants.  These tests pin the generalised script to a Core whose mission,
constitution and policy names share nothing with the legacy ones, and pin the
other half of the answer -- that a workspace created from now on does not need
the script, because its first mission signs the qualitative rule itself.
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
from dalton_core.research_auto_commit import (
    DOCUMENT_QUALITATIVE_RULE_REF,
    MISSION_VERIFIED_FIGURE_RULE_REF,
    RULE_REF as FILING_COUNT_RULE_REF,
    policy_lists_document_rule,
)
from dalton_core.store import DaltonStore
from dalton_core.workspace_mission_setup import ensure_first_mission_auto_commit_policy
from scripts.sign_auto_commit_rules import (
    PlanError, SIGNABLE_RULE_REFS, main, plan, read_current, rehearse,
)
from tests.p9a_fixtures import bootstrap_method_authorities, mission_params

# Deliberately not ``us-it-services``: the script must read the mission from
# the pointer, not know it.
MISSION_REF = "coverage-mission:ws-fixture"
OWNER = "human:fixture-owner"


class AutoCommitRuleSigningTests(unittest.TestCase):
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
            # The policy a blank Core installs: no auto-commit block at all,
            # which is exactly the workspace's situation.
            self.assertIsNone(store.active_policy_version().to_dict()["policy"].get(
                "research_candidate_auto_commit"))
        finally:
            store.close()

    def _active_rules(self, state_dir: Path) -> list[str] | None:
        connection = sqlite3.connect(
            f"file:{state_dir / 'core.sqlite'}?mode=ro", uri=True)
        try:
            current = read_current(connection)
        finally:
            connection.close()
        rule = current["policy"].get("research_candidate_auto_commit")
        return None if rule is None else list(rule["rules"])

    def test_plan_shows_the_exact_before_after_and_the_whole_cascade(self):
        result = plan(self.state_dir, rules=[DOCUMENT_QUALITATIVE_RULE_REF])
        self.assertEqual(result["status"], "would-publish")
        self.assertEqual(result["mission_ref"], MISSION_REF)
        self.assertEqual(result["active_policy"], "policy-1")
        self.assertEqual(result["rules_to_add"], [DOCUMENT_QUALITATIVE_RULE_REF])
        diff = result["diff"]["policy.research_candidate_auto_commit"]
        self.assertIsNone(diff["before"])
        self.assertEqual(diff["after"], {
            "enabled": True, "rules": [DOCUMENT_QUALITATIVE_RULE_REF],
            "max_records": 20})
        # Three records, named before anything is written; the constitution
        # keeps its own ref family, which is not the mission's.
        self.assertEqual(result["publishes"], [
            "policy-2",
            "constitution-version:us-it-services:2",
            "coverage-mission-version:ws-fixture:2",
        ])
        self.assertIn(DOCUMENT_QUALITATIVE_RULE_REF, result["change_reason"])
        # A dry run publishes nothing.
        self.assertIsNone(self._active_rules(self.state_dir))

    def test_default_is_every_signable_rule_the_policy_lacks(self):
        result = plan(self.state_dir, rules=[])
        self.assertEqual(result["rules_to_add"], list(SIGNABLE_RULE_REFS))
        # The filing-count rule is only valid as the entire rule set, so it is
        # never swept in by the default.
        self.assertNotIn(FILING_COUNT_RULE_REF, result["rules_to_add"])

    def _rehearsed(self, name: str = "rehearsal") -> Path:
        """Publish the qualitative rule on a copy, and hand back that Core."""

        target = Path(self.temp.name) / name
        result = rehearse(self.state_dir, target, actor=OWNER,
                          rules=[DOCUMENT_QUALITATIVE_RULE_REF])
        self.assertEqual(result["mode"], "rehearsal")
        self.assertEqual(result["active_rules"], [DOCUMENT_QUALITATIVE_RULE_REF])
        self.assertTrue(result["policy_lists_document_rule"])
        self.assertTrue(result["mission_binds_new_constitution"])
        self.assertEqual(result["chain"]["policy"]["ref"], "policy-2")
        self.assertEqual(result["chain"]["mandate"]["status"], "unchanged")
        self.assertEqual(result["chain"]["mission"]["ref"],
                         "coverage-mission-version:ws-fixture:2")
        # The source Core is a copy's source and nothing else.
        self.assertIsNone(self._active_rules(self.state_dir))
        self.assertEqual(self._active_rules(target), [DOCUMENT_QUALITATIVE_RULE_REF])
        return target

    def test_rehearsal_publishes_the_cascade_and_the_evaluator_accepts_it(self):
        self._rehearsed()

    def test_planning_a_rule_already_signed_is_a_report_not_a_failure(self):
        target = self._rehearsed()
        again = plan(target, rules=[DOCUMENT_QUALITATIVE_RULE_REF])
        self.assertEqual(again["status"], "already-signed")
        self.assertEqual(again["rules_to_add"], [])
        self.assertEqual(again["publishes"], [])
        self.assertEqual(again["already_listed"], [DOCUMENT_QUALITATIVE_RULE_REF])
        diff = again["diff"]["policy.research_candidate_auto_commit"]
        self.assertEqual(diff["before"], diff["after"])
        # Publishing, however, refuses rather than forking the chain for nothing.
        with self.assertRaisesRegex(PlanError, "nothing to do"):
            rehearse(target, Path(self.temp.name) / "second", actor=OWNER,
                     rules=[DOCUMENT_QUALITATIVE_RULE_REF])
        # And the default still finds the rules that are genuinely missing.
        self.assertEqual(
            plan(target, rules=[])["rules_to_add"],
            [rule for rule in SIGNABLE_RULE_REFS
             if rule != DOCUMENT_QUALITATIVE_RULE_REF])

    def test_no_only_missing_refuses_a_rule_the_policy_already_lists(self):
        target = self._rehearsed()
        with self.assertRaisesRegex(PlanError, "already lists"):
            plan(target, rules=[DOCUMENT_QUALITATIVE_RULE_REF], only_missing=False)

    def test_a_rule_this_build_does_not_implement_is_refused(self):
        with self.assertRaisesRegex(PlanError, "does not implement"):
            plan(self.state_dir, rules=["research-auto-commit:whatever:v1"])

    def test_the_filing_count_rule_is_never_mixed_into_a_rule_set(self):
        # It is valid only as the whole set: ``_policy_rule`` rejects every
        # candidate, including the ones that used to pass, otherwise.
        with self.assertRaisesRegex(PlanError, "entire rule set"):
            plan(self.state_dir,
                 rules=[FILING_COUNT_RULE_REF, DOCUMENT_QUALITATIVE_RULE_REF])
        target = self._rehearsed()
        with self.assertRaisesRegex(PlanError, "entire rule set"):
            plan(target, rules=[FILING_COUNT_RULE_REF])

    def test_cli_dry_run_prints_the_plan_and_writes_nothing(self):
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            self.assertEqual(main([
                "--state-dir", str(self.state_dir),
                "--rule", MISSION_VERIFIED_FIGURE_RULE_REF]), 0)
        result = json.loads(stdout.getvalue())
        self.assertEqual(result["mode"], "dry-run")
        self.assertEqual(result["rules_to_add"], [MISSION_VERIFIED_FIGURE_RULE_REF])
        self.assertIsNone(self._active_rules(self.state_dir))

    def test_mission_ref_is_only_needed_when_the_core_is_ambiguous(self):
        connection = sqlite3.connect(
            f"file:{self.state_dir / 'core.sqlite'}?mode=ro", uri=True)
        try:
            self.assertEqual(read_current(connection)["mission_ref"], MISSION_REF)
            with self.assertRaisesRegex(PlanError, "no active mission named"):
                read_current(connection, mission_ref="coverage-mission:absent")
        finally:
            connection.close()


class FirstMissionPolicyTests(unittest.TestCase):
    """A workspace created from now on should not need the script at all."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = DaltonStore(str(Path(self.temp.name) / "core.sqlite"))
        self.addCleanup(self.store.close)

    def test_the_first_mission_policy_names_the_document_rule(self):
        before = self.store.active_policy_version().to_dict()
        self.assertIsNone(before["policy"].get("research_candidate_auto_commit"))
        binding = ensure_first_mission_auto_commit_policy(self.store, actor_ref=OWNER)
        active = self.store.active_policy_version().to_dict()
        self.assertEqual(binding, {"ref": active["id"], "hash": active["content_hash"]})
        self.assertEqual(active["policy"]["research_candidate_auto_commit"], {
            "enabled": True, "rules": [DOCUMENT_QUALITATIVE_RULE_REF],
            "max_records": 20})
        # The gate the held reviews were stuck behind now opens.
        self.assertTrue(policy_lists_document_rule(active))
        # Nothing else in the policy moved.
        self.assertEqual(
            {k: v for k, v in active["policy"].items()
             if k != "research_candidate_auto_commit"},
            dict(before["policy"]))
        self.assertEqual(active["independence_predicates"],
                         before["independence_predicates"])
        self.assertEqual(active["prior_version_ref"], before["id"])
        self.assertEqual(active["actor_ref"], OWNER)

    def test_it_is_idempotent_and_does_not_fork_the_policy_chain(self):
        first = ensure_first_mission_auto_commit_policy(self.store, actor_ref=OWNER)
        second = ensure_first_mission_auto_commit_policy(self.store, actor_ref=OWNER)
        self.assertEqual(first, second)
        rows = self.store.connection.execute(
            "SELECT COUNT(*) FROM governance_policy_versions").fetchone()[0]
        self.assertEqual(rows, 2)


if __name__ == "__main__":
    unittest.main()
