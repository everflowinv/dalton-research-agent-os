"""Closed fixture-model tests for bounded annual unknown-result recovery."""

from __future__ import annotations

import copy
import json
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from dalton_core.annual_report_qualitative import (
    RegisteredAnnualReportDraftWorker,
    RegisteredAnnualReportVerifierWorker,
)
from dalton_core.contracts import ModelInvocation, ResultEnvelope
from dalton_core.coverage_mission import CoverageMissionAuthority
from dalton_core.model_router import ModelRouter
from dalton_core.openclaw_model_adapter import BrokerConnectionError
from dalton_core.openclaw_model_adapter import OpenClawModelAdapter
from dalton_core.research_plan import _plan_work_orders
from dalton_core.research_plan import ResearchPlanValidationError
from dalton_core.research_plan_executor import (
    ResearchPlanExecutor, ResearchPlanExecutorConflict,
)
from dalton_core.sec_lane_launcher import SecLaneLauncher
from dalton_core.sec_company_facts_lane import read_active_annual_budget_mission
from dalton_core.store import canonical_json, content_hash
from dalton_core.thesis_impact_budget import ThesisImpactBudgetStore
from tests.test_research_plan_annual_report import (
    AnnualSourceHarness,
    COMPANY,
    MISSION,
    seed_core_registration,
)
from tests.p9a_fixtures import bootstrap_method_authorities, mission_params
from tests import test_research_plan_annual_report as annual_support
from tests.test_research_plan_executor import PlanExecutorHarness
from tests.test_transcript_polish_model_worker import FakeAdapter
from tests.test_openclaw_model_adapter import (
    AUTH_CLIENT_ID, AUTH_SECRET, FakeBroker, seal, success_response,
)


class UnknownThenSuccessAdapter(FakeAdapter):
    """One returned result with unavailable cost, then an actual success."""

    def __init__(
        self, candidate_wire: dict, *, first_error_code: str = "PROVIDER_INTERNAL_ERROR",
        unknown_calls: int = 1,
    ) -> None:
        super().__init__(candidate_wire)
        self.calls: list[str] = []
        self.first_error_code = first_error_code
        self.unknown_calls = unknown_calls

    def execute(self, work, route, selected):
        self.calls.append(work.id)
        invocation, succeeded = super().execute(work, route, selected)
        if len(self.calls) > self.unknown_calls:
            return invocation, succeeded
        wire = invocation.to_dict()
        wire["usage"] = {
            "input_tokens": None, "output_tokens": None, "total_tokens": None,
            "cache_read_tokens": None, "cache_write_tokens": None,
            "raw_provider_telemetry": {
                "cost": {"available": False, "usd": None}
            },
            "measurement_status": "unavailable",
            "authority_status": "uncommitted",
        }
        invocation = ModelInvocation.from_dict(wire)
        failed = ResultEnvelope(
            schema_version=succeeded.schema_version, id=succeeded.id,
            created_at=succeeded.created_at, work_order_ref=work.id,
            invocation_ref=invocation.id, status="failed", outputs={},
            actual_side_effects=(), usage_refs=succeeded.usage_refs,
            artifact_refs=(),
            error={"code": self.first_error_code, "message": "unknown",
                   "source": "openclaw-model-broker"},
            metadata=dict(succeeded.metadata) | {
                "broker_response_hash": content_hash({"work": work.id}),
                "broker_request_mode": "execute",
                "dispatch_proof": {
                    "authority": "openclaw-model-adapter",
                    "state": "provider_completed_failure", "version": "0.1",
                },
            },
        )
        return invocation, failed


class PostSendExceptionAdapter:
    def execute(self, _work, _route, _selected):
        raise BrokerConnectionError("fixture disconnected after dispatch")

    def replay(self, _work, _route, _selected):
        raise AssertionError("terminal unknown Work must not replay")


class RecoveryFixture:
    def __init__(
        self, case: unittest.TestCase, *, recovery: dict,
        day_cap_micros: int = 20_000_000,
        max_attempts: int = 1,
        unknown_calls: int = 1,
        same_profile_retries: int = 0,
        production_mission: bool = False,
    ) -> None:
        self.harness = PlanExecutorHarness(suffix="annual-unknown-recovery")
        case.addCleanup(self.harness.close)
        if production_mission:
            self.harness.clock.value = datetime(
                2026, 9, 11, 12, 0, tzinfo=timezone.utc
            )
            outer = {
                "max_daily_paid_calls": 20,
                "max_daily_cost_usd": 20.0,
                "max_alphaengine_calls_24h": 30,
            }
            active = self.harness.core.active_policy_version()
            self.harness.core.create_policy(
                {**active.policy, "research_budget": outer},
                policy_version_id="governance-policy-version:recovery:2",
                version_number=2, prior_version_ref=active.id,
                effective_from=self.harness.clock().isoformat(),
                actor_ref="human:test-owner",
                change_reason="authorize bounded annual recovery",
                activate=True,
            )
            method = bootstrap_method_authorities(
                self.harness.core,
                mandate_ref="mandate:annual-recovery-test",
                mandate_constraints={"research_budget": outer},
            )
            params = mission_params(method)
            params.update({
                "mission_ref": "coverage-mission:annual-test",
                "version_id": MISSION,
                "idempotency_key": "coverage-mission:annual-recovery:1",
                "universe": [{
                    "company_ref": COMPANY, "ticker": "TEST",
                    "coverage_tier": "A", "bootstrap_priority": "P0",
                }],
                "budget": {
                    "max_daily_paid_calls": 10,
                    "max_daily_cost_usd": 10.0,
                    "max_alphaengine_calls_24h": 0,
                },
            })
            params["autonomy"] = {
                **params["autonomy"], "automation_principal": "automation:test",
            }
            for key in ("playbook_ref", "constitution_ref", "mandate_ref"):
                params.pop(key, None)
            CoverageMissionAuthority(self.harness.core).create_mission(
                params.pop("mission_ref"), **params
            )
        self.source = AnnualSourceHarness(self.harness.planner)
        case.addCleanup(self.source.close)
        self.harness.planner.plans.annual_report_registry = self.source.registry
        registration = seed_core_registration(self.harness.planner, self.source)
        decision, records = self.harness.planner._selected_questions([(
            "Which customers and outsourced operations shape the company?",
            "Use only exact registered annual-report passages",
        )])
        draft_capability = "research"
        verifier_capability = "verify"
        helper = annual_support.RegisteredAnnualReportExecutorTests
        self.draft_profile = helper._model_profile(
            stage="recovery-draft", capability=draft_capability,
            slot="credential-slot:model:recovery-draft",
        )
        self.verifier_profile = helper._model_profile(
            stage="recovery-verifier", capability=verifier_capability,
            slot="credential-slot:model:recovery-verifier",
        )
        if production_mission:
            for profile_wire in (self.draft_profile, self.verifier_profile):
                profile_wire["availability"] = {
                    "state": "available",
                    "checked_at": "2026-09-01T00:00:00+00:00",
                    "valid_until": "2027-09-11T00:00:00+00:00",
                }
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
            "max_same_profile_retries": same_profile_retries,
            "retry_backoff_seconds": 0,
            "unknown_recovery": recovery,
        }
        common = {
            "budget_db": str(self.budget_path),
            "budget_policy_ref": self.budget_policy_ref,
            "max_input_tokens": 32_000, "max_output_tokens": 4_000,
            "max_cost_usd": 1.0, "max_seconds": 120,
            "max_elapsed_seconds": 3600, "max_attempts": max_attempts,
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
        self.draft_output = draft_output
        verifier_output = {
            "schema_version": "0.1", "verdict": "pass",
            "verified_statement": statement, "findings": [],
        }
        self.draft_adapter = UnknownThenSuccessAdapter(
            draft_output, unknown_calls=unknown_calls
        )
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
        if production_mission:
            resolver = lambda ref, company: read_active_annual_budget_mission(
                self.harness.core.connection, ref, company,
                now=self.harness.clock(),
            )
        else:
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

    def new_executor(self, *, fault_injector=None):
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
            actor_ref=h.actor_ref, fault_injector=fault_injector,
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

    def test_real_unix_sent_then_drop_persists_unknown_and_recovers_fresh(self):
        fixture = RecoveryFixture(self, recovery=self.POLICY)
        state = Path(fixture.harness.planner.temp.name)

        def adapter_for(broker):
            return OpenClawModelAdapter(
                broker.path,
                route_resolver=fixture.router.get_decision,
                auth_client_id=AUTH_CLIENT_ID,
                auth_key_provider=lambda: AUTH_SECRET,
                timeout_seconds=1,
                expected_agent_id="dalton-model-broker",
                clock=fixture.harness.clock,
            )

        dropped = FakeBroker(state, lambda _request: None)
        self.addCleanup(dropped.close)
        fixture.draft_worker.adapter = adapter_for(dropped)
        admitted = fixture.executor.run_once(plan_version_ref=fixture.plan_ref)
        self.assertEqual(admitted["status"], "admitted")
        failed = fixture.executor.run_once(plan_version_ref=fixture.plan_ref)
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(len(dropped.requests), 1)
        original_work = _plan_work_orders(
            fixture.harness.planner.plans.plan_version(fixture.plan_ref)
        )[1]
        formal = fixture.harness.scheduler().formal_result(original_work["id"])
        self.assertEqual(
            formal["result_envelope"]["error"]["code"],
            "POST_SEND_RESULT_UNKNOWN",
        )
        self.assertTrue(formal["result_envelope"]["invocation_ref"].startswith(
            "invocation:"
        ))
        original_budget = fixture.budget.admission(
            work_order_ref=original_work["id"], attempt_number=1,
            phase="assessment",
        )
        self.assertIsNone(original_budget["settlement"])

        recovery = fixture.executor.run_once(plan_version_ref=fixture.plan_ref)
        self.assertEqual(recovery["status"], "admitted")
        recovery_work_ref = recovery["admitted_work_order_ref"]

        def recovered_response(request):
            response = success_response(
                request, text=canonical_json(fixture.draft_output)
            )
            response.pop("contentHash")
            response.update({
                "provider": fixture.draft_profile["provider"],
                "model": fixture.draft_profile["model"],
                "canonicalModel": (
                    f"{fixture.draft_profile['provider']}/"
                    f"{fixture.draft_profile['model']}"
                ),
            })
            return seal(response)

        recovered_broker = FakeBroker(state, recovered_response)
        self.addCleanup(recovered_broker.close)
        fixture.draft_worker.adapter = adapter_for(recovered_broker)
        verifier_admitted = fixture.executor.run_once(
            plan_version_ref=fixture.plan_ref
        )
        self.assertEqual(verifier_admitted["status"], "admitted")
        self.assertNotEqual(recovery_work_ref, original_work["id"])
        self.assertEqual(len(recovered_broker.requests), 1)
        recovered_budget = fixture.budget.admission(
            work_order_ref=recovery_work_ref, attempt_number=1,
            phase="assessment",
        )
        self.assertIsNotNone(recovered_budget["settlement"])
        self.assertEqual(
            recovered_budget["settlement"]["actual_micros"], 10_000
        )
        staged = fixture.executor.run_once(plan_version_ref=fixture.plan_ref)
        self.assertEqual(staged["status"], "admitted")
        complete = fixture.executor.run_once(plan_version_ref=fixture.plan_ref)
        self.assertEqual(complete["status"], "complete")
        self.assertEqual(fixture.harness.staging_counts()["candidate_claim_versions"], 1)

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

    def test_open_reservation_alone_is_not_unknown_result_proof(self):
        fixture = RecoveryFixture(self, recovery=self.POLICY)
        fixture.draft_worker.adapter = PostSendExceptionAdapter()
        admitted, failed = fixture.run_to_unknown()
        self.assertEqual(admitted["status"], "admitted")
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(fixture.budget.connection.execute(
            "SELECT COUNT(*) FROM thesis_impact_day_admissions"
        ).fetchone()[0], 1)

        self.assertEqual(fixture.harness.core.connection.execute(
            "SELECT COUNT(*) FROM model_invocations WHERE work_order_ref LIKE 'work:plan-%'"
        ).fetchone()[0], 0)
        blocked = fixture.executor.run_once(plan_version_ref=fixture.plan_ref)
        self.assertEqual(blocked["status"], "blocked")
        self.assertEqual(blocked["reason"], "unknown_result_unproven")

    def test_unapproved_completed_failure_does_not_gain_recovery_authority(self):
        fixture = RecoveryFixture(self, recovery=self.POLICY)
        fixture.draft_worker.adapter = UnknownThenSuccessAdapter(
            {}, first_error_code="UNCLASSIFIED_PROVIDER_FAILURE"
        )
        fixture.run_to_unknown()
        self.assertEqual(fixture.budget.connection.execute(
            "SELECT COUNT(*) FROM thesis_impact_day_admissions"
        ).fetchone()[0], 1)
        self.assertEqual(fixture.harness.core.connection.execute(
            "SELECT COUNT(*) FROM model_invocations"
        ).fetchone()[0], 1)
        blocked = fixture.executor.run_once(plan_version_ref=fixture.plan_ref)
        self.assertEqual(blocked["status"], "blocked")
        self.assertEqual(blocked["reason"], "unknown_result_ineligible")

    def test_expired_mission_refuses_recovery_without_model_read(self):
        fixture = RecoveryFixture(self, recovery=self.POLICY)
        fixture.run_to_unknown()
        fixture.draft_worker.mission_resolver = lambda _ref, _company: (_ for _ in ()).throw(
            RuntimeError("mission inactive")
        )
        blocked = fixture.executor.run_once(plan_version_ref=fixture.plan_ref)
        self.assertEqual(blocked["reason"], "recovery_mission_invalid")
        self.assertEqual(len(fixture.draft_adapter.calls), 1)

    def test_same_mission_ref_with_wrong_hash_refuses_recovery(self):
        fixture = RecoveryFixture(self, recovery=self.POLICY)
        fixture.run_to_unknown()
        original = fixture.draft_worker.mission_resolver(MISSION, "wanhua")
        fixture.draft_worker.mission_resolver = lambda _ref, _company: {
            **original, "content_hash": "f" * 64,
        }
        blocked = fixture.executor.run_once(plan_version_ref=fixture.plan_ref)
        self.assertEqual(blocked["status"], "blocked")
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

    def test_crash_after_recovery_enqueue_reuses_same_work_without_charge(self):
        fixture = RecoveryFixture(self, recovery=self.POLICY)
        fixture.run_to_unknown()
        faulted = False

        def fault(seam):
            nonlocal faulted
            if seam == "after_recovery_enqueue" and not faulted:
                faulted = True
                raise RuntimeError("crash after recovery enqueue")

        crashing = fixture.new_executor(fault_injector=fault)
        with self.assertRaisesRegex(RuntimeError, "after recovery enqueue"):
            crashing.run_once(plan_version_ref=fixture.plan_ref)
        orphan = fixture.harness.core.connection.execute(
            "SELECT work_order_id FROM scheduler_work_orders "
            "WHERE work_order_json LIKE '%unknown_recovery_derivation%'"
        ).fetchall()
        self.assertEqual(len(orphan), 1)
        self.assertEqual(fixture.harness.core.connection.execute(
            "SELECT COUNT(*) FROM research_plan_recovery_links"
        ).fetchone()[0], 0)
        self.assertEqual(fixture.budget.connection.execute(
            "SELECT COUNT(*) FROM thesis_impact_day_admissions"
        ).fetchone()[0], 1)

        resumed = fixture.new_executor()
        linked = resumed.run_once(plan_version_ref=fixture.plan_ref)
        self.assertEqual(linked["status"], "admitted")
        self.assertEqual(linked["admitted_work_order_ref"], orphan[0][0])
        self.assertEqual(fixture.harness.core.connection.execute(
            "SELECT COUNT(*) FROM scheduler_work_orders "
            "WHERE work_order_json LIKE '%unknown_recovery_derivation%'"
        ).fetchone()[0], 1)
        self.assertEqual(fixture.budget.connection.execute(
            "SELECT COUNT(*) FROM thesis_impact_day_admissions"
        ).fetchone()[0], 1)

    def test_recovery_link_recomputes_ordinal_number_and_identity(self):
        mutations = {
            "ordinal": lambda wire: wire["derivation_identity"].__setitem__(
                "ordinal", 3
            ),
            "number": lambda wire: (
                wire.__setitem__("recovery_number", 2),
                wire["derivation_identity"].__setitem__("recovery_number", 2),
            ),
            "identity": lambda wire: (
                wire.__setitem__("upstream_work_order_hash", "f" * 64),
                wire["derivation_identity"].__setitem__(
                    "upstream_work_order_hash", "f" * 64
                ),
            ),
        }
        for label, mutate in mutations.items():
            with self.subTest(label=label):
                fixture = RecoveryFixture(self, recovery=self.POLICY)
                fixture.run_to_unknown()
                fixture.executor.run_once(plan_version_ref=fixture.plan_ref)
                row = fixture.harness.core.connection.execute(
                    "SELECT recovery_link_id,record_json FROM research_plan_recovery_links"
                ).fetchone()
                wire = json.loads(row["record_json"])
                mutate(wire)
                wire["content_hash"] = content_hash({
                    key: value for key, value in wire.items()
                    if key != "content_hash"
                })
                fixture.harness.core.connection.execute(
                    "DROP TRIGGER research_plan_recovery_links_no_update"
                )
                fixture.harness.core.connection.execute(
                    "UPDATE research_plan_recovery_links SET record_json=?,content_hash=? "
                    "WHERE recovery_link_id=?",
                    (canonical_json(wire), wire["content_hash"], row["recovery_link_id"]),
                )
                with self.assertRaises(ResearchPlanExecutorConflict):
                    fixture.executor.run_once(plan_version_ref=fixture.plan_ref)

    def test_launcher_resumes_recovery_work_during_scheduler_backoff(self):
        fixture = RecoveryFixture(
            self,
            recovery={**self.POLICY, "max_fresh_work_orders": 1},
            max_attempts=2,
            unknown_calls=2,
            same_profile_retries=1,
            production_mission=True,
        )
        _, retryable_original = fixture.run_to_unknown()
        self.assertEqual(retryable_original["status"], "retryable")
        terminal_original = fixture.executor.run_once(
            plan_version_ref=fixture.plan_ref
        )
        self.assertEqual(terminal_original["status"], "failed")
        admitted = fixture.executor.run_once(plan_version_ref=fixture.plan_ref)
        recovery_work_ref = admitted["admitted_work_order_ref"]
        scheduler = fixture.harness.scheduler()
        claim = scheduler.claim("worker:killed", work_order_id=recovery_work_ref)
        retry_at = fixture.harness.clock.value + timedelta(seconds=30)
        retryable = ResultEnvelope(
            schema_version="0.1",
            id="result:recovery-process-killed",
            created_at=fixture.harness.clock.value.isoformat(timespec="microseconds"),
            work_order_ref=recovery_work_ref,
            invocation_ref="invocation:recovery-process-killed",
            status="retryable",
            outputs={}, actual_side_effects=(), usage_refs=(), artifact_refs=(),
            error={"code": "PROCESS_KILLED", "message": "fixture", "source": "test"},
        )
        completed = scheduler.complete(
            recovery_work_ref, claim["lease"]["attempt_number"], "worker:killed",
            claim["lease_token"], retryable,
            idempotency_key="complete:recovery-process-killed",
            retry_at=retry_at,
        )
        self.assertEqual(completed["work_state"], "ready")
        original_work = _plan_work_orders(
            fixture.harness.planner.plans.plan_version(fixture.plan_ref)
        )[1]
        self.assertEqual(
            scheduler.formal_result(original_work["id"])["terminal_state"],
            "failed",
        )
        launcher_view = SimpleNamespace(
            state_dir=Path(fixture.harness.planner.temp.name),
            clock=fixture.harness.clock,
        )
        resumable, deadline_expired = SecLaneLauncher._annual_resume_state(
            launcher_view, {"plan_version_ref": fixture.plan_ref}
        )
        self.assertTrue(resumable)
        self.assertFalse(deadline_expired)
        self.assertEqual(scheduler.status(recovery_work_ref)["state"], "ready")

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

        disabled = RecoveryFixture(self, recovery={
            **self.POLICY, "max_fresh_work_orders": 0,
        })
        disabled.run_to_unknown()
        before_admissions = disabled.budget.connection.execute(
            "SELECT COUNT(*) FROM thesis_impact_day_admissions"
        ).fetchone()[0]
        blocked = disabled.executor.run_once(plan_version_ref=disabled.plan_ref)
        self.assertEqual(blocked["status"], "blocked")
        self.assertEqual(blocked["reason"], "unknown_recovery_exhausted")
        self.assertEqual(len(disabled.draft_adapter.calls), 1)
        self.assertEqual(disabled.harness.core.connection.execute(
            "SELECT COUNT(*) FROM research_plan_recovery_links"
        ).fetchone()[0], 0)
        self.assertEqual(disabled.harness.core.connection.execute(
            "SELECT COUNT(*) FROM scheduler_work_orders "
            "WHERE work_order_json LIKE '%unknown_recovery_derivation%'"
        ).fetchone()[0], 0)
        self.assertEqual(disabled.budget.connection.execute(
            "SELECT COUNT(*) FROM thesis_impact_day_admissions"
        ).fetchone()[0], before_admissions)

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
