import importlib.util
import json
import unittest
from datetime import datetime, timezone
from pathlib import Path

from dalton_core.coverage_mission import CoverageMissionAuthority
from dalton_core.model_router import ModelRouter
from dalton_core.observability import ObservabilityStore
from dalton_core.store import DaltonStore, canonical_json, content_hash
from dalton_core.thesis_impact_budget import (
    ThesisImpactBudgetStore, ThesisImpactDayBudgetExceeded,
)
from tests.test_model_router import MutableClock, policy, profile, route_args, work_order
from tests.test_research_planner import NOW, directive, plan_from_response, response, state


SCRIPT = Path(__file__).parents[1] / "scripts" / "audit_postdeploy_products.py"
SPEC = importlib.util.spec_from_file_location("audit_postdeploy_products", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
audit = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(audit)


class ResearchPlanAuditTests(unittest.TestCase):
    def setUp(self):
        self.store = DaltonStore()
        self.authority = CoverageMissionAuthority(self.store)
        self.work_ref = "work:cockpit-plan:test"
        self.plan = plan_from_response(
            state(), response(directive()), created_at=NOW)
        self.authority.record_research_plan(
            self.plan, decided_by="actor:test", work_order_ref=self.work_ref)
        self.row = self.store.connection.execute(
            "SELECT * FROM coverage_mission_research_plans WHERE work_order_ref=?",
            (self.work_ref,),
        ).fetchone()

    def tearDown(self):
        self.store.close()

    def test_real_coverage_mission_plan_replays_without_embedded_id(self):
        wire = audit.exact_research_plan(self.row)
        self.assertNotIn("id", wire)
        self.assertEqual(wire, self.plan)
        row, resolved = audit.stored_plan_for_work(
            self.store.connection, self.work_ref)
        self.assertEqual(row["plan_id"], self.row["plan_id"])
        self.assertEqual(resolved, self.plan)

    def _mutated_row(self, *, column=None, value=None, wire_change=None):
        row = dict(self.row)
        if wire_change is not None:
            wire = json.loads(row["plan_json"])
            wire_change(wire)
            body = dict(wire)
            body.pop("content_hash", None)
            wire["content_hash"] = content_hash(body)
            row["plan_json"] = canonical_json(wire)
            row["content_hash"] = wire["content_hash"]
        if column is not None:
            row[column] = value
        return row

    def test_identity_columns_hash_and_task_drift_are_rejected(self):
        mutations = {
            "plan id": self._mutated_row(column="plan_id", value="mission-research-plan:foreign"),
            "mission": self._mutated_row(
                column="mission_version_ref", value="coverage-mission-version:foreign:1"),
            "assessment column": self._mutated_row(column="assessment", value="foreign"),
            "hash column": self._mutated_row(column="content_hash", value="f" * 64),
            "task": self._mutated_row(
                wire_change=lambda wire: wire.__setitem__("task_ref", "task:foreign:0.1")),
        }
        for label, row in mutations.items():
            with self.subTest(label=label), self.assertRaisesRegex(
                    RuntimeError, "formal research plan authority differs"):
                audit.exact_research_plan(row)

    def test_null_sufficiency_cannot_alias_an_empty_list(self):
        row = self._mutated_row(
            wire_change=lambda wire: wire.__setitem__("sufficiency", None))
        self.assertEqual(row["sufficiency_json"], "[]")
        with self.assertRaisesRegex(RuntimeError, "formal research plan authority differs"):
            audit.exact_research_plan(row)

    def test_one_work_order_cannot_claim_multiple_stored_plans(self):
        moved = state(figures_by_company={})
        second = plan_from_response(moved, response(), created_at=NOW)
        self.authority.record_research_plan(
            second, decided_by="actor:test", work_order_ref=self.work_ref)
        with self.assertRaisesRegex(RuntimeError, "multiple stored plans"):
            audit.stored_plan_for_work(self.store.connection, self.work_ref)

    def test_one_work_order_cannot_exist_in_both_schedulers(self):
        seen = set()
        audit.claim_planner_work(seen, self.work_ref)
        with self.assertRaisesRegex(RuntimeError, "both schedulers"):
            audit.claim_planner_work(seen, self.work_ref)

    def test_stored_plan_is_distinct_from_scheduler_formal_authority(self):
        plan = {"plan_ref": self.row["plan_id"]}
        self.assertEqual(
            audit.planner_classification(plan, []),
            "stored_plan_without_succeeded_formal")
        self.assertEqual(
            audit.planner_classification(plan, [{"terminal_state": "succeeded"}]),
            "stored_plan_with_succeeded_formal")
        self.assertEqual(
            audit.planner_classification(None, [{"terminal_state": "failed"}]),
            "planner_terminal_failed")

    def test_completed_historical_reentry_precedes_stale_due_marker(self):
        classification = audit.directed_classification(
            promotion=None, outcome=None, fresh_links=[], fresh_tickets=[],
            controlled_reentry_markers=[{"record": {"kind": "exact_scheduler_replay"}}],
            completed_controlled_reentry=True,
            latest_recovery={"retry_at": "2026-09-12T00:00:00+00:00"},
            works=[{"work_order_ref": "work:test"}],
            now=datetime(2026, 9, 12, 1, tzinfo=timezone.utc),
        )
        self.assertEqual(classification,
                         "controlled_reentry_completed_without_candidate")

    def test_paid_contract_barrier_is_not_unknown_send_state(self):
        classification = audit.directed_classification(
            promotion=None, outcome=None, fresh_links=[], fresh_tickets=[],
            controlled_reentry_markers=[], completed_controlled_reentry=False,
            latest_recovery={
                "reason": "paid_send_output_contract_failed", "retry_at": None,
            },
            works=[{"work_order_ref": "work:test"}],
            now=datetime(2026, 9, 12, 1, tzinfo=timezone.utc),
        )
        self.assertEqual(classification,
                         "proved_paid_output_contract_terminal_barrier")


class BudgetNoSendAuditTests(unittest.TestCase):
    def setUp(self):
        self.core = DaltonStore()
        self.addCleanup(self.core.close)
        ObservabilityStore(self.core)
        self.clock = MutableClock()
        self.router = ModelRouter(":memory:", clock=self.clock)
        self.addCleanup(self.router.close)
        self.router.register_policy(policy())
        self.router.register_profile(profile("audit"))
        self.work = work_order()
        self.route = self.router.route(self.work, **route_args())["decision"]
        self.budget = ThesisImpactBudgetStore(clock=self.clock)
        self.addCleanup(self.budget.close)
        self.budget.register_policy(policy_version_id="budget:audit:1", day_cap_micros=100_000)
        self.formal = {
            "terminal_state": "failed", "attempt_number": 1,
            "invocation_ref": "invocation:not-started:test", "usage_refs": [],
            "error": {"code": "BUDGET_REFUSED"},
            "metadata": {"route_decision_ref": self.route["id"]},
        }

    def admit(self, reserved, *, phase="assessment"):
        return self.budget.admit(
            policy_version_id="budget:audit:1", day="2026-08-14",
            work_order_ref=self.work["id"], attempt_number=1, phase=phase,
            route_decision_ref=self.route["id"], reserved_micros=reserved,
        )

    def reject(self):
        with self.assertRaises(ThesisImpactDayBudgetExceeded):
            self.admit(200_000)

    def proof(self, *, expected_phase="assessment", **updates):
        return audit.provider_send_proof(
            self.core.connection, self.router.connection, self.budget.connection,
            work_ref=self.work["id"], work_hash=self.route["work_order_hash"],
            formal={**self.formal, **updates},
            expected_phase=expected_phase,
        )

    def test_exact_local_admission_refusal_proves_no_send(self):
        self.reject()
        proof = self.proof()
        self.assertEqual(proof["classification"], "atomic_budget_refusal_no_send")
        self.assertFalse(proof["provider_send_proven"])

    def test_synthetic_not_started_after_admission_does_not_prove_no_send(self):
        admission = self.admit(50_000)
        self.budget.settle(admission["admission_id"], actual_micros=50_000)
        proof = self.proof()
        self.assertEqual(proof["classification"], "no_successful_provider_response_proven")
        self.assertEqual(proof["budget"]["admissions"][0]["settlements"][0]["actual_micros"], 50_000)
        self.assertFalse(proof["actual_cost_settled"])

    def test_rejection_must_match_formal_attempt_route_error_and_empty_usage(self):
        self.reject()
        variants = (
            {"attempt_number": 2},
            {"metadata": {"route_decision_ref": "route:foreign"}},
            {"error": {"code": "PROVIDER_BUDGET_EXCEEDED"}},
            {"usage_refs": ["usage:returned"]},
            {"terminal_state": "retryable"},
        )
        for variant in variants:
            with self.subTest(variant=variant):
                self.assertNotEqual(self.proof(**variant)["classification"],
                                    "atomic_budget_refusal_no_send")

    def test_rejection_sql_identity_drift_is_not_no_send_authority(self):
        self.reject()
        # Simulate a damaged projection after bypassing the local fixture's
        # append-only guard; audit must still reject its canonical mismatch.
        self.budget.connection.execute("DROP TRIGGER thesis_impact_day_rejections_no_update")
        self.budget.connection.execute(
            "UPDATE thesis_impact_day_rejections SET attempt_number=2")
        with self.assertRaisesRegex(RuntimeError, "budget rejection SQL projection differs"):
            self.proof(attempt_number=2)

    def test_missing_usage_authority_cannot_prove_absence(self):
        self.reject()
        self.core.connection.execute("DROP TABLE observability_usage_entries")
        self.assertNotEqual(self.proof()["classification"], "atomic_budget_refusal_no_send")

    def test_rejection_for_another_phase_cannot_prove_assessment_no_send(self):
        with self.assertRaises(ThesisImpactDayBudgetExceeded):
            self.admit(200_000, phase="verification")
        self.assertNotEqual(
            self.proof()["classification"], "atomic_budget_refusal_no_send"
        )

    def test_verification_rejection_requires_explicit_verification_phase(self):
        with self.assertRaises(ThesisImpactDayBudgetExceeded):
            self.admit(200_000, phase="verification")
        self.assertEqual(
            self.proof(expected_phase="verification")["classification"],
            "atomic_budget_refusal_no_send",
        )
        self.assertNotEqual(
            self.proof(expected_phase=None)["classification"],
            "atomic_budget_refusal_no_send",
        )


if __name__ == "__main__":
    unittest.main()
