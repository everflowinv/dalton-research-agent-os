"""Closed fixture-model tests for bounded annual unknown-result recovery."""

from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path

from dalton_core.annual_report_qualitative import (
    RegisteredAnnualReportDraftWorker,
    RegisteredAnnualReportVerifierWorker,
)
from dalton_core.contracts import ModelInvocation, ResultEnvelope
from dalton_core.model_router import ModelRouter
from dalton_core.research_plan import _plan_work_orders
from dalton_core.research_plan import ResearchPlanValidationError
from dalton_core.research_plan_executor import ResearchPlanExecutor
from dalton_core.store import canonical_json, content_hash
from dalton_core.thesis_impact_budget import ThesisImpactBudgetStore
from tests.test_research_plan_annual_report import (
    AnnualSourceHarness,
    MISSION,
    seed_core_registration,
)
from tests import test_research_plan_annual_report as annual_support
from tests.test_research_plan_executor import PlanExecutorHarness
from tests.test_transcript_polish_model_worker import FakeAdapter


class UnknownThenSuccessAdapter(FakeAdapter):
    """One returned result with unavailable cost, then an actual success."""

    def __init__(self, candidate_wire: dict) -> None:
        super().__init__(candidate_wire)
        self.calls: list[str] = []

    def execute(self, work, route, selected):
        self.calls.append(work.id)
        invocation, succeeded = super().execute(work, route, selected)
        if len(self.calls) != 1:
            return invocation, succeeded
        wire = invocation.to_dict()
        wire["usage"] = {
            "input_tokens": None, "output_tokens": None, "total_tokens": None,
            "cache_read_tokens": None, "cache_write_tokens": None,
            "raw_provider_telemetry": {
                "cost": {"available": False, "usd": None}
            },
        }
        invocation = ModelInvocation.from_dict(wire)
        failed = ResultEnvelope(
            schema_version=succeeded.schema_version, id=succeeded.id,
            created_at=succeeded.created_at, work_order_ref=work.id,
            invocation_ref=invocation.id, status="failed", outputs={},
            actual_side_effects=(), usage_refs=succeeded.usage_refs,
            artifact_refs=(),
            error={"code": "UNCLASSIFIED_PROVIDER_FAILURE", "message": "unknown"},
            metadata=dict(succeeded.metadata),
        )
        return invocation, failed


class RecoveryFixture:
    def __init__(
        self, case: unittest.TestCase, *, recovery: dict,
        day_cap_micros: int = 20_000_000,
    ) -> None:
        self.harness = PlanExecutorHarness(suffix="annual-unknown-recovery")
        case.addCleanup(self.harness.close)
        self.source = AnnualSourceHarness(self.harness.planner)
        case.addCleanup(self.source.close)
        self.harness.planner.plans.annual_report_registry = self.source.registry
        registration = seed_core_registration(self.harness.planner, self.source)
        decision, records = self.harness.planner._selected_questions([(
            "Which customers and outsourced operations shape the company?",
            "Use only exact registered annual-report passages",
        )])
        draft_capability = "capability:dalton:model:qualitative-research"
        verifier_capability = "capability:dalton:model:qualitative-verifier"
        helper = annual_support.RegisteredAnnualReportExecutorTests
        self.draft_profile = helper._model_profile(
            stage="recovery-draft", capability=draft_capability,
            slot="credential-slot:model:recovery-draft",
        )
        self.verifier_profile = helper._model_profile(
            stage="recovery-verifier", capability=verifier_capability,
            slot="credential-slot:model:recovery-verifier",
        )
        self.draft_policy = helper._model_policy(
            stage="recovery-draft", profile_ids=[self.draft_profile["id"]],
            capability=draft_capability, tier="brain",
        )
        self.verifier_policy = helper._model_policy(
            stage="recovery-verifier", profile_ids=[self.verifier_profile["id"]],
            capability=verifier_capability, tier="verifier",
        )
        self.router = ModelRouter(clock=self.harness.clock)
        case.addCleanup(self.router.close)
        for item in (self.draft_profile, self.verifier_profile):
            self.router.register_profile(item)
        for item in (self.draft_policy, self.verifier_policy):
            self.router.register_policy(item)

        self.budget_path = Path(self.harness.planner.temp.name) / "annual-budget.sqlite"
        self.budget = ThesisImpactBudgetStore(self.budget_path, clock=self.harness.clock)
        case.addCleanup(self.budget.close)
        self.budget_policy_ref = "budget-policy:annual-recovery:test:1"
        self.budget.register_policy(
            policy_version_id=self.budget_policy_ref,
            day_cap_micros=day_cap_micros,
        )
        provider_retry = {
            "max_same_profile_retries": 0, "retry_backoff_seconds": 0,
            "unknown_recovery": recovery,
        }
        common = {
            "budget_db": str(self.budget_path),
            "budget_policy_ref": self.budget_policy_ref,
            "max_input_tokens": 32_000, "max_output_tokens": 4_000,
            "max_cost_usd": 1.0, "max_seconds": 120,
            "max_elapsed_seconds": 3600, "max_attempts": 1,
            "transport_retry": None,
        }
        record = records[0]
        self.created = self.harness.planner.plans.create_registered_annual_report_plan(
            question_ref=record["question_ref"],
            question_version_ref=record["question_version_ref"],
            decision_ref=decision["id"], **registration,
            query_terms=["customers", "outsourcing partners"],
            draft_model_execution={
                **common,
                "routing_policy_ref": self.draft_policy["policy_version_ref"],
                "credential_slot_refs": [self.draft_profile["credential_slot_ref"]],
                "provider_retry": provider_retry,
            },
            verifier_model_execution={
                **common,
                "routing_policy_ref": self.verifier_policy["policy_version_ref"],
                "credential_slot_refs": [self.verifier_profile["credential_slot_ref"]],
                "provider_retry": None,
            },
            actor_ref="core:planner", idempotency_key="annual-unknown-plan",
        )
        self.harness.planner._approve(self.created, suffix="annual-unknown")
        self.harness.planner._start(self.created, suffix="annual-unknown")

        statement = (
            "The company serves varied customers and depends on outsourcing partners."
        )
        draft_output = {
            "schema_version": "0.1", "answer": statement,
            "candidate": {
                "normalized_statement": statement,
                "metric_or_aspect": "customer and operating dependencies",
                "period": "FY2025 annual report", "basis": "reported",
                "cited_match_indexes": [0, 1],
            },
        }
        verifier_output = {
            "schema_version": "0.1", "verdict": "pass",
            "verified_statement": statement, "findings": [],
        }
        self.draft_adapter = UnknownThenSuccessAdapter(draft_output)
        mission = {
            "id": MISSION, "mission_version_ref": MISSION,
            "mission_ref": "coverage-mission:annual-test",
            "content_hash": "a" * 64,
            "budget": {"max_daily_paid_calls": 20,
                       "max_daily_cost_usd": 20.0},
            "outer_budget": {
                "mandate_ref": "mandate:annual-test",
                "mandate_version_ref": "mandate-version:annual-test",
                "mandate_version_hash": "b" * 64,
                "governance_policy_ref": "policy:annual-test",
                "governance_policy_version_ref": "policy-version:annual-test",
                "governance_policy_version_hash": "c" * 64,
                "max_daily_paid_calls": 20,
                "max_daily_cost_micros": 20_000_000,
            },
        }
        resolver = lambda ref, _company: (
            copy.deepcopy(mission) if ref == MISSION else None
        )
        common_worker = {
            "scheduler": self.harness.scheduler(), "router": self.router,
            "store": self.harness.core,
            "observability": self.harness.observability,
            "polish_worker": None, "budget_store": self.budget,
            "budget_policy_ref": self.budget_policy_ref,
            "mission_resolver": resolver, "clock": self.harness.clock,
        }
        self.draft_worker = RegisteredAnnualReportDraftWorker(
            **common_worker, adapter=self.draft_adapter,
            routing_policy_ref=self.draft_policy["policy_version_ref"],
            credential_slot_refs=[self.draft_profile["credential_slot_ref"]],
            provider_retry=provider_retry,
        )
        self.verifier_worker = RegisteredAnnualReportVerifierWorker(
            **common_worker, adapter=FakeAdapter(verifier_output),
            routing_policy_ref=self.verifier_policy["policy_version_ref"],
            credential_slot_refs=[self.verifier_profile["credential_slot_ref"]],
            provider_retry=None,
        )
        self.executor = self.new_executor()

    @property
    def plan_ref(self):
        return self.created["plan_version_ref"]

    def new_executor(self):
        h = self.harness
        return ResearchPlanExecutor(
            plan=h.planner.plans, scheduler=h.scheduler(),
            connector_records=h.connector_records, coordinator=h.coordinator,
            connectors=h.connectors, catalog=h.catalog, transport=h.transport,
            resolver=h.resolver, staging=h.staging, clock=h.clock,
            permissions=h.permissions, policy_resolver=h.authorities.policy,
            principal_ref="principal:worker-1",
            runner_environment_hash=h.runner_environment_hash,
            annual_report_registry=self.source.registry,
            annual_report_draft_worker=self.draft_worker,
            annual_report_verifier_worker=self.verifier_worker,
            actor_ref=h.actor_ref,
        )

    def run_to_unknown(self):
        first = self.executor.run_once(plan_version_ref=self.plan_ref)
        second = self.executor.run_once(plan_version_ref=self.plan_ref)
        return first, second


class AnnualReportUnknownRecoveryTests(unittest.TestCase):
    POLICY = {
        "max_fresh_work_orders": 2,
        "retry_backoff_seconds": 0,
        "max_elapsed_seconds": 7200,
    }

    def test_plan_aggregate_includes_every_authorized_fresh_work_attempt(self):
        harness = PlanExecutorHarness(suffix="annual-recovery-aggregate")
        self.addCleanup(harness.close)
        source = AnnualSourceHarness(harness.planner)
        self.addCleanup(source.close)
        harness.planner.plans.annual_report_registry = source.registry
        registration = seed_core_registration(harness.planner, source)
        decision, records = harness.planner._selected_questions([(
            "Which dependencies shape the company?", "Use the annual report",
        )])
        retry = {
            "max_same_profile_retries": 0, "retry_backoff_seconds": 0,
            "unknown_recovery": self.POLICY,
        }
        model = {
            "routing_policy_ref": "routing-policy:test:1",
            "credential_slot_refs": ["credential-slot:model:test"],
            "budget_db": str(Path(harness.planner.temp.name) / "budget.sqlite"),
            "budget_policy_ref": "budget:test:1",
            "max_input_tokens": 1_000, "max_output_tokens": 100,
            "max_cost_usd": 200.0, "max_seconds": 30,
            "max_elapsed_seconds": 3600, "max_attempts": 1,
            "provider_retry": retry, "transport_retry": None,
        }
        with self.assertRaisesRegex(
            ResearchPlanValidationError, "exceeds the exact coverage mission"
        ):
            harness.planner.plans.create_registered_annual_report_plan(
                question_ref=records[0]["question_ref"],
                question_version_ref=records[0]["question_version_ref"],
                decision_ref=decision["id"], **registration,
                query_terms=["dependencies"],
                draft_model_execution=model, verifier_model_execution=model,
                actor_ref="core:planner",
            )

    def test_unknown_full_reserve_gets_new_work_charge_and_effective_suffix(self):
        fixture = RecoveryFixture(self, recovery=self.POLICY)
        plan_before = fixture.harness.planner.plans.plan_version(fixture.plan_ref)
        work_before = _plan_work_orders(plan_before)[1]
        _, failed = fixture.run_to_unknown()
        self.assertEqual(failed["status"], "failed")
        original_formal = fixture.harness.scheduler().formal_result(work_before["id"])
        original_bytes = canonical_json(original_formal)

        outcomes = [fixture.executor.run_once(plan_version_ref=fixture.plan_ref)]
        for _ in range(3):
            outcomes.append(fixture.executor.run_once(plan_version_ref=fixture.plan_ref))
        self.assertEqual(
            [item["status"] for item in outcomes],
            ["admitted", "admitted", "admitted", "complete"], outcomes,
        )
        links = fixture.harness.core.connection.execute(
            "SELECT record_json FROM research_plan_recovery_links "
            "WHERE plan_version_ref=? ORDER BY version_number", (fixture.plan_ref,),
        ).fetchall()
        self.assertEqual(len(links), 3)
        link_wires = [json.loads(row[0]) for row in links]
        self.assertEqual(
            [row["kind"] for row in link_wires],
            ["unknown_recovery", "recovery_suffix", "recovery_suffix"],
        )
        recovery_work = link_wires[0]["recovery_work_order_ref"]
        self.assertNotEqual(recovery_work, work_before["id"])
        self.assertEqual(len(fixture.draft_adapter.calls), 2)
        admissions = fixture.budget.connection.execute(
            "SELECT work_order_ref,reserved_micros FROM thesis_impact_day_admissions "
            "ORDER BY created_at,work_order_ref"
        ).fetchall()
        self.assertEqual(len(admissions), 3)  # failed draft, recovered draft, verifier
        by_work = {row["work_order_ref"]: row["reserved_micros"] for row in admissions}
        self.assertEqual(by_work[work_before["id"]], 1_000_000)
        self.assertEqual(by_work[recovery_work], 1_000_000)
        self.assertIsNone(fixture.budget.admission(
            work_order_ref=work_before["id"], attempt_number=1,
            phase="assessment",
        )["settlement"])
        draft_families = {
            json.loads(row[0])["model_family"]
            for row in fixture.harness.core.connection.execute(
                "SELECT invocation_json FROM model_invocations WHERE work_order_ref IN (?,?)",
                (work_before["id"], recovery_work),
            ).fetchall()
        }
        verifier_work = link_wires[1]["recovery_work_order_ref"]
        verifier_invocation = json.loads(fixture.harness.core.connection.execute(
            "SELECT invocation_json FROM model_invocations WHERE work_order_ref=?",
            (verifier_work,),
        ).fetchone()[0])
        self.assertNotIn(verifier_invocation["model_family"], draft_families)
        self.assertEqual(fixture.harness.staging_counts()["candidate_claim_versions"], 1)
        self.assertEqual(
            canonical_json(fixture.harness.scheduler().formal_result(work_before["id"])),
            original_bytes,
        )
        self.assertEqual(
            canonical_json(fixture.harness.planner.plans.plan_version(fixture.plan_ref)),
            canonical_json(plan_before),
        )

        before = fixture.budget.connection.execute(
            "SELECT COUNT(*) FROM thesis_impact_day_admissions"
        ).fetchone()[0]
        restarted = fixture.new_executor()
        again = restarted.run_once(plan_version_ref=fixture.plan_ref)
        self.assertEqual(again["status"], "complete")
        self.assertEqual(fixture.budget.connection.execute(
            "SELECT COUNT(*) FROM thesis_impact_day_admissions"
        ).fetchone()[0], before)
        self.assertEqual(fixture.harness.core.connection.execute(
            "SELECT COUNT(*) FROM research_plan_recovery_links WHERE plan_version_ref=?",
            (fixture.plan_ref,),
        ).fetchone()[0], 3)

    def test_missing_budget_authority_refuses_recovery(self):
        fixture = RecoveryFixture(self, recovery=self.POLICY)
        _, failed = fixture.run_to_unknown()
        self.assertEqual(failed["status"], "failed")
        fixture.draft_worker.budget_store = None
        blocked = fixture.executor.run_once(plan_version_ref=fixture.plan_ref)
        self.assertEqual(blocked["status"], "blocked")
        self.assertEqual(blocked["reason"], "unknown_result_unproven")
        self.assertEqual(fixture.harness.core.connection.execute(
            "SELECT COUNT(*) FROM research_plan_recovery_links"
        ).fetchone()[0], 0)

    def test_expired_mission_refuses_recovery_without_model_read(self):
        fixture = RecoveryFixture(self, recovery=self.POLICY)
        fixture.run_to_unknown()
        fixture.draft_worker.mission_resolver = lambda _ref, _company: (_ for _ in ()).throw(
            RuntimeError("mission inactive")
        )
        blocked = fixture.executor.run_once(plan_version_ref=fixture.plan_ref)
        self.assertEqual(blocked["reason"], "recovery_mission_invalid")
        self.assertEqual(len(fixture.draft_adapter.calls), 1)

    def test_budget_refusal_is_terminal_and_never_reads_the_provider(self):
        fixture = RecoveryFixture(
            self, recovery=self.POLICY, day_cap_micros=1_000_000
        )
        fixture.run_to_unknown()
        admitted = fixture.executor.run_once(plan_version_ref=fixture.plan_ref)
        self.assertEqual(admitted["status"], "admitted")
        blocked = fixture.executor.run_once(plan_version_ref=fixture.plan_ref)
        self.assertEqual(blocked["status"], "blocked")
        self.assertEqual(blocked["reason"], "recovery_budget_refused")
        self.assertEqual(len(fixture.draft_adapter.calls), 1)
        self.assertEqual(fixture.budget.connection.execute(
            "SELECT COUNT(*) FROM thesis_impact_day_admissions"
        ).fetchone()[0], 1)

    def test_max_fresh_work_orders_and_deadline_stop(self):
        exhausted = RecoveryFixture(self, recovery={
            **self.POLICY, "max_fresh_work_orders": 1,
        })
        exhausted.run_to_unknown()
        admitted = exhausted.executor.run_once(plan_version_ref=exhausted.plan_ref)
        self.assertEqual(admitted["status"], "admitted")
        # Make the recovery call unknown as well.
        exhausted.draft_adapter.calls.clear()
        failed = exhausted.executor.run_once(plan_version_ref=exhausted.plan_ref)
        self.assertEqual(failed["status"], "failed")
        blocked = exhausted.executor.run_once(plan_version_ref=exhausted.plan_ref)
        self.assertEqual(blocked["reason"], "unknown_recovery_exhausted")

        deadline = RecoveryFixture(self, recovery={
            **self.POLICY, "max_elapsed_seconds": 1,
        })
        deadline.run_to_unknown()
        deadline.harness.clock.advance(2)
        blocked = deadline.executor.run_once(plan_version_ref=deadline.plan_ref)
        self.assertEqual(blocked["reason"], "unknown_recovery_deadline_exceeded")
        self.assertEqual(len(deadline.draft_adapter.calls), 1)


if __name__ == "__main__":
    unittest.main()
