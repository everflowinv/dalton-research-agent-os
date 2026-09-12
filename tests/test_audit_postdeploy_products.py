import importlib.util
import json
import unittest
from datetime import datetime, timezone
from pathlib import Path

from dalton_core.coverage_mission import CoverageMissionAuthority
from dalton_core.model_router import ModelRouter
from dalton_core.observability import ObservabilityStore
from dalton_core.scheduler import Scheduler
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


class ModelSpecTaskContractAuditTests(unittest.TestCase):
    def spec(self, task_hash="a" * 64):
        spec = {
            "company_ref": "company:sec-cik:0000051143",
            "state_hash": "b" * 64,
            "task_hash": task_hash,
        }
        spec["spec_id"] = "company-model-spec:" + content_hash(spec)[:32]
        return spec

    def authority(self, spec, *, task_hash=None):
        identity = {
            "schema_version": "company-model-spec-request-0.1",
            "state_hash": spec["state_hash"],
            "task_hash": task_hash or spec["task_hash"],
            "structured_output_repair": {"max_attempts": 0},
        }
        return {
            "purpose": "model_spec",
            "request_id": content_hash(identity)[:32],
            "model_spec_request_identity_present": True,
            "model_spec_request_identity": identity,
            "structured_output_repair_present": False,
            "structured_output_repair": None,
            "producer_route_decision_refs": [],
            "work_authority_verified": True,
        }

    def repair_authority(self, spec):
        proof = {
            "work_order_ref": "work:root", "work_order_hash": "1" * 64,
            "result_envelope_ref": "result:root", "result_envelope_hash": "2" * 64,
            "invocation_ref": "invocation:root", "route_decision_ref": "route:root",
        }
        binding = {
            "schema_version": "company-model-spec-repair-binding-0.1",
            "root_original": proof, "repair_parent": dict(proof),
            "original_text_sha256": "3" * 64, "parent_text_sha256": "4" * 64,
            "state_hash": spec["state_hash"], "task_hash": spec["task_hash"],
            "validation_error": {"code": "format", "message": "invalid JSON"},
            "repair_contract_ref": "contract:company-model-spec-structured-output-repair:0.1",
            "repair_contract_hash": "5" * 64, "repair_prompt_sha256": "6" * 64,
            "repair_config": {"max_attempts": 1}, "repair_number": 1,
        }
        return {
            "purpose": "model_spec", "model_spec_request_identity_present": False,
            "model_spec_request_identity": None,
            "structured_output_repair_present": True,
            "structured_output_repair": binding,
            "request_id": "model-spec-repair:" + content_hash(binding)[:32],
            "producer_route_decision_refs": [], "work_authority_verified": True,
        }

    def test_new_and_historical_hashes_are_reported_from_exact_work(self):
        for task_hash in ("a" * 64, "c" * 64):
            with self.subTest(task_hash=task_hash):
                spec = self.spec(task_hash)
                proof = audit.model_spec_task_contract(spec, self.authority(spec))
                self.assertEqual(proof["status"], "verified_exact_work_identity")
                self.assertEqual(proof["task_hash"], task_hash)
                self.assertEqual(proof["request_id"], content_hash(
                    self.authority(spec)["model_spec_request_identity"]
                )[:32])

    def test_missing_historical_work_proof_remains_unknown(self):
        spec = self.spec()
        self.assertEqual(
            audit.model_spec_task_contract(spec, None)["status"],
            "unknown_legacy_missing_work_identity",
        )
        self.assertEqual(
            audit.model_spec_task_contract(spec, {
                "purpose": "model_spec", "model_spec_request_identity_present": False,
                "model_spec_request_identity": None,
                "structured_output_repair_present": False,
                "structured_output_repair": None,
                "work_authority_verified": True,
            })["status"],
            "unknown_legacy_missing_request_identity",
        )

    def test_repair_work_without_root_identity_is_separately_unknown(self):
        spec = self.spec()
        proof = audit.model_spec_task_contract(spec, self.repair_authority(spec))
        self.assertEqual(proof["status"], "unknown_repair_work_missing_root_identity")

    def test_actual_repair_work_replay_keeps_unknown_root_classification(self):
        spec = self.spec()
        authority = self.repair_authority(spec)
        scheduler = Scheduler(":memory:")
        self.addCleanup(scheduler.close)
        work = {
            "schema_version": "0.1", "id": "work:model-spec-repair:a",
            "created_at": "2026-01-01T00:00:00+00:00",
            "updated_at": "2026-01-01T00:00:00+00:00", "question": "repair",
            "requested_capabilities": ["research"], "runtime_profile_ref": "runtime:test",
            "budget": {"max_seconds": 60}, "idempotency_key": "model-spec-repair:a",
            "declared_side_effects": [], "status": "ready", "input_refs": [],
            "metadata": {
                "purpose": "model_spec", "request_id": authority["request_id"],
                "structured_output_repair": authority["structured_output_repair"],
            },
        }
        self.assertEqual(scheduler.enqueue(work)["status"], "fresh")
        bundle = audit.scheduler_bundle(
            scheduler.connection, work["id"], include_request_identity=True,
        )
        self.assertEqual(
            audit.model_spec_task_contract(spec, bundle)["status"],
            "unknown_repair_work_missing_root_identity",
        )

    def test_malformed_repair_binding_cannot_earn_unknown_classification(self):
        spec = self.spec()
        for value in (None, "repair", {}, {"schema_version": "wrong"}):
            authority = self.repair_authority(spec)
            authority["structured_output_repair"] = value
            with self.subTest(value=value), self.assertRaisesRegex(
                RuntimeError, "repair binding is invalid"
            ):
                audit.model_spec_task_contract(spec, authority)

    def test_presence_proofs_require_actual_booleans(self):
        spec = self.spec()
        for field in (
            "model_spec_request_identity_present", "structured_output_repair_present",
        ):
            authority = self.repair_authority(spec)
            authority[field] = 1
            with self.subTest(field=field), self.assertRaisesRegex(
                RuntimeError, "presence proof is invalid"
            ):
                audit.model_spec_task_contract(spec, authority)

    def test_explicit_null_identity_is_not_treated_as_absent(self):
        spec = self.spec()
        authority = self.authority(spec)
        authority["model_spec_request_identity"] = None
        with self.assertRaisesRegex(RuntimeError, "explicitly null"):
            audit.model_spec_task_contract(spec, authority)

    def test_producer_bound_request_id_and_refs_are_verified(self):
        spec = self.spec()
        authority = self.authority(spec)
        refs = ["route-decision:a", "route-decision:b"]
        authority["producer_route_decision_refs"] = refs
        authority["request_id"] += ":producer:" + content_hash(refs)[:16]
        proof = audit.model_spec_task_contract(spec, authority)
        self.assertEqual(proof["status"], "verified_exact_work_identity")
        authority["producer_route_decision_refs"] = list(reversed(refs))
        with self.assertRaisesRegex(RuntimeError, "producer route refs are invalid"):
            audit.model_spec_task_contract(spec, authority)
        for malformed in (None, False, 0, {}, "route-decision:a"):
            authority = self.authority(spec)
            authority["producer_route_decision_refs"] = malformed
            with self.subTest(malformed=malformed), self.assertRaisesRegex(
                RuntimeError, "producer route refs are invalid"
            ):
                audit.model_spec_task_contract(spec, authority)

    def test_scheduler_bundle_replays_work_before_exposing_identity(self):
        spec = self.spec()
        authority = self.authority(spec)
        scheduler = Scheduler(":memory:")
        self.addCleanup(scheduler.close)
        work = {
            "schema_version": "0.1", "id": "work:model-spec:a",
            "created_at": "2026-01-01T00:00:00+00:00",
            "updated_at": "2026-01-01T00:00:00+00:00", "question": "model",
            "requested_capabilities": ["research"], "runtime_profile_ref": "runtime:test",
            "budget": {"max_seconds": 60}, "idempotency_key": "model-spec:a",
            "declared_side_effects": [], "status": "ready", "input_refs": [],
            "metadata": {
                "purpose": "model_spec", "request_id": authority["request_id"],
                "model_spec_request_identity": authority["model_spec_request_identity"],
            },
        }
        self.assertEqual(scheduler.enqueue(work)["status"], "fresh")
        bundle = audit.scheduler_bundle(
            scheduler.connection, work["id"], include_request_identity=True,
        )
        self.assertTrue(bundle["work_authority_verified"])
        self.assertEqual(
            audit.model_spec_task_contract(spec, bundle)["status"],
            "verified_exact_work_identity",
        )

    def test_inconsistent_or_malformed_proof_is_rejected(self):
        spec = self.spec()
        wrong_task = self.authority(spec, task_hash="c" * 64)
        malformed = self.authority(spec)
        malformed["model_spec_request_identity"] = {
            **malformed["model_spec_request_identity"], "extra": "not-authority",
        }
        variants = (
            (wrong_task, "task identities differ"),
            (malformed, "request identity is invalid"),
            ({**self.authority(spec), "purpose": "dossier"}, "Work purpose differs"),
            ({**self.authority(spec), "request_id": "0" * 32}, "request_id differs"),
            ({**self.authority(spec), "work_authority_verified": False},
             "was not canonically verified"),
        )
        for authority, message in variants:
            with self.subTest(message=message), self.assertRaisesRegex(RuntimeError, message):
                audit.model_spec_task_contract(spec, authority)

    def test_spec_id_must_bind_the_reported_task_hash(self):
        spec = self.spec()
        spec["task_hash"] = "c" * 64
        with self.assertRaisesRegex(RuntimeError, "differs from spec_ref"):
            audit.model_spec_task_contract(spec, None)


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

    def test_legacy_adapter_label_requires_the_same_exact_local_rejection(self):
        error = {"code": "MODEL_ADAPTER_REJECTED"}
        self.assertNotEqual(self.proof(error=error)["classification"],
                            "atomic_budget_refusal_no_send")
        self.reject()
        self.assertEqual(self.proof(error=error)["classification"],
                         "atomic_budget_refusal_no_send")
        self.assertNotEqual(self.proof(error=error, attempt_number=2)["classification"],
                            "atomic_budget_refusal_no_send")

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
