"""Mission-bound admission tests for targeted registered annual-report repair."""

from __future__ import annotations

import json
import os
import sqlite3
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dalton_core.annual_report_runtime import (
    DRAFT_MODEL_CONFIG_NAME, VERIFIER_MODEL_CONFIG_NAME,
)
from dalton_core.company_dossier_launcher import run_digest
from dalton_core.contracts import ResultEnvelope
from dalton_core.coverage_mission import CoverageMissionAuthority
from dalton_core.dossier_repair_feedback import read_dossier_repair_feedback
from dalton_core.mission_annual_research import (
    MissionAnnualResearchAuthority, MissionAnnualResearchError,
    WORKFLOW_CONTRACT_REF,
)
from dalton_core.mission_annual_research_executor import (
    MissionAnnualResearchExecutor, MissionAnnualResearchExecutorError,
)
from dalton_core.model_router import ModelRouter
from dalton_core.openclaw_model_adapter import _WORK_ID_RE
from dalton_core.mission_dossier_lane import ledger_signature
from dalton_core.research_auto_commit import DOCUMENT_QUALITATIVE_RULE_REF
from dalton_core.research_auto_commit import ResearchAutoCommitRejected
from dalton_core.annual_report_qualitative import (
    AnnualReportQualitativeError, RegisteredAnnualReportDraftWorker,
    RegisteredAnnualReportVerifierWorker,
)
from dalton_core.registered_annual_report import OPERATION, RegisteredAnnualReportError
from dalton_core.sec_company_facts_lane import LanePreconditionError
from dalton_core.sec_company_facts_lane import read_active_annual_budget_mission
from dalton_core.store import canonical_json, content_hash
from dalton_core.thesis_impact_budget import ThesisImpactBudgetStore
from tests.p9a_fixtures import bootstrap_method_authorities, mission_params
from tests.test_research_plan_annual_report import (
    ACCESSION, CIK, AnnualSourceHarness,
    seed_core_registration,
)
from tests import test_research_plan_annual_report as annual_support
from tests.test_research_plan_executor import PlanExecutorHarness
from tests.test_transcript_polish_model_worker import FakeAdapter


COMPANY = "wanhua"
MISSION = "coverage-mission-version:annual-test"
NOW = datetime(2026, 9, 11, 12, 0, tzinfo=timezone.utc)


class CountingFakeAdapter(FakeAdapter):
    def __init__(self, candidate_wire):
        super().__init__(candidate_wire)
        self.calls = 0

    def execute(self, work, route, selected):
        self.calls += 1
        return super().execute(work, route, selected)


class MissionAnnualFixture:
    def __init__(self, case: unittest.TestCase, *, mission_calls: int = 10,
                 same_family: bool = False, sec_connected: bool = True,
                 unusable_route: bool = False, auto_commit: bool = False,
                 canonical_writes: bool = True) -> None:
        self.case = case
        self.harness = PlanExecutorHarness(suffix="mission-annual-admission")
        case.addCleanup(self.harness.close)
        self.harness.clock.value = NOW
        self.store = self.harness.core
        self.state = Path(self.harness.planner.temp.name)

        outer = {
            "max_daily_paid_calls": 20,
            "max_daily_cost_usd": 20.0,
            "max_alphaengine_calls_24h": 30,
        }
        active = self.store.active_policy_version()
        policy_body = {**active.policy, "research_budget": outer}
        if auto_commit:
            policy_body["research_candidate_auto_commit"] = {
                "enabled": True, "max_records": 20,
                "rules": [DOCUMENT_QUALITATIVE_RULE_REF],
            }
        self.store.create_policy(
            policy_body,
            policy_version_id="governance-policy-version:mission-annual:2",
            version_number=2, prior_version_ref=active.id,
            effective_from=NOW.isoformat(),
            actor_ref="human:test-owner", change_reason="test exact research budget",
            activate=True,
        )
        method = bootstrap_method_authorities(
            self.store, mandate_ref="mandate:mission-annual-test",
            mandate_constraints={"research_budget": outer},
        )
        params = mission_params(method)
        params.update({
            "mission_ref": "coverage-mission:annual-test",
            "version_id": MISSION,
            "idempotency_key": "coverage-mission:mission-annual:1",
            "universe": [{
                "company_ref": COMPANY, "ticker": "TEST",
                "coverage_tier": "A", "bootstrap_priority": "P0",
            }],
            "budget": {
                "max_daily_paid_calls": mission_calls,
                "max_daily_cost_usd": 10.0,
                "max_alphaengine_calls_24h": 0,
            },
        })
        may_write = list(params["autonomy"]["may_write"]) + ["research_task"]
        if not canonical_writes:
            may_write = [item for item in may_write if item not in {"claim", "evidence"}]
        params["autonomy"] = {
            **params["autonomy"],
            "automation_principal": "automation:test",
            "may_write": may_write,
        }
        if not sec_connected:
            for source in params["source_plan"]:
                if source["source_ref"] == "source:sec-edgar":
                    source["status"] = "not_connected"
        for key in ("playbook_ref", "constitution_ref", "mandate_ref"):
            params.pop(key, None)
        self.mission = CoverageMissionAuthority(self.store).create_mission(
            params.pop("mission_ref"), **params
        )

        self.source = AnnualSourceHarness(self.harness.planner)
        case.addCleanup(self.source.close)
        self.registration = seed_core_registration(self.harness.planner, self.source)
        self._write_feedback()

        helper = annual_support.RegisteredAnnualReportExecutorTests
        draft_family = "annual-producer"
        verifier_family = draft_family if same_family else "annual-independent"
        self.draft_profile = helper._model_profile(
            stage="mission-draft", capability="capability:dalton:model:qualitative-research",
            slot="credential-slot:model:mission-draft", family=draft_family,
        )
        self.verifier_profile = helper._model_profile(
            stage="mission-verifier", capability="capability:dalton:model:qualitative-verifier",
            slot="credential-slot:model:mission-verifier", family=verifier_family,
        )
        for profile in (self.draft_profile, self.verifier_profile):
            profile["availability"] = {
                "state": "available", "checked_at": "2026-09-01T00:00:00+00:00",
                "valid_until": "2027-09-11T00:00:00+00:00",
            }
        self.draft_policy = helper._model_policy(
            stage="mission-draft", profile_ids=[self.draft_profile["id"]],
            capability="capability:dalton:model:qualitative-research", tier="brain",
        )
        self.verifier_policy = helper._model_policy(
            stage="mission-verifier", profile_ids=[self.verifier_profile["id"]],
            capability="capability:dalton:model:qualitative-verifier", tier="verifier",
        )
        self.verifier_policy["filters"]["family_independence_capabilities"] = [
            "capability:dalton:model:qualitative-verifier"
        ]
        if unusable_route:
            self.draft_policy["filters"]["allowed_providers"] = ["other"]
        self.router_path = self.state / "model-router.sqlite"
        self.router = ModelRouter(self.router_path, clock=self.harness.clock)
        case.addCleanup(self.router.close)
        for value in (self.draft_profile, self.verifier_profile):
            self.router.register_profile(value)
        for value in (self.draft_policy, self.verifier_policy):
            self.router.register_policy(value)
        self._write_configs()
        self.authority = MissionAnnualResearchAuthority(
            self.store, state_dir=self.state, registry=self.source.registry,
            router=self.router, clock=self.harness.clock,
        )

    def _write_feedback(self, marker: str = "missing-kpi", *, targets=None) -> None:
        runs = self.state / "company-dossier-runs"
        runs.mkdir(mode=0o700, exist_ok=True)
        signature = "ledger:" + marker
        digest = run_digest(COMPANY, signature)
        directory = runs / digest
        directory.mkdir(mode=0o700)
        ticket = {
            "id": "company-dossier-run:" + digest, "company_ref": COMPANY,
            "signature": signature, "run_digest": digest, "status": "succeeded",
            "exit_code": 0, "completed_at": (
                "2026-09-11T13:00:00+00:00" if marker != "missing-kpi"
                else "2026-09-11T12:00:00+00:00"
            ),
        }
        summary = {
            "status": "succeeded", "company_ref": COMPANY,
            "dossier_status": "insufficient_evidence",
            "repair_targets": ([{
                "unit": "kpi_dictionary", "code": "missing_evidence",
                "detail": "retention numerator and denominator",
            }] if targets is None else targets),
        }
        for name, value in (("ticket.json", ticket), ("summary.json", summary)):
            path = directory / name
            path.write_text(json.dumps(value), encoding="utf-8")
            os.chmod(path, 0o600)

    def _write_configs(self) -> None:
        key = self.state / "broker.key"
        key.write_text("fixture", encoding="utf-8")
        os.chmod(key, 0o600)
        common = {
            "model_router_db": str(self.router_path.resolve()),
            "broker_socket": str((self.state / "broker.sock").resolve()),
            "broker_auth_key": str(key.resolve()), "broker_client_id": "client:test",
            "expected_agent_id": "test", "budget_db": str((self.state / "budget.sqlite").resolve()),
            "budget_policy_ref": "budget-policy:mission-annual:1",
            "call_budget": {"max_input_tokens": 32000, "max_output_tokens": 4000,
                            "max_cost_usd": 1.0, "timeout_seconds": 120},
            "run_budget": {"max_units": 1},
        }
        self.budget = ThesisImpactBudgetStore(
            (self.state / "budget.sqlite").resolve(), clock=self.harness.clock
        )
        self.case.addCleanup(self.budget.close)
        self.budget.register_policy(
            policy_version_id="budget-policy:mission-annual:1",
            day_cap_micros=10_000_000,
        )
        for path, policy, profile in (
            (self.state / DRAFT_MODEL_CONFIG_NAME, self.draft_policy, self.draft_profile),
            (self.state / VERIFIER_MODEL_CONFIG_NAME, self.verifier_policy, self.verifier_profile),
        ):
            value = {
                **common, "routing_policy_ref": policy["policy_version_ref"],
                "credential_slot_refs": [profile["credential_slot_ref"]],
            }
            path.write_text(canonical_json(value) + "\n", encoding="utf-8")
            os.chmod(path, 0o600)

    def args(self, **overrides):
        feedback = read_dossier_repair_feedback(self.state)[COMPANY]
        target = feedback["repair_targets"][0]
        values = {
            "operation": OPERATION, "workflow_contract_ref": WORKFLOW_CONTRACT_REF,
            "mission_version_ref": self.mission["id"],
            "mission_version_hash": self.mission["content_hash"],
            "company_ref": COMPANY, "actor_ref": "automation:test",
            "repair_feedback_ref": feedback["id"],
            "repair_feedback_hash": feedback["content_hash"],
            "repair_target_ref": target["id"], "repair_target_hash": target["content_hash"],
            "inquiry": {
                "rank": 0, "company_ref": COMPANY,
                "question": "How is 客户留存 defined and calculated?",
                "wants": "Find the accounting definition and numerator/denominator",
                "because": "The Dossier KPI dictionary lacks cited provenance",
                "repair_target_ref": target["id"],
                "repair_target_hash": target["content_hash"],
            },
            "query_rationale": (
                "Translate the Chinese retention gap into the filing's English accounting terms"
            ),
            "review_ref": self.registration["review_ref"], "issuer_cik": CIK,
            "accession": ACCESSION,
            "query_terms": ["customer segments", "outsourcing partners"],
            "limits": {
                "max_query_terms": 8, "max_results": 12,
                "max_source_bytes": 4 * 1024 * 1024,
                "context_before_chars": 180, "context_after_chars": 520,
            },
            # Targeted search proves the complete, readable acquired rendering
            # itself. It need not pretend every broad-reading window finished.
            "document_read_proof_ref": None,
        }
        values.update(overrides)
        return values


class MissionAnnualResearchTests(unittest.TestCase):
    def _executor(self, fixture):
        statement = (
            "The company serves varied customers and depends on outsourcing partners."
        )
        draft = CountingFakeAdapter({
            "schema_version": "0.1", "answer": statement,
            "candidate": {
                "normalized_statement": statement,
                "metric_or_aspect": "customer and operating dependencies",
                "period": "FY2025 annual report", "basis": "reported",
                "cited_match_indexes": [0],
            },
        })
        verifier = CountingFakeAdapter({
            "schema_version": "0.1", "verdict": "pass",
            "verified_statement": statement, "findings": [],
        })
        scheduler = fixture.harness.scheduler()
        draft_worker = RegisteredAnnualReportDraftWorker(
            scheduler=scheduler, router=fixture.router, adapter=draft,
            store=fixture.store, observability=fixture.harness.observability,
            polish_worker=None,
            routing_policy_ref=fixture.draft_policy["policy_version_ref"],
            credential_slot_refs=(fixture.draft_profile["credential_slot_ref"],),
            budget_store=fixture.budget,
            budget_policy_ref="budget-policy:mission-annual:1",
            mission_resolver=lambda ref, company: read_active_annual_budget_mission(
                fixture.store.connection, ref, company, now=fixture.harness.clock()
            ),
            mission_annual_research_authority=fixture.authority,
            clock=fixture.harness.clock,
        )
        verifier_worker = RegisteredAnnualReportVerifierWorker(
            scheduler=scheduler, router=fixture.router, adapter=verifier,
            store=fixture.store, observability=fixture.harness.observability,
            polish_worker=None,
            routing_policy_ref=fixture.verifier_policy["policy_version_ref"],
            credential_slot_refs=(fixture.verifier_profile["credential_slot_ref"],),
            budget_store=fixture.budget,
            budget_policy_ref="budget-policy:mission-annual:1",
            mission_resolver=lambda ref, company: read_active_annual_budget_mission(
                fixture.store.connection, ref, company, now=fixture.harness.clock()
            ),
            mission_annual_research_authority=fixture.authority,
            clock=fixture.harness.clock,
        )
        executor = MissionAnnualResearchExecutor(
            authority=fixture.authority, scheduler=scheduler,
            registry=fixture.source.registry, draft_worker=draft_worker,
            verifier_worker=verifier_worker, staging=fixture.harness.staging,
            actor_ref="automation:test", clock=fixture.harness.clock,
        )
        return executor, draft, verifier

    def test_exact_admission_is_append_only_idempotent_and_resolves_without_dispatch(self):
        fixture = MissionAnnualFixture(self)
        before_work = fixture.store.connection.execute(
            "SELECT count(*) FROM scheduler_work_orders"
        ).fetchone()[0]
        before_routes = len(fixture.router.list_decisions())
        admitted = fixture.authority.admit(**fixture.args())
        duplicate = fixture.authority.admit(**fixture.args())
        resolved = fixture.authority.resolve_for_execution(admitted["id"])
        self.assertEqual(admitted["status_marker"], "fresh")
        self.assertEqual(duplicate["status_marker"], "duplicate")
        self.assertEqual(resolved["id"], admitted["id"])
        self.assertEqual(admitted["request"]["limits"]["max_results"], 12)
        self.assertEqual(
            fixture.store.connection.execute(
                "SELECT count(*) FROM mission_annual_research_admissions"
            ).fetchone()[0], 1,
        )
        self.assertEqual(fixture.store.connection.execute(
            "SELECT count(*) FROM scheduler_work_orders"
        ).fetchone()[0], before_work)
        self.assertEqual(len(fixture.router.list_decisions()), before_routes)
        with self.assertRaisesRegex(sqlite3.IntegrityError, "append-only"):
            fixture.store.connection.execute(
                "UPDATE mission_annual_research_admissions SET company_ref='company:other'"
            )

    def test_foreign_scope_stale_authority_unsupported_workflow_and_actor_are_refused(self):
        fixture = MissionAnnualFixture(self)
        cases = (
            ({"company_ref": "company:other"}, LanePreconditionError),
            ({"mission_version_hash": "0" * 64}, MissionAnnualResearchError),
            ({"repair_target_hash": "0" * 64}, MissionAnnualResearchError),
            ({"inquiry": {
                **fixture.args()["inquiry"], "repair_target_ref": "dossier-repair-target:other",
            }}, MissionAnnualResearchError),
            ({"accession": "0000320193-25-000080"}, RegisteredAnnualReportError),
            ({"workflow_contract_ref": "workflow:unsupported"}, MissionAnnualResearchError),
            ({"actor_ref": "human:owner"}, MissionAnnualResearchError),
        )
        for overrides, error_type in cases:
            with self.subTest(overrides=overrides), self.assertRaises(error_type):
                fixture.authority.admit(**fixture.args(**overrides))
        self.assertEqual(fixture.store.connection.execute(
            "SELECT count(*) FROM mission_annual_research_admissions"
        ).fetchone()[0], 0)

    def test_unconnected_source_and_nonindependent_verifier_are_refused(self):
        with self.subTest("unconnected"):
            fixture = MissionAnnualFixture(self, sec_connected=False)
            with self.assertRaises(LanePreconditionError):
                fixture.authority.admit(**fixture.args())
        with self.subTest("same-family"):
            fixture = MissionAnnualFixture(self, same_family=True)
            with self.assertRaisesRegex(MissionAnnualResearchError, "independent"):
                fixture.authority.admit(**fixture.args())
        with self.subTest("policy-filter"):
            fixture = MissionAnnualFixture(self, unusable_route=True)
            with self.assertRaisesRegex(MissionAnnualResearchError, "no eligible model"):
                fixture.authority.admit(**fixture.args())

    def test_budget_refusal_and_stale_feedback_have_no_model_side_effect(self):
        fixture = MissionAnnualFixture(self, mission_calls=1)
        before = len(fixture.router.list_decisions())
        with self.assertRaises(RegisteredAnnualReportError):
            fixture.authority.admit(**fixture.args())
        self.assertEqual(len(fixture.router.list_decisions()), before)
        self.assertEqual(fixture.store.connection.execute(
            "SELECT count(*) FROM mission_annual_research_admissions"
        ).fetchone()[0], 0)

        healthy = MissionAnnualFixture(self)
        admitted = healthy.authority.admit(**healthy.args())
        healthy._write_feedback("cleared", targets=[])
        with self.assertRaisesRegex(MissionAnnualResearchError, "stale"):
            healthy.authority.resolve_for_execution(admitted["id"])

    def test_translated_query_terms_are_bound_without_literal_target_matching(self):
        fixture = MissionAnnualFixture(self)
        admitted = fixture.authority.admit(**fixture.args())
        self.assertIn("客户留存", admitted["planner_inquiry"]["question"])
        self.assertEqual(
            admitted["request"]["query_terms"],
            ["customer segments", "outsourcing partners"],
        )

    def test_executor_runs_registered_retrieval_models_and_draft_only_staging(self):
        fixture = MissionAnnualFixture(self)
        admission = fixture.authority.admit(**fixture.args())
        executor, draft, verifier = self._executor(fixture)
        self.assertTrue(all(
            _WORK_ID_RE.fullmatch(work["id"])
            for work in executor._blueprints(admission)
        ))
        outcomes = [executor.run_once(admission["id"]) for _ in range(9)]
        self.assertEqual(
            [item["status"] for item in outcomes],
            ["admitted", "succeeded", "admitted", "succeeded", "admitted",
             "succeeded", "admitted", "complete", "complete"],
            outcomes,
        )
        self.assertEqual(len(fixture.router.list_decisions()), 2)
        self.assertEqual((draft.calls, verifier.calls), (1, 1))
        self.assertEqual(fixture.budget.connection.execute(
            "SELECT count(*) FROM thesis_impact_day_admissions "
            "WHERE work_order_ref LIKE 'work:mission-annual-research-%'"
        ).fetchone()[0], 2)
        self.assertEqual(fixture.store.connection.execute(
            "SELECT count(*) FROM mission_annual_research_starts"
        ).fetchone()[0], 1)
        outcome = fixture.store.connection.execute(
            "SELECT * FROM mission_annual_research_outcomes"
        ).fetchone()
        self.assertIsNotNone(outcome)
        self.assertEqual(outcome["repair_target_ref"], admission["repair_target_ref"])
        self.assertEqual(outcomes[-1]["research_status"], "candidate_staged")
        self.assertEqual(outcomes[-2]["outcome_ref"], outcomes[-1]["outcome_ref"])
        evidence = json.loads(fixture.harness.staging.connection.execute(
            "SELECT record_json FROM candidate_evidence_versions"
        ).fetchone()[0])
        self.assertTrue(evidence["source_envelope_ref"].startswith("source-envelope:"))
        self.assertTrue(evidence["artifact_refs"][0]["ref"].startswith("artifact-version:"))
        self.assertEqual(
            evidence["source_envelope_ref"], fixture.source.manifest["source_envelope_ref"]
        )
        self.assertEqual(fixture.harness.staging.counts(), {
            "candidate_source_materials": 1, "candidate_verifications": 1,
            "candidate_numeric_specs": 0, "candidate_evidence_versions": 1,
            "candidate_claim_versions": 1, "candidate_stage_requests": 1,
            "candidate_figures": 0,
        })

    def test_remaining_day_budget_refusal_is_terminal_before_model_adapter(self):
        fixture = MissionAnnualFixture(self)
        admission = fixture.authority.admit(**fixture.args())
        executor, draft, verifier = self._executor(fixture)
        consumed = fixture.budget.admit(
            policy_version_id="budget-policy:mission-annual:1",
            day=NOW.date().isoformat(), work_order_ref="work:other-budget-consumer",
            attempt_number=1, phase="assessment",
            route_decision_ref="route:other-budget-consumer",
            reserved_micros=9_500_000,
        )
        self.assertEqual(consumed["status"], "fresh")
        for _ in range(3):
            executor.run_once(admission["id"])
        refused = executor.run_once(admission["id"])
        self.assertEqual(refused["status"], "failed")
        self.assertEqual(draft.calls, 0)
        self.assertEqual(verifier.calls, 0)
        formal = fixture.harness.scheduler().formal_result(refused["work_order_ref"])
        self.assertEqual(
            formal["result_envelope"]["error"]["code"],
            "MODEL_CHAIN_HALTED",
        )
        self.assertEqual(fixture.store.connection.execute(
            "SELECT count(*) FROM model_invocations WHERE work_order_ref=?",
            (refused["work_order_ref"],),
        ).fetchone()[0], 0)

    def test_model_worker_re_resolves_mission_authority_and_rejects_metadata_claim(self):
        fixture = MissionAnnualFixture(self)
        admission = fixture.authority.admit(**fixture.args())
        executor, _draft, _verifier = self._executor(fixture)
        for _ in range(3):
            executor.run_once(admission["id"])
        blueprint = executor._blueprints(admission)
        work = executor._derive_work(admission, blueprint, 1)
        work["metadata"]["repair_target_hash"] = "0" * 64
        with self.assertRaisesRegex(AnnualReportQualitativeError, "authority is invalid"):
            executor.draft_worker._work(work)

    def test_model_worker_rejects_substituted_question_and_request_binding_before_send(self):
        fixture = MissionAnnualFixture(self)
        admission = fixture.authority.admit(**fixture.args())
        executor, draft, _verifier = self._executor(fixture)
        for _ in range(3):
            executor.run_once(admission["id"])
        work = executor._derive_work(admission, executor._blueprints(admission), 1)
        work["question"] = "Use another mission and source instead"
        work["metadata"]["question"] = "Use another mission and source instead"
        work["metadata"]["prompt_hash"] = content_hash(work["question"])
        work["metadata"]["model_request_binding_hash"] = "0" * 64
        with self.assertRaisesRegex(AnnualReportQualitativeError, "authority is invalid"):
            executor.draft_worker._work(work)
        self.assertEqual(draft.calls, 0)
        self.assertEqual(fixture.budget.connection.execute(
            "SELECT count(*) FROM thesis_impact_day_admissions"
        ).fetchone()[0], 0)

    def test_foreign_stage_completion_cannot_forge_resolved_outcome(self):
        fixture = MissionAnnualFixture(self)
        admission = fixture.authority.admit(**fixture.args())
        executor, _draft, _verifier = self._executor(fixture)
        for _ in range(7):
            executor.run_once(admission["id"])
        work = executor._derive_work(admission, executor._blueprints(admission), 3)
        scheduler = fixture.harness.scheduler()
        claim = scheduler.claim("worker:foreign", work_order_id=work["id"])
        self.assertIsNotNone(claim)
        forged = {
            "authority_ref": admission["id"],
            "repair_target_ref": admission["repair_target_ref"],
            "repair_target_hash": admission["repair_target_hash"],
            "research_status": "dossier_resolved",
            "candidate_evidence_ref": "evidence:forged",
            "candidate_evidence_hash": "1" * 64,
            "candidate_claim_ref": "claim:forged",
            "candidate_claim_hash": "2" * 64,
        }
        envelope = ResultEnvelope(
            schema_version="0.1", id="result-envelope:foreign-stage",
            created_at=NOW.isoformat(), work_order_ref=work["id"],
            invocation_ref="execution:foreign", status="succeeded", outputs=forged,
            actual_side_effects=(), usage_refs=(), artifact_refs=(), error=None,
            metadata={"authority_ref": admission["id"]},
        ).to_dict()
        scheduler.complete(
            work["id"], claim["attempt"]["attempt_number"], "worker:foreign",
            claim["lease_token"], envelope,
            idempotency_key="foreign-stage-completion",
            result_envelope_hash=content_hash(envelope),
        )
        with self.assertRaisesRegex(
            MissionAnnualResearchExecutorError, "not completed by the mission executor"
        ):
            executor.run_once(admission["id"])
        self.assertEqual(fixture.harness.staging.counts(), {
            "candidate_source_materials": 0, "candidate_verifications": 0,
            "candidate_numeric_specs": 0, "candidate_evidence_versions": 0,
            "candidate_claim_versions": 0, "candidate_stage_requests": 0,
            "candidate_figures": 0,
        })
        self.assertEqual(fixture.store.connection.execute(
            "SELECT count(*) FROM mission_annual_research_outcomes"
        ).fetchone()[0], 0)

    def test_stage_claim_precedes_side_effect_and_restart_converges_after_crash(self):
        fixture = MissionAnnualFixture(self)
        admission = fixture.authority.admit(**fixture.args())
        executor, _draft, _verifier = self._executor(fixture)
        for _ in range(7):
            executor.run_once(admission["id"])
        work = executor._derive_work(admission, executor._blueprints(admission), 3)
        foreign = executor.scheduler.claim("worker:foreign", work_order_id=work["id"])
        self.assertIsNotNone(foreign)
        self.assertEqual(executor.run_once(admission["id"])["status"], "pending")
        self.assertEqual(fixture.harness.staging.counts()["candidate_stage_requests"], 0)

    def test_stage_restart_reuses_exact_candidate_after_post_stage_crash(self):
        fixture = MissionAnnualFixture(self)
        admission = fixture.authority.admit(**fixture.args())
        executor, _draft, _verifier = self._executor(fixture)
        for _ in range(7):
            executor.run_once(admission["id"])

        original_complete = executor.scheduler.complete
        crashed = {"done": False}

        def crash_after_staging(*args, **kwargs):
            if not crashed["done"]:
                crashed["done"] = True
                raise RuntimeError("injected crash after candidate staging")
            return original_complete(*args, **kwargs)

        executor.scheduler.complete = crash_after_staging
        with self.assertRaisesRegex(RuntimeError, "injected crash"):
            executor.run_once(admission["id"])
        self.assertEqual(fixture.harness.staging.counts()["candidate_stage_requests"], 1)
        executor.scheduler.complete = original_complete
        fixture.harness.clock.value += timedelta(hours=1)
        completed = executor.run_once(admission["id"])
        self.assertEqual(completed["status"], "complete")
        self.assertEqual(fixture.harness.staging.counts()["candidate_stage_requests"], 1)
        replay = executor.run_once(admission["id"])
        self.assertEqual(replay["outcome_ref"], completed["outcome_ref"])

    def test_policy_promotes_exact_annual_candidate_to_canonical_evidence_and_claim(self):
        fixture = MissionAnnualFixture(self, auto_commit=True)
        before_signature = ledger_signature(fixture.store.connection)
        admission = fixture.authority.admit(**fixture.args())
        executor, draft, verifier = self._executor(fixture)
        outcomes = [executor.run_once(admission["id"]) for _ in range(9)]
        promoted = outcomes[-1]
        self.assertEqual(promoted["status"], "complete")
        self.assertEqual(promoted["research_status"], "canonical_claim_promoted")
        self.assertEqual((draft.calls, verifier.calls), (1, 1))
        self.assertIsNotNone(fixture.store.get_claim(promoted["claim_version_ref"]))
        self.assertEqual(fixture.store.connection.execute(
            "SELECT count(*) FROM evidence_versions WHERE evidence_version_id=?",
            (promoted["evidence_version_ref"],),
        ).fetchone()[0], 1)
        self.assertEqual(fixture.store.connection.execute(
            "SELECT count(*) FROM mission_annual_research_promotions"
        ).fetchone()[0], 1)
        self.assertNotEqual(
            ledger_signature(fixture.store.connection),
            before_signature,
        )
        canonical_evidence = json.loads(fixture.store.connection.execute(
            "SELECT evidence_json FROM evidence_versions WHERE evidence_version_id=?",
            (promoted["evidence_version_ref"],),
        ).fetchone()[0])
        self.assertEqual(canonical_evidence["source_type"], "official_filing")
        self.assertEqual(
            canonical_evidence["source_envelope_ref"],
            fixture.source.manifest["source_envelope_ref"],
        )
        self.assertEqual(
            canonical_evidence["artifact_refs"][0]["ref"],
            fixture.source.manifest["raw_artifact_version_ref"],
        )
        staged_outcome = json.loads(fixture.store.connection.execute(
            "SELECT record_json FROM mission_annual_research_outcomes"
        ).fetchone()[0])
        self.assertEqual(staged_outcome["research_status"], "candidate_staged")
        replay = executor.run_once(admission["id"])
        self.assertEqual(replay["promotion_ref"], promoted["promotion_ref"])
        self.assertEqual((draft.calls, verifier.calls), (1, 1))

    def test_policy_promotion_refuses_missing_exact_budget_authority(self):
        fixture = MissionAnnualFixture(self, auto_commit=True)
        admission = fixture.authority.admit(**fixture.args())
        executor, draft, verifier = self._executor(fixture)
        outcomes = [executor.run_once(admission["id"]) for _ in range(8)]
        self.assertEqual(outcomes[-1]["research_status"], "candidate_staged")
        budget_path = Path(fixture.budget.path)
        moved = budget_path.with_suffix(".unavailable")
        budget_path.rename(moved)
        self.addCleanup(lambda: moved.rename(budget_path) if moved.exists() else None)
        with self.assertRaisesRegex(
            MissionAnnualResearchExecutorError, "budget policy is unavailable"
        ):
            executor.run_once(admission["id"])
        self.assertEqual((draft.calls, verifier.calls), (1, 1))
        self.assertEqual(fixture.store.connection.execute(
            "SELECT count(*) FROM evidence_versions"
        ).fetchone()[0], 0)
        self.assertEqual(fixture.store.connection.execute(
            "SELECT count(*) FROM claim_versions"
        ).fetchone()[0], 0)

    def test_policy_promotion_requires_mission_evidence_and_claim_permissions(self):
        fixture = MissionAnnualFixture(
            self, auto_commit=True, canonical_writes=False
        )
        admission = fixture.authority.admit(**fixture.args())
        executor, draft, verifier = self._executor(fixture)
        outcomes = [executor.run_once(admission["id"]) for _ in range(8)]
        self.assertEqual(outcomes[-1]["research_status"], "candidate_staged")
        with self.assertRaisesRegex(
            ResearchAutoCommitRejected, "does not grant canonical research writes"
        ):
            executor.run_once(admission["id"])
        self.assertEqual((draft.calls, verifier.calls), (1, 1))
        self.assertEqual(fixture.store.connection.execute(
            "SELECT count(*) FROM claim_versions"
        ).fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
