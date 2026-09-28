"""The runtime governance baseline every new workspace is created with.

Reference: legacy ``policy-18``.  Its runtime rules are the baseline; its
weekly-brief plan binding is legacy's research content and must never be
copied.  And a cockpit budget edit must not drop the independence gate the
way legacy policy-14 and ws-7d policy-2 did.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from dalton_core.research_auto_commit import (
    DOCUMENT_QUALITATIVE_RULE_REF,
    RULE_REF as FILING_COUNT_RULE_REF,
)
from dalton_core.store import DaltonStore
from dalton_core.workspace_governance_baseline import (
    BASELINE_AUTO_COMMIT_RULES,
    BASELINE_PLAN_AUTO_START_RULES,
    baseline_policy_body,
    closed_research_budget,
    governance_baseline_checks,
    research_specific_policy_keys,
)

FY_MINUS_9M = "research-auto-commit:sec-statement-line-growth-fy-minus-9m:v1"

#: Legacy's active policy on 2026-09-27, exported read-only.
LEGACY_POLICY_18 = {
    "allowed_verdicts": ["pass"], "independence_predicates": [], "required_verification": True,
    "research_budget": {"max_alphaengine_calls_24h": 130, "max_daily_cost_usd": 500,
                        "max_daily_document_reads": 2000, "max_daily_paid_calls": 100000,
                        "pools_enforcement": "off"},
    "research_candidate_auto_commit": {"enabled": True, "max_records": 20, "rules": [
        "research-auto-commit:sec-public-company-facts-growth:v1",
        "research-auto-commit:sec-public-company-facts-growth-annual:v1",
        "research-auto-commit:mission-document-qualitative:v1",
        "research-auto-commit:mission-verified-figure:v1",
        "research-auto-commit:sec-statement-line:v1"]},
    "research_plan_auto_start": {"enabled": True, "rules": [
        "research-plan-auto-start:sec-public-company-facts:v1",
        "research-plan-auto-start:sec-public-company-facts-annual:v1"]},
    "weekly_brief_auto_publish": {"allowed_plan_bindings": [{
        "plan_hash": "c" * 64, "plan_ref": "weekly-brief-plan:us-it-services:v4"}],
        "enabled": True, "max_issues_per_week": 1,
        "rule_ref": "weekly-brief-auto-publish:scheduled-exact-plan:v1"},
}
BOOTSTRAP = {"allowed_verdicts": ["pass"], "required_verification": True}
PREDICATE = {"left_path": "producer.model_family", "operator": "ne",
             "right_path": "verifier.model_family"}
BUDGET = {"max_daily_paid_calls": 100, "max_daily_cost_usd": 100.0,
          "max_alphaengine_calls_24h": 50, "max_alphaengine_probe_calls_24h": 10}


class BaselineBodyTests(unittest.TestCase):
    def test_legacy_policy_18_is_the_baseline_but_for_the_fy_minus_9m_rule(self):
        # policy-18 predates the FY - 9M rule (2026-09-28): the baseline adds
        # exactly that rule, after the ones legacy already lists, and nothing else.
        body, added = baseline_policy_body(LEGACY_POLICY_18, mission_budget=BUDGET)
        self.assertEqual(added, [f"research_candidate_auto_commit:{FY_MINUS_9M}"])
        self.assertEqual(body["research_candidate_auto_commit"]["rules"],
                         [*LEGACY_POLICY_18["research_candidate_auto_commit"]["rules"],
                          FY_MINUS_9M])
        self.assertEqual({key: value for key, value in body.items()
                          if key != "research_candidate_auto_commit"},
                         {key: value for key, value in LEGACY_POLICY_18.items()
                          if key != "research_candidate_auto_commit"})
        self.assertEqual(
            LEGACY_POLICY_18["research_candidate_auto_commit"]["rules"] + [FY_MINUS_9M],
            list(BASELINE_AUTO_COMMIT_RULES))
        again, added_again = baseline_policy_body(body, mission_budget=BUDGET)
        self.assertEqual((again, added_again), (body, []))
        self.assertEqual(LEGACY_POLICY_18["research_plan_auto_start"]["rules"],
                         list(BASELINE_PLAN_AUTO_START_RULES))

    def test_a_bootstrap_policy_gets_the_runtime_baseline_and_no_research_content(self):
        body, added = baseline_policy_body(
            BOOTSTRAP, mission_budget=BUDGET, independence_predicates=[PREDICATE])
        self.assertEqual(body["research_candidate_auto_commit"]["rules"],
                         list(BASELINE_AUTO_COMMIT_RULES))
        self.assertEqual(body["research_plan_auto_start"],
                         {"enabled": True, "rules": list(BASELINE_PLAN_AUTO_START_RULES)})
        self.assertEqual(body["research_budget"], {
            "max_alphaengine_calls_24h": 50, "max_daily_cost_usd": 100.0,
            "max_daily_paid_calls": 100})
        self.assertEqual(body["independence_predicates"], [PREDICATE])
        self.assertNotIn("weekly_brief_auto_publish", body)
        self.assertIn("research_budget", added)
        self.assertEqual(len(added), 6 + 2 + 1)
        self.assertIn(FY_MINUS_9M, body["research_candidate_auto_commit"]["rules"])

    def test_what_the_owner_decided_is_kept(self):
        disabled = {**BOOTSTRAP, "research_plan_auto_start": {"enabled": False, "rules": []},
                    "research_candidate_auto_commit": {
                        "enabled": True, "rules": [FILING_COUNT_RULE_REF], "max_records": 5}}
        body, added = baseline_policy_body(disabled, mission_budget=BUDGET)
        self.assertEqual(body["research_plan_auto_start"], {"enabled": False, "rules": []})
        # The exclusive filing-count rule set cannot be extended.
        self.assertEqual(body["research_candidate_auto_commit"]["rules"], [FILING_COUNT_RULE_REF])
        self.assertEqual(added, ["research_budget"])

    def test_a_closed_budget_is_three_caps(self):
        self.assertEqual(sorted(closed_research_budget(BUDGET)),
                         ["max_alphaengine_calls_24h", "max_daily_cost_usd",
                          "max_daily_paid_calls"])
        with self.assertRaises(ValueError):
            closed_research_budget({"max_daily_paid_calls": 1})

    def test_checks_name_every_gap_and_legacy_content_is_not_one(self):
        rows = governance_baseline_checks({"id": "policy-4", "policy": {
            **BOOTSTRAP, "research_candidate_auto_commit": {
                "enabled": True, "max_records": 20, "rules": [DOCUMENT_QUALITATIVE_RULE_REF]}},
            "independence_predicates": []}, mission={"budget": BUDGET})
        gaps = {row["check"] for row in rows if row["status"] == "gap"}
        self.assertIn("policy.research_plan_auto_start:"
                      "research-plan-auto-start:sec-public-company-facts:v1", gaps)
        self.assertIn("policy.research_budget", gaps)
        self.assertNotIn("policy.research_candidate_auto_commit:"
                         "research-auto-commit:mission-document-qualitative:v1", gaps)
        drift = [row for row in rows if row["check"] == "policy.independence_predicates"]
        self.assertEqual(drift[0]["status"], "drift")
        legacy_rows = governance_baseline_checks(
            {"id": "policy-18", "policy": LEGACY_POLICY_18}, mission={"budget": BUDGET})
        # policy-18 predates FY - 9M: that one rule is its only gap, and the
        # row says which lane needs it.
        legacy_gaps = [row for row in legacy_rows if row["status"] != "ok"
                       and row["check"] != "policy.independence_predicates"]
        self.assertEqual([row["check"] for row in legacy_gaps],
                         [f"policy.research_candidate_auto_commit:{FY_MINUS_9M}"])
        self.assertIn("FY - 9M", legacy_gaps[0]["detail"])
        self.assertEqual(research_specific_policy_keys(LEGACY_POLICY_18),
                         ["weekly_brief_auto_publish"])

    def test_a_mission_asking_more_than_the_policy_budget_is_a_gap(self):
        rows = governance_baseline_checks(
            {"id": "policy-18", "policy": LEGACY_POLICY_18},
            mission={"budget": {**BUDGET, "max_daily_paid_calls": 10 ** 9}})
        [row] = [row for row in rows if row["check"] == "policy.research_budget"]
        self.assertEqual(row["status"], "gap")


class BudgetEditKeepsIndependenceTests(unittest.TestCase):
    """The cockpit budget editor published ``policy`` without its predicates."""

    def test_the_owner_budget_operation_carries_the_predicates(self):
        from dalton_core.writer_server import WriterServer

        server = object.__new__(WriterServer)
        old_budget = {"max_daily_paid_calls": 1, "max_daily_cost_usd": 1,
                      "max_alphaengine_calls_24h": 1}
        mission = {"mission_ref": "coverage-mission:test", "id": "coverage-mission-version:test:1",
                   "version": 1, "content_hash": "a" * 64, "budget": old_budget,
                   "bindings": {"mandate_version": {"ref": "mandate-version:test:1"},
                                "constitution_version": {"ref": "constitution-version:test:1"}},
                   **{k: [] for k in ("universe", "research_questions", "deliverables",
                                      "source_plan")},
                   "title": "t", "objective": "o", "industry_ref": "industry:test",
                   "autonomy": {}}
        made: dict = {}
        constitution = {"id": "constitution-version:test:1", "version": 1,
                        "constitution_ref": "constitution:test", "industry_ref": "industry:test",
                        "title": "t", "bindings": {
                            "mandate_version": {}, "driver_pack_version": {},
                            "governance_policy_version": {}, "doctrine_pack_version": None,
                            "weekly_brief_plan": None}, "method": {}}
        server._coverage_mission = SimpleNamespace(
            active_mission=lambda ref: mission,
            create_mission=lambda ref, **kw: made.setdefault(
                "mission", {"id": "coverage-mission-version:test:2", **kw}))
        server._agenda = SimpleNamespace(
            mandate_version=lambda ref: {"id": ref, "version": 1, "mandate_ref": "mandate:test",
                                         "objective": "o", "scope_refs": ["scope:test"],
                                         "constraints": {"research_budget": old_budget},
                                         "success_criteria": {}},
            active_mandates=lambda: [],
            create_mandate=lambda ref, **kw: made.setdefault(
                "mandate", {"id": "mandate-version:test:2", "content_hash": "b" * 64}))
        server._research_constitution = SimpleNamespace(
            constitution=lambda ref: constitution, active_constitution=lambda ref: constitution,
            publish_constitution=lambda ref, **kw: made.setdefault(
                "constitution", {"id": "constitution-version:test:2", "content_hash": "c" * 64}))
        server._store = SimpleNamespace(
            active_policy_version=lambda: SimpleNamespace(to_dict=lambda: {
                "id": "policy-1", "version": 1, "policy_ref": "commit-gate",
                "policy": {"allowed_verdicts": ["pass"], "required_verification": True,
                           "research_budget": old_budget},
                "independence_predicates": [PREDICATE]}),
            create_policy=lambda body, **kw: made.setdefault(
                "policy", {"id": "policy-2", "content_hash": "d" * 64, "body": body}))
        server._workspace = None
        server.db_path = "/tmp/dalton-budget-test/core.db"
        server._planner_model_config = None
        server._document_extraction_model_config = None
        day_module = SimpleNamespace(synchronize_day_budget_policy=lambda *a, **kw: {
            "policy_version_id": "thesis-impact-day-budget-policy:production:2"})
        with patch.dict(sys.modules, {"dalton_core.day_budget_configuration": day_module}):
            server._op_set_research_budget_authority_chain({
                "mission_ref": "coverage-mission:test",
                "budget": {"max_daily_paid_calls": 2, "max_daily_cost_usd": 2,
                           "max_alphaengine_calls_24h": 2},
                "expected_mission_hash": "a" * 64, "actor_ref": "human:owner"})
        self.assertEqual(made["policy"]["body"]["independence_predicates"], [PREDICATE])

    def test_a_real_store_keeps_them_through_the_same_body(self):
        # The store reads predicates from the body it is handed; this is the
        # shape the operation now hands it.
        with tempfile.TemporaryDirectory() as directory:
            store = DaltonStore(str(Path(directory) / "core.sqlite"))
            try:
                before = store.active_policy_version().to_dict()
                body = {**before["policy"], "research_budget": {
                    "max_daily_paid_calls": 2, "max_daily_cost_usd": 2,
                    "max_alphaengine_calls_24h": 2},
                    "independence_predicates": before["independence_predicates"]}
                store.create_policy(body, policy_version_id="policy-2", version_number=2,
                                    activate=True, prior_version_ref=before["id"],
                                    actor_ref="human:owner")
                after = store.active_policy_version().to_dict()
                self.assertEqual(after["independence_predicates"], [PREDICATE])
            finally:
                store.close()


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
