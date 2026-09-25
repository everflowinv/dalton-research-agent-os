from __future__ import annotations

import copy
import hashlib
import json
import sqlite3
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
import dalton_core.mission_document_research_executor as executor_module

from dalton_core.cockpit_model import CockpitModel
from dalton_core.contracts import ModelInvocation, ResultEnvelope, WorkOrder
from dalton_core.coverage_mission import CoverageMissionAuthority
from dalton_core.document_research import (
    CoreAcquiredDocumentSourceAdapter, DocumentResearchRegistry, FeedDocumentSourceAdapter,
    build_document_research_policy,
)
from dalton_core.document_research_strategy import (
    FINANCIAL_NOTE_TARGET_REF,
    FINANCIAL_NOTE_TARGET_SCHEMA_VERSION,
    STRATEGY_VERSION,
)
from dalton_core.feed_acquisition import (
    COMPANY_WIKI_SOURCE_REF, build_feed_acquisition_manifest,
)
from dalton_core.host_tool_runner import ACCESS_POLICY_REF, RETENTION_POLICY_REF, TERMS_POLICY_REF
from dalton_core.mission_document_research import (
    MissionDocumentResearchAuthority, MissionDocumentResearchError, PURPOSE,
)
from dalton_core.model_accounting import record_model_accounting
from dalton_core.document_research_qualitative import (
    MissionDocumentDraftWorker, MissionDocumentVerifierWorker,
    _MissionDocumentCandidateAuthority, _build_candidate_bundle,
)
from dalton_core.mission_document_research_executor import (
    MissionDocumentResearchExecutor, MissionDocumentResearchExecutorError,
    effective_mission_document_work_orders,
    exact_mission_document_model_execution_authority,
    read_mission_document_research_observations,
    _formal_hash, _formal_ref,
)
from dalton_core.mission_document_research_lane import (
    MissionDocumentResearchCoordinator,
)
from dalton_core.annual_report_qualitative import AnnualReportQualitativeError
from dalton_core.annual_report_runtime import (
    DRAFT_MODEL_CONFIG_NAME, VERIFIER_MODEL_CONFIG_NAME,
)
from dalton_core.raw_spool import RawSpool
from dalton_core.research_question_backlog import ResearchQuestionBacklog
from dalton_core.research_verification import MISSION_DOCUMENT_AUTHORITY_MODE
from dalton_core.scheduler import Scheduler
from dalton_core.research_planner import build_prompt, project_state_for_prompt
from dalton_core.research_planner_cli import run_planner
from dalton_core.research_task import inquiry_content_hash, inquiry_ref_for
from dalton_core.store import canonical_json, content_hash
from tests.test_document_research import FakeLauncher, FakeReceiptReader
from tests.test_mission_annual_research import (
    COMPANY, CountingFakeAdapter, MissionAnnualFixture, NOW,
)


class CapacityOnceAdapter(CountingFakeAdapter):
    failed_work_id = None

    def execute(self, work, route, selected):
        invocation, result = super().execute(work, route, selected)
        if self.failed_work_id is None:
            self.failed_work_id = work.id
        if work.id != self.failed_work_id:
            return invocation, result
        return invocation, ResultEnvelope(
            schema_version="0.1", id="result:capacity:" + work.id.rsplit("-", 1)[-1],
            created_at=NOW.isoformat(), work_order_ref=work.id,
            invocation_ref=invocation.id, status="failed", outputs={},
            actual_side_effects=(), usage_refs=(), artifact_refs=(),
            error={"code": "BUSY", "message": "capacity unavailable"},
            metadata={
                "route_decision_ref": route["id"],
                "dispatch_proof": {"authority": "openclaw-model-adapter",
                                   "state": "definitely_not_sent", "version": "0.1"},
            },
        )


class AlwaysCapacityAdapter(CapacityOnceAdapter):
    def execute(self, work, route, selected):
        self.failed_work_id = work.id
        return super().execute(work, route, selected)


class DefinitelyNotSentFirstWorkAdapter(CountingFakeAdapter):
    failed_work_id = None

    def execute(self, work, route, selected):
        from dalton_core.openclaw_model_adapter import BrokerDefinitelyNotSent
        self.calls += 1
        if self.failed_work_id is None:
            self.failed_work_id = work.id
        if work.id == self.failed_work_id:
            raise BrokerDefinitelyNotSent("connect failed before send")
        # Avoid CountingFakeAdapter's second increment on successful recovery.
        from tests.test_transcript_polish_model_worker import FakeAdapter
        return FakeAdapter.execute(self, work, route, selected)


class ContractRejectOnceAdapter(CountingFakeAdapter):
    """Reject the output contract on the first Work of a stage, then answer.

    The live shape this stands for: the provider request happened and was
    charged, and only the reply body was unusable -- the one paid failure whose
    replay is honest, and the exact case the bounded automatic retry exists for.
    """

    def __init__(self, good_wire, bad_wire):
        super().__init__(good_wire)
        self.good_wire, self.bad_wire = good_wire, bad_wire
        self.rejected_work_id = None

    def execute(self, work, route, selected):
        if self.rejected_work_id is None:
            self.rejected_work_id = work.id
        self.candidate_wire = (
            self.bad_wire if work.id == self.rejected_work_id else self.good_wire)
        return super().execute(work, route, selected)


class UnprovedSendOnceAdapter(CountingFakeAdapter):
    """Fail the first Work of a stage unclassifiably, then answer.

    The live shape this stands for: the broker raised after a boundary nothing
    can place, so no receipt says the request was never sent and no settled
    charge says it was.  That is ``send_state_unproved``, and it is the exact
    state the bounded automatic retry exists for.
    """

    failed_work_id = None

    def execute(self, work, route, selected):
        from dalton_core.openclaw_model_adapter import BrokerConnectionError
        if self.failed_work_id is None:
            self.failed_work_id = work.id
        if work.id == self.failed_work_id:
            self.calls += 1
            raise BrokerConnectionError("socket failed after an unknown boundary")
        # Avoid CountingFakeAdapter's second increment on successful recovery.
        from tests.test_transcript_polish_model_worker import FakeAdapter
        return FakeAdapter.execute(self, work, route, selected)


class AlwaysUnprovedSendAdapter(CountingFakeAdapter):
    def execute(self, work, route, selected):
        from dalton_core.openclaw_model_adapter import BrokerConnectionError
        self.calls += 1
        raise BrokerConnectionError("socket failed after an unknown boundary")


class ProviderBudgetExceededAdapter(CountingFakeAdapter):
    """Every call is sent, charged, and then refused on its token telemetry.

    Live, 2026-09-25: a claude-cli-gateway draft reported 65,831 cache-write +
    2,991 cache-read + 2 input + 803 output tokens against a 68,096-token
    WorkOrder total, and the adapter refused the paid result with
    ``PROVIDER_BUDGET_EXCEEDED``.
    """

    def execute(self, work, route, selected):
        from dataclasses import replace
        from tests.test_transcript_polish_model_worker import FakeAdapter
        self.calls += 1
        invocation, result = FakeAdapter.execute(self, work, route, selected)
        return invocation, replace(
            result, status="failed", outputs={},
            error={"code": "PROVIDER_BUDGET_EXCEEDED",
                   "message": "provider max_total_tokens telemetry exceeds WorkOrder budget",
                   "source": "openclaw-model-adapter"})


class RouteBoundCountingFakeAdapter(CountingFakeAdapter):
    """Keep the shared fixture adapter's invocation faithful to the selected route."""

    def execute(self, work, route, selected):
        invocation, result = super().execute(work, route, selected)
        wire = invocation.to_dict()
        wire["capability"] = route["capability"]
        return ModelInvocation.from_dict(wire), result


class EstimatedCostAdapter(RouteBoundCountingFakeAdapter):
    def execute(self, work, route, selected):
        invocation, result = super().execute(work, route, selected)
        wire = invocation.to_dict()
        wire["usage"]["raw_provider_telemetry"]["cost"] = {
            "available": False, "usd": None,
        }
        return ModelInvocation.from_dict(wire), result


class HistoricalControlsFailureAdapter(RouteBoundCountingFakeAdapter):
    def execute(self, work, route, selected):
        invocation, _result = super().execute(work, route, selected)
        wire = invocation.to_dict()
        wire["usage"]["input_tokens"] = None
        wire["usage"]["output_tokens"] = None
        wire["usage"]["total_tokens"] = None
        invocation = ModelInvocation.from_dict(wire)
        response_hash = "9" * 64
        return invocation, ResultEnvelope(
            schema_version="0.1", id="result:historical-controls-test",
            created_at=NOW.isoformat(), work_order_ref=work.id,
            invocation_ref=invocation.id, status="failed", outputs={},
            actual_side_effects=(), usage_refs=(), artifact_refs=(),
            error={"code": "REQUIRED_CONTROLS_UNAVAILABLE", "message": "historical"},
            metadata={
                "route_decision_ref": route["id"], "broker_response_hash": response_hash,
                "profile_version_ref": route["selected_profile_version_ref"],
                "required_provider_controls": True,
                "provider_control_mode": "provider-controlled-v1",
                "dispatch_proof": None,
            },
        )


class MissionDocumentResearchTests(unittest.TestCase):
    @staticmethod
    def _financial_note_target(**overrides):
        return {
            "schema_version": FINANCIAL_NOTE_TARGET_SCHEMA_VERSION,
            "target_ref": FINANCIAL_NOTE_TARGET_REF,
            "kind": "diluted_eps_numerator",
            "statement_ingest_ref": "statement-ingest:" + "a" * 32,
            "statement_filing_hash": "b" * 64,
            "accession": "0001467373-25-000217", "form": "10-K",
            "applicability_kind": "annual",
            "periods": [{"period_start": "2024-09-01", "period_end": "2025-08-31"}],
            **overrides,
        }

    def _executor(self, fixture, authority, *, draft_adapter=None,
                  verifier_adapter=None, fault_injector=None,
                  max_automatic_contract_retries_per_day=None,
                  max_automatic_unproved_send_retries_per_day=None):
        statement = "Managed services revenue is recognized over time."
        draft_adapter = draft_adapter or RouteBoundCountingFakeAdapter({
            "schema_version": "0.1", "status": "answered", "answer": statement,
            "candidate": {"normalized_statement": statement,
                          "metric_or_aspect": "managed services revenue recognition",
                          "period": "current policy", "basis": "reported",
                          "cited_match_indexes": [0]}, "missing": [],
        })
        verifier_adapter = verifier_adapter or RouteBoundCountingFakeAdapter({
            "schema_version": "0.1", "verdict": "pass",
            "verified_statement": statement, "findings": [],
        })
        scheduler = fixture.harness.scheduler()
        common = dict(
            scheduler=scheduler, router=fixture.router, store=fixture.store,
            observability=fixture.harness.observability, polish_worker=None,
            budget_store=fixture.budget,
            budget_policy_ref="budget-policy:mission-annual:1",
            mission_document_research_authority=authority,
            clock=fixture.harness.clock,
        )
        draft = MissionDocumentDraftWorker(
            adapter=draft_adapter,
            routing_policy_ref=fixture.draft_policy["policy_version_ref"],
            credential_slot_refs=(fixture.draft_profile["credential_slot_ref"],),
            **common,
        )
        verifier = MissionDocumentVerifierWorker(
            adapter=verifier_adapter,
            routing_policy_ref=fixture.verifier_policy["policy_version_ref"],
            credential_slot_refs=(fixture.verifier_profile["credential_slot_ref"],),
            **common,
        )
        cap = ({} if max_automatic_contract_retries_per_day is None else
               {"max_automatic_contract_retries_per_day":
                max_automatic_contract_retries_per_day})
        if max_automatic_unproved_send_retries_per_day is not None:
            cap["max_automatic_unproved_send_retries_per_day"] = (
                max_automatic_unproved_send_retries_per_day)
        return MissionDocumentResearchExecutor(
            authority=authority, scheduler=scheduler, registry=authority.registry,
            draft_worker=draft, verifier_worker=verifier,
            staging=fixture.harness.staging, actor_ref="automation:test",
            clock=fixture.harness.clock, fault_injector=fault_injector, **cap,
        ), draft_adapter, verifier_adapter

    @staticmethod
    def _enable_recovery(fixture, *, maximum=2, backoff=0, elapsed=3600):
        recovery = {"max_fresh_work_orders": maximum,
                    "retry_backoff_seconds": backoff,
                    "max_elapsed_seconds": elapsed}
        for name in (DRAFT_MODEL_CONFIG_NAME, VERIFIER_MODEL_CONFIG_NAME):
            path = fixture.state / name
            value = json.loads(path.read_text(encoding="utf-8"))
            value["provider_retry"] = {
                "max_same_profile_retries": 0, "retry_backoff_seconds": 0,
                "unknown_recovery": recovery,
            }
            path.write_text(json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n",
                            encoding="utf-8")

    def _ready_atomic_day_recovery(self, *, draft_adapter=None):
        fixture, authority, args, _registration, _launcher = self._fixture()
        self._enable_recovery(fixture, maximum=1, elapsed=7200)
        admission = authority.admit_from_plan(**args)
        other = fixture.budget.admit(
            policy_version_id="budget-policy:mission-annual:1",
            day=NOW.date().isoformat(), work_order_ref="work:other-settled-consumer",
            attempt_number=1, phase="assessment", route_decision_ref="route:other",
            reserved_micros=9_500_000,
        )
        fixture.budget.settle(other["admission_id"], actual_micros=9_500_000)
        executor, draft, verifier = self._executor(
            fixture, authority, draft_adapter=draft_adapter)
        for _ in range(4):
            failed = executor.run_once(admission["id"])
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(
            executor.run_once(admission["id"])["reason"],
            "fresh_work_recovery_backoff",
        )
        fixture.harness.clock.value += timedelta(hours=12)
        recovery = executor.run_once(admission["id"])
        self.assertEqual(recovery["reason"], "fresh_work_recovery")
        return fixture, authority, admission, executor, draft, verifier, recovery

    def _record_plan(self, fixture, plan):
        raw = {
            "schema_version": "0.1", "assessment": plan["assessment"],
            "directives": [
                {key: value for key, value in item.items() if key != "rank"}
                for item in plan["directives"]
            ],
            "inquiries": [
                {key: value for key, value in item.items()
                 if key not in {"rank", "repair_target_hash"}}
                for item in plan["inquiries"]
            ],
            "sufficiency": [],
        }
        text = json.dumps(raw)
        work = WorkOrder.from_dict({
            "schema_version": "0.1",
            "id": "work:cockpit-plan-" + plan["state_hash"][:32],
            "created_at": fixture.mission["created_at"],
            "updated_at": fixture.mission["created_at"], "question": "fixture plan",
            "requested_capabilities": ["research"],
            "runtime_profile_ref": "runtime-profile:dalton-model-broker:0.1",
            "budget": {"max_input_tokens": 1_000, "max_output_tokens": 1_000,
                       "max_total_tokens": 2_000, "max_cost_usd": 1.0,
                       "max_seconds": 30},
            "idempotency_key": "fixture-plan:" + plan["state_hash"],
            "declared_side_effects": [], "status": "ready", "input_refs": [],
            "metadata": {"control_plane": "cockpit", "purpose": "plan",
                         "request_id": plan["state_hash"][:32],
                         "mission_version_ref": fixture.mission["id"]},
        })
        scheduler = fixture.harness.scheduler()
        self.assertIn(scheduler.enqueue(work.to_dict())["status"], {"fresh", "duplicate"})
        claim = scheduler.claim("worker:fixture-planner", work_order_id=work.id)
        invocation_ref = "model-invocation:fixture-planner:" + plan["state_hash"][:16]
        route = fixture.router.route(
            work, attempt_number=claim["attempt"]["attempt_number"],
            capability="research",
            policy_version_ref=fixture.draft_policy["policy_version_ref"],
            credential_slot_refs=[fixture.draft_profile["credential_slot_ref"]],
            required_modalities=["text"], required_context_tokens=1_000,
            estimated_input_tokens=100, estimated_output_tokens=100,
            idempotency_key="fixture-planner-route:" + plan["state_hash"],
            tier="brain", purpose="registered_annual_report_draft",
        )["decision"]
        route_ref = route["id"]
        envelope = ResultEnvelope(
            schema_version="0.1",
            id="result-envelope:fixture-planner:" + plan["state_hash"][:16],
            created_at=NOW.isoformat(), work_order_ref=work.id,
            invocation_ref=invocation_ref, status="succeeded",
            outputs={"text": text, "content_hash": hashlib.sha256(text.encode()).hexdigest()},
            actual_side_effects=(), usage_refs=(), artifact_refs=(), error=None,
            metadata={"route_decision_ref": route_ref,
                      "profile_version_ref": route["selected_profile_version_ref"]},
        ).to_dict()
        scheduler.complete(
            work.id, claim["attempt"]["attempt_number"], "worker:fixture-planner",
            claim["lease_token"], envelope,
            idempotency_key="fixture-planner:" + plan["state_hash"],
            result_envelope_hash=content_hash(envelope),
        )
        return CoverageMissionAuthority(fixture.store).record_research_plan(
            plan, decided_by=fixture.mission["autonomy"]["automation_principal"],
            work_order_ref=work.id,
        )

    def _fixture(self, *, auto_commit=False, company_in_mandate=True):
        fixture = MissionAnnualFixture(
            self, additional_connected_source=COMPANY_WIKI_SOURCE_REF,
            company_in_mandate=company_in_mandate, auto_commit=auto_commit,
        )
        spool = RawSpool(str(fixture.state / "document-research-spool"), max_total_bytes=2_000_000)
        text = (
            "Revenue recognition policy. Managed services revenue is recognized over time "
            "as the customer receives the service. Contract assets represent earned amounts."
        )
        document_ref = "wiki-document:revenue-policy:1"
        sink = spool.open_sink(
            "raw-sink:" + hashlib.sha256(document_ref.encode()).hexdigest(),
            max_response_bytes=1_000_000,
        )
        sink.write(text.encode())
        assembled = sink.finalize().to_dict()
        receipts = FakeReceiptReader(source_ref=COMPANY_WIKI_SOURCE_REF)
        ticket_ref = "feed-run:mission-document:wiki"
        manifest = build_feed_acquisition_manifest(
            created_at=NOW.isoformat(timespec="microseconds"),
            source_ref=COMPANY_WIKI_SOURCE_REF, operation="get_document",
            document_ref=document_ref, target_ref="host-tool:company-wiki",
            governance_ref="connector-governance:company-wiki:get:approved",
            governance_hash="1" * 64, doc_kind="company_wiki",
            evidence_tier="internal", doc_date="2026-09-10",
            origin_ref="fixture:company-wiki", subject_tickers=["TEST"],
            text=text, assembled_object=assembled,
            connector_invocation_ref=receipts.invocation["id"],
            connector_invocation_hash=receipts.invocation["content_hash"],
        )
        launcher = FakeLauncher(manifest, ticket_ref, state_dir=fixture.state)
        adapter = FeedDocumentSourceAdapter(
            source_ref=COMPANY_WIKI_SOURCE_REF, launcher=launcher, core=fixture.store,
            spool=spool, receipt_reader=receipts,
        )
        policy = build_document_research_policy(
            policy_ref="policy:mission-directed-document:test:0.1",
            allowed_purposes=[PURPOSE], allowed_access_policy_refs=[ACCESS_POLICY_REF],
            max_question_chars=2_000, max_query_terms=10, max_query_term_chars=160,
            max_results=8, max_context_before_chars=80,
            max_context_after_chars=160, max_read_chars=10_000,
        )
        adapters = {COMPANY_WIKI_SOURCE_REF: adapter}
        registry = DocumentResearchRegistry(
            adapters=adapters, policy=policy,
            acquired_fetched_adapter=CoreAcquiredDocumentSourceAdapter(
                core=fixture.store, adapters=adapters,
            ),
        )
        acquired_ref = "mission-discovered-document:mission-document-wiki"
        coverage = CoverageMissionAuthority(fixture.store)
        fixture.store.connection.commit()
        fixture.store.connection.execute("PRAGMA foreign_keys=OFF")
        self.assertEqual(
            fixture.store.connection.execute("PRAGMA foreign_keys").fetchone()[0], 0
        )
        with coverage._transaction() as cur:
            cur.execute(
                "INSERT INTO coverage_mission_discovered_documents"
                "(record_id,mission_version_ref,company_ref,source_ref,document_ref,"
                "discovery_ref,status,ticket_ref,failure_reason,failure_retryable,"
                "created_at,updated_at,host) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (acquired_ref, fixture.mission["id"], COMPANY,
                 COMPANY_WIKI_SOURCE_REF, document_ref,
                 "mission-source-discovery:mission-document-wiki", "acquired",
                 ticket_ref, None, None, NOW.isoformat(timespec="microseconds"),
                 NOW.isoformat(timespec="microseconds"), None),
            )
        fixture.store.connection.execute("PRAGMA foreign_keys=ON")
        registration = registry.register_acquired_document(
            record_id=acquired_ref, purpose=PURPOSE,
        )
        inquiry = {
            "rank": 0, "company_ref": COMPANY,
            "question": "How does the company recognize managed services revenue?",
            "wants": "Identify whether revenue is recognized at a point or over time.",
            "because": "The accounting policy affects revenue comparability.",
            "directed_document": {
                "strategy_version": STRATEGY_VERSION,
                "document_ref": document_ref,
                "document_version_hash": registration["content_hash"],
                "query_terms": ["revenue recognition", "recognized over time"],
                "query_rationale": "Search exact accounting-policy language.",
            },
        }
        plan = {
            "schema_version": "0.1", "task_ref": "task:research-plan-directives:0.1",
            "created_at": NOW.isoformat(timespec="microseconds"),
            "state_hash": "2" * 64,
            "mission_version_ref": fixture.mission["id"],
            "assessment": "Read the selected original document.",
            "directives": [], "inquiries": [inquiry], "sufficiency": [],
        }
        plan["content_hash"] = content_hash(plan)
        stored_plan = self._record_plan(fixture, plan)
        mandate_ref = fixture.mission["bindings"]["mandate_version"]["ref"]
        question = ResearchQuestionBacklog(fixture.store).record_question(
            mandate_version_ref=mandate_ref, company_ref=COMPANY,
            question=inquiry["question"], answer_criteria=inquiry["wants"],
            source_refs=[COMPANY_WIKI_SOURCE_REF],
            actor_ref=fixture.mission["autonomy"]["automation_principal"],
            idempotency_key="mission-document:test:question",
            mission_binding={"ref": fixture.mission["id"], "hash": fixture.mission["content_hash"]},
        )
        registrations = {registration["id"]: registration}
        authority = MissionDocumentResearchAuthority(
            fixture.store, registry=registry,
            registration_resolver=lambda ref: registrations[ref],
            model_execution_resolver=fixture.authority._model_authority,
            planner_scheduler_connection=fixture.harness.scheduler().connection,
            planner_router_connection=fixture.router.connection,
            clock=fixture.harness.clock,
        )
        args = {
            "plan_ref": stored_plan["plan_id"],
            "inquiry_ref": inquiry_ref_for(inquiry_content_hash(inquiry)),
            "question_version_ref": question["question_version_ref"],
            "document_authority_ref": registration["id"],
        }
        return fixture, authority, args, registration, launcher

    def _roll_mission(self, fixture, *, budget=None, universe=None,
                      source_plan=None, version=2):
        """Roll the mission pointer forward, the way a signing does."""

        coverage = CoverageMissionAuthority(fixture.store)
        current = coverage.active_mission(fixture.mission["mission_ref"])
        return coverage.create_mission(
            current["mission_ref"],
            title=current["title"], objective=current["objective"],
            industry_ref=current["industry_ref"],
            universe=current["universe"] if universe is None else universe,
            research_questions=current["research_questions"],
            deliverables=current["deliverables"],
            source_plan=(current["source_plan"] if source_plan is None
                         else source_plan),
            bindings=current["bindings"], autonomy=current["autonomy"],
            budget=current["budget"] if budget is None else budget,
            actor_ref="human:test-owner",
            version_id=f"coverage-mission-version:annual-test:{version}",
            prior_version_ref=current["id"],
            idempotency_key=f"coverage-mission:mission-annual:roll:{version}",
        )

    def test_admission_admitted_under_a_prior_mission_version_still_runs(self):
        """Live: six legacy admissions died on a budget change, unspent.

        Every budget revision and every policy signing rolls the mission
        version.  An admission is bound to the version its plan was written
        against, so before this each signing killed everything held -- with
        ``directed document research requires the active mission``, before a
        single call -- and the lane spent its one automatic retry proving it.
        """

        fixture, authority, args, _registration, _launcher = self._fixture()
        admission = authority.admit_from_plan(**args)
        rolled = self._roll_mission(fixture, budget={
            **fixture.mission["budget"], "max_daily_cost_usd": 9.0})
        self.assertNotEqual(rolled["id"], fixture.mission["id"])
        resolved = authority.resolve_for_execution(admission["id"])
        # The admission still names the version it was admitted under.
        self.assertEqual(resolved["mission_version_ref"], fixture.mission["id"])
        self.assertEqual(resolved["content_hash"], admission["content_hash"])
        mission = authority.active_budget_mission(admission["id"])
        self.assertEqual(mission["mission_version_provenance"], {
            "admitted_mission_version_ref": fixture.mission["id"],
            "admitted_mission_version_hash": fixture.mission["content_hash"],
            "governing_mission_version_ref": rolled["id"],
            "governing_mission_version_hash": rolled["content_hash"],
        })
        # The *new* ceiling is the one that binds the work.
        self.assertEqual(mission["budget"]["max_daily_cost_usd"], 9.0)
        executor, draft, verifier = self._executor(fixture, authority)
        outcomes = [executor.run_once(admission["id"]) for _ in range(9)]
        self.assertEqual([item["status"] for item in outcomes], [
            "admitted", "succeeded", "admitted", "succeeded", "admitted",
            "succeeded", "admitted", "complete", "complete",
        ])
        self.assertEqual((draft.calls, verifier.calls), (1, 1))
        self.assertEqual(outcomes[-1]["research_status"], "candidate_staged")

    def test_mission_roll_that_drops_the_company_or_source_still_refuses(self):
        """Lineage tolerance is not tolerance of a changed mission."""

        fixture, authority, args, _registration, _launcher = self._fixture()
        admission = authority.admit_from_plan(**args)
        self._roll_mission(fixture, universe=[{
            "company_ref": "company:other", "ticker": "OTHR",
            "coverage_tier": "A", "bootstrap_priority": "P0",
        }])
        with self.assertRaisesRegex(
            MissionDocumentResearchError,
            "company is outside the active mission universe",
        ):
            authority.resolve_for_execution(admission["id"])
        dropped = [
            {**item, "status": "not_connected"}
            if item["source_ref"] == COMPANY_WIKI_SOURCE_REF else item
            for item in fixture.mission["source_plan"]
        ]
        self._roll_mission(fixture, source_plan=dropped, version=3,
                           universe=fixture.mission["universe"])
        with self.assertRaisesRegex(
            MissionDocumentResearchError,
            "selected document source is not connected",
        ):
            authority.resolve_for_execution(admission["id"])

    def test_a_version_of_another_mission_is_still_refused(self):
        fixture, authority, args, _registration, _launcher = self._fixture()
        admission = authority.admit_from_plan(**args)
        coverage = CoverageMissionAuthority(fixture.store)
        current = coverage.active_mission(fixture.mission["mission_ref"])
        with patch.object(
            CoverageMissionAuthority, "active_mission",
            return_value={**current, "id": "coverage-mission-version:other:1",
                          "prior_version_ref": None},
        ):
            with self.assertRaisesRegex(
                MissionDocumentResearchError,
                "requires the active mission",
            ):
                authority.resolve_for_execution(admission["id"])

    def test_exact_archived_plan_question_and_registration_admit_and_replay(self):
        fixture, authority, args, registration, _launcher = self._fixture()
        admitted = authority.admit_from_plan(**args)
        self.assertEqual(admitted["status_marker"], "fresh")
        self.assertEqual(admitted["request"]["registration"], registration)
        self.assertEqual(admitted["source_ref"], COMPANY_WIKI_SOURCE_REF)
        self.assertEqual(admitted["request"]["query_terms"], [
            "revenue recognition", "recognized over time",
        ])
        replay = authority.admit_from_plan(**args)
        self.assertEqual(replay["status_marker"], "duplicate")
        self.assertEqual(authority.resolve_for_execution(admitted["id"]), {
            key: value for key, value in admitted.items() if key != "status_marker"
        })
        self.assertEqual(fixture.store.connection.execute(
            "SELECT count(*) FROM mission_document_research_admissions"
        ).fetchone()[0], 1)

    def test_model_policy_roll_refreshes_execution_without_rewriting_admission(self):
        fixture, authority, args, _registration, _launcher = self._fixture()
        admission = authority.admit_from_plan(**args)
        admitted_execution = copy.deepcopy(admission["model_execution"])
        admitted_authority = copy.deepcopy(admission["model_authority"])
        old_works = executor_module._blueprints(admission)
        current_execution = copy.deepcopy(admitted_execution)
        current_authority = copy.deepcopy(admitted_authority)
        current_execution["draft"]["routing_policy_ref"] = "routing-policy:current:2"
        current_authority["draft"]["routing_policy_ref"] = "routing-policy:current:2"
        current_authority["draft"]["routing_policy_hash"] = "9" * 64
        authority.model_execution_resolver = lambda: (
            current_execution, current_authority
        )

        resolved = authority.resolve_for_execution(admission["id"])

        self.assertEqual(authority.admission(admission["id"])["model_execution"],
                         admitted_execution)
        self.assertEqual(resolved["admitted_model_execution"], admitted_execution)
        self.assertEqual(resolved["model_execution"], current_execution)
        self.assertNotEqual(
            content_hash({key: value for key, value in resolved.items()
                          if key != "content_hash"}),
            admission["content_hash"],
        )
        refresh = resolved["model_authority_refresh"]
        self.assertEqual(refresh["admission_hash"], admission["content_hash"])
        self.assertEqual(refresh["content_hash"], content_hash({
            key: value for key, value in refresh.items() if key != "content_hash"
        }))
        new_works = executor_module._blueprints(resolved)
        self.assertEqual(new_works[0]["id"], old_works[0]["id"])
        self.assertNotEqual(new_works[1]["id"], old_works[1]["id"])
        self.assertNotEqual(new_works[1]["idempotency_key"],
                            old_works[1]["idempotency_key"])
        self.assertEqual(new_works[1]["metadata"]["routing_policy_ref"],
                         "routing-policy:current:2")
        self.assertEqual(new_works[1]["metadata"]["model_authority_refresh"],
                         refresh["stages"]["draft"])

    def test_refreshed_model_authority_is_audited_by_completed_model_work(self):
        fixture, authority, args, _registration, _launcher = self._fixture()
        admission = authority.admit_from_plan(**args)
        executor, _draft, _verifier = self._executor(fixture, authority)
        before_roll = [executor.run_once(admission["id"]) for _ in range(2)]
        self.assertEqual([item["status"] for item in before_roll],
                         ["admitted", "succeeded"])
        old_draft_ref = executor_module._blueprints(admission)[1]["id"]
        current_execution = copy.deepcopy(admission["model_execution"])
        current_authority = copy.deepcopy(admission["model_authority"])
        current_authority["draft"]["routing_policy_hash"] = "8" * 64
        authority.model_execution_resolver = lambda: (
            current_execution, current_authority
        )

        outcomes = [executor.run_once(admission["id"]) for _ in range(8)]

        self.assertEqual(outcomes[-1]["status"], "complete")
        works = effective_mission_document_work_orders(
            authority, executor.scheduler, admission["id"],
            draft_worker=executor.draft_worker,
            verifier_worker=executor.verifier_worker,
        )
        refresh = works[1]["metadata"]["model_authority_refresh"]
        self.assertNotEqual(works[1]["id"], old_draft_ref)
        self.assertNotEqual(works[1]["idempotency_key"],
                            executor_module._blueprints(admission)[1]["idempotency_key"])
        self.assertEqual(refresh["admission_hash"], admission["content_hash"])
        formal = executor.scheduler.formal_result(works[1]["id"])
        audit = exact_mission_document_model_execution_authority(
            works[1], formal, executor.draft_worker
        )
        self.assertEqual(audit["model_result"]["work_order_ref"], works[1]["id"])
        self.assertEqual(
            audit["execution_proof"]["route_decision_ref"],
            audit["model_result"]["route_decision_ref"],
        )

    def test_refreshed_model_idempotency_does_not_alias_enqueued_old_epoch(self):
        fixture, authority, args, _registration, _launcher = self._fixture()
        admission = authority.admit_from_plan(**args)
        executor, _draft, _verifier = self._executor(fixture, authority)
        old = executor_module._blueprints(admission)[1]
        self.assertEqual(executor.scheduler.enqueue(old)["status"], "fresh")
        current_execution = copy.deepcopy(admission["model_execution"])
        current_authority = copy.deepcopy(admission["model_authority"])
        current_authority["draft"]["routing_policy_hash"] = "4" * 64
        authority.model_execution_resolver = lambda: (
            current_execution, current_authority
        )
        refreshed = executor_module._blueprints(
            authority.resolve_for_execution(admission["id"])
        )[1]

        self.assertNotEqual(refreshed["id"], old["id"])
        self.assertNotEqual(refreshed["idempotency_key"], old["idempotency_key"])
        self.assertEqual(executor.scheduler.enqueue(refreshed)["status"], "fresh")

    def test_model_roll_reuses_already_succeeded_paid_stage(self):
        fixture, authority, args, _registration, _launcher = self._fixture()
        admission = authority.admit_from_plan(**args)
        executor, draft, _verifier = self._executor(fixture, authority)
        before_roll = [executor.run_once(admission["id"]) for _ in range(4)]
        self.assertEqual([item["status"] for item in before_roll],
                         ["admitted", "succeeded", "admitted", "succeeded"])
        old_draft_ref = before_roll[-1]["work_order_ref"]
        self.assertEqual(draft.calls, 1)
        current_execution = copy.deepcopy(admission["model_execution"])
        current_authority = copy.deepcopy(admission["model_authority"])
        current_authority["draft"]["routing_policy_hash"] = "5" * 64
        authority.model_execution_resolver = lambda: (
            current_execution, current_authority
        )

        completed = self._run_until(
            executor, admission,
            lambda item: item.get("research_status") == "candidate_staged",
        )

        self.assertEqual(completed["research_status"], "candidate_staged")
        self.assertEqual(draft.calls, 1)
        works = effective_mission_document_work_orders(
            authority, executor.scheduler, admission["id"],
            draft_worker=executor.draft_worker,
            verifier_worker=executor.verifier_worker,
        )
        self.assertEqual(works[1]["id"], old_draft_ref)

    def test_model_roll_after_staging_reuses_the_finished_staging_stage(self):
        """Live 2026-09-25: ``staging completion did not converge`` on re-entry.

        All four stages had succeeded before a model-policy roll.  The roll's
        refresh hash renamed the staging stage, so the next run enqueued a
        second staging Work whose completion collided with the first one's
        admission-scoped completion key (legacy 16a7a137, 28632c70; ws-7d
        d02eaa60, 42459ee5).
        """

        fixture, authority, args, _registration, _launcher = self._fixture()
        admission = authority.admit_from_plan(**args)
        executor, draft, verifier = self._executor(fixture, authority)
        finished = self._run_until(
            executor, admission,
            lambda item: item.get("research_status") == "candidate_staged")
        staged_before = effective_mission_document_work_orders(
            authority, executor.scheduler, admission["id"],
            draft_worker=executor.draft_worker, verifier_worker=executor.verifier_worker)
        current_execution = copy.deepcopy(admission["model_execution"])
        current_authority = copy.deepcopy(admission["model_authority"])
        current_authority["draft"]["routing_policy_hash"] = "5" * 64
        current_authority["verifier"]["routing_policy_hash"] = "6" * 64
        authority.model_execution_resolver = lambda: (current_execution, current_authority)
        refreshed = authority.resolve_for_execution(admission["id"])
        self.assertIsInstance(refreshed.get("model_authority_refresh"), dict)

        again = executor.run_once(admission["id"])

        self.assertEqual(again["status"], "complete")
        self.assertEqual(again["research_status"], finished["research_status"])
        self.assertEqual((draft.calls, verifier.calls), (1, 1))
        works = effective_mission_document_work_orders(
            authority, executor.scheduler, admission["id"],
            draft_worker=executor.draft_worker, verifier_worker=executor.verifier_worker)
        self.assertEqual([work["id"] for work in works],
                         [work["id"] for work in staged_before])
        self.assertEqual(fixture.store.connection.execute(
            "SELECT count(*) FROM scheduler_work_orders WHERE json_extract("
            "work_order_json,'$.metadata.stage')='qualitative_candidate_staging'"
        ).fetchone()[0], 1)

    def test_model_roll_reuses_exact_pre_numeric_prompt_draft(self):
        fixture, authority, args, _registration, _launcher = self._fixture()
        admission = authority.admit_from_plan(**args)
        executor, draft, _verifier = self._executor(fixture, authority)
        derive = executor_module._derive

        def legacy_derive(admission_wire, scheduler, registry, blueprints, index,
                          **kwargs):
            if index == 1:
                kwargs["draft_prompt_builder"] = (
                    executor_module._pre_numeric_normalization_draft_prompt)
            return derive(
                admission_wire, scheduler, registry, blueprints, index, **kwargs)

        with patch.object(executor_module, "_derive", side_effect=legacy_derive):
            before_roll = [executor.run_once(admission["id"]) for _ in range(4)]
        self.assertEqual([item["status"] for item in before_roll],
                         ["admitted", "succeeded", "admitted", "succeeded"])
        legacy_ref = before_roll[-1]["work_order_ref"]
        legacy = executor.scheduler.work_order_authority(legacy_ref)["work_order"]
        current = derive(
            admission, executor.scheduler, authority.registry,
            executor_module._blueprints(admission), 1,
        )
        self.assertEqual(current["id"], legacy["id"])
        self.assertNotEqual(content_hash(current), content_hash(legacy))
        self.assertEqual(
            json.loads(legacy["question"])["task"],
            executor_module._PRE_NUMERIC_NORMALIZATION_DRAFT_TASK,
        )
        current_execution = copy.deepcopy(admission["model_execution"])
        current_authority = copy.deepcopy(admission["model_authority"])
        current_authority["draft"]["routing_policy_hash"] = "c" * 64
        authority.model_execution_resolver = lambda: (
            current_execution, current_authority
        )
        calls = draft.calls

        result = executor.run_once(admission["id"])

        self.assertEqual(result["status"], "admitted")
        self.assertEqual(draft.calls, calls)
        effective = effective_mission_document_work_orders(
            authority, executor.scheduler, admission["id"],
            draft_worker=executor.draft_worker,
            verifier_worker=executor.verifier_worker,
        )
        self.assertEqual(effective[1]["id"], legacy_ref)
        self.assertEqual(content_hash(effective[1]), content_hash(legacy))

    def test_model_roll_rejects_unknown_historical_prompt_drift(self):
        fixture, authority, args, _registration, _launcher = self._fixture()
        admission = authority.admit_from_plan(**args)
        executor, draft, _verifier = self._executor(fixture, authority)
        executor.run_once(admission["id"])
        executor.run_once(admission["id"])
        legacy = executor_module._derive(
            admission, executor.scheduler, authority.registry,
            executor_module._blueprints(admission), 1,
            draft_prompt_builder=(
                executor_module._pre_numeric_normalization_draft_prompt),
        )
        unknown = copy.deepcopy(legacy)
        unknown["metadata"]["prompt_hash"] = "1" * 64
        self.assertEqual(executor.scheduler.enqueue(unknown)["status"], "fresh")
        current_execution = copy.deepcopy(admission["model_execution"])
        current_authority = copy.deepcopy(admission["model_authority"])
        current_authority["draft"]["routing_policy_hash"] = "9" * 64
        authority.model_execution_resolver = lambda: (
            current_execution, current_authority
        )

        with self.assertRaisesRegex(
            MissionDocumentResearchExecutorError, "unresolved historical stage",
        ):
            executor.run_once(admission["id"])

        self.assertEqual(draft.calls, 0)

    def test_second_model_roll_reuses_succeeded_intermediate_epoch(self):
        fixture, authority, args, _registration, _launcher = self._fixture()
        admission = authority.admit_from_plan(**args)
        executor, draft, verifier = self._executor(fixture, authority)
        v2_execution = copy.deepcopy(admission["model_execution"])
        v2_authority = copy.deepcopy(admission["model_authority"])
        v2_authority["draft"]["routing_policy_hash"] = "3" * 64
        authority.model_execution_resolver = lambda: (
            v2_execution, v2_authority
        )
        self._run_until(
            executor, admission,
            lambda item: item.get("research_status") == "candidate_staged",
        )
        v2_works = effective_mission_document_work_orders(
            authority, executor.scheduler, admission["id"],
            draft_worker=executor.draft_worker,
            verifier_worker=executor.verifier_worker,
        )
        calls = (draft.calls, verifier.calls)
        v3_execution = copy.deepcopy(admission["model_execution"])
        v3_authority = copy.deepcopy(admission["model_authority"])
        v3_authority["draft"]["routing_policy_hash"] = "4" * 64
        authority.model_execution_resolver = lambda: (
            v3_execution, v3_authority
        )

        v3_works = effective_mission_document_work_orders(
            authority, executor.scheduler, admission["id"],
            draft_worker=executor.draft_worker,
            verifier_worker=executor.verifier_worker,
        )

        self.assertEqual(v3_works[1]["id"], v2_works[1]["id"])
        self.assertEqual(v3_works[2]["id"], v2_works[2]["id"])
        self.assertEqual((draft.calls, verifier.calls), calls)

    def test_model_policy_roll_cannot_expand_admitted_call_budget(self):
        fixture, authority, args, _registration, _launcher = self._fixture()
        admission = authority.admit_from_plan(**args)
        current_execution = copy.deepcopy(admission["model_execution"])
        current_execution["draft"]["max_cost_usd"] += 0.01
        authority.model_execution_resolver = lambda: (
            current_execution, copy.deepcopy(admission["model_authority"])
        )
        with self.assertRaisesRegex(
            MissionDocumentResearchError, "exceeds admitted budget"
        ):
            authority.resolve_for_execution(admission["id"])

    def test_verifier_only_roll_keeps_draft_work_byte_identical(self):
        fixture, authority, args, _registration, _launcher = self._fixture()
        admission = authority.admit_from_plan(**args)
        before = executor_module._blueprints(admission)
        current_execution = copy.deepcopy(admission["model_execution"])
        current_authority = copy.deepcopy(admission["model_authority"])
        current_authority["verifier"]["routing_policy_hash"] = "6" * 64
        authority.model_execution_resolver = lambda: (
            current_execution, current_authority
        )

        after = executor_module._blueprints(
            authority.resolve_for_execution(admission["id"])
        )

        self.assertEqual(after[1], before[1])
        self.assertNotEqual(after[2]["id"], before[2]["id"])
        self.assertNotEqual(after[2]["idempotency_key"],
                            before[2]["idempotency_key"])

    def test_equal_retrieval_proofs_get_distinct_completion_ids(self):
        fixture, authority, args, _registration, _launcher = self._fixture()
        admission = authority.admit_from_plan(**args)
        executor, _draft, _verifier = self._executor(fixture, authority)
        first = executor_module._blueprints(admission)[0]
        second = copy.deepcopy(first)
        second["id"] = "work:mission-document-research-distinct-retrieval"
        second["idempotency_key"] = "mission-document-research-work:distinct:1"
        executor.scheduler.enqueue(first)
        executor.scheduler.enqueue(second)

        one = executor._complete_retrieval(admission, first)
        two = executor._complete_retrieval(
            {**admission, "id": "mission-document-research-admission:distinct"},
            second,
        )

        self.assertEqual((one["status"], two["status"]),
                         ("succeeded", "succeeded"))
        rows = fixture.store.connection.execute(
            "SELECT result_envelope_id,work_order_id FROM scheduler_result_envelopes "
            "WHERE work_order_id IN (?,?) ORDER BY work_order_id",
            (first["id"], second["id"]),
        ).fetchall()
        self.assertEqual(len(rows), 2)
        self.assertNotEqual(rows[0]["result_envelope_id"],
                            rows[1]["result_envelope_id"])

    def test_retrieval_reentry_after_expiry_gets_attempt_scoped_result_id(self):
        fixture, authority, args, _registration, _launcher = self._fixture()
        admission = authority.admit_from_plan(**args)
        executor, _draft, _verifier = self._executor(fixture, authority)
        work = executor_module._blueprints(admission)[0]
        executor.scheduler.enqueue(work)
        first = executor.scheduler.claim(
            executor.actor_ref, work_order_id=work["id"], lease_seconds=1
        )
        fixture.harness.clock.value += timedelta(seconds=2)

        completed = executor._complete_retrieval(admission, work)

        self.assertEqual(completed["status"], "succeeded")
        formal = executor.scheduler.formal_result(work["id"])
        self.assertEqual(formal["attempt_number"], 2)
        proof_hash = formal["result_envelope"]["outputs"]["content_hash"]
        first_id = executor_module._ref(
            "result-envelope:mission-document-search", {
                "work_order_ref": work["id"], "attempt_number": 1,
                "proof_hash": proof_hash,
            })
        self.assertNotEqual(formal["result_envelope_id"], first_id)
        self.assertEqual(first["attempt"]["attempt_number"], 1)

    def test_typed_target_is_replayed_and_never_retags_legacy_admission(self):
        fixture, authority, args, _registration, _launcher = self._fixture()
        legacy = authority.admit_from_plan(**args)
        old_plan = json.loads(fixture.store.connection.execute(
            "SELECT plan_json FROM coverage_mission_research_plans WHERE plan_id=?",
            (args["plan_ref"],),
        ).fetchone()[0])
        plan = {key: value for key, value in old_plan.items() if key != "content_hash"}
        plan["state_hash"] = "7" * 64
        target = self._financial_note_target()
        plan["inquiries"][0]["directed_document"]["evidence_target"] = target
        plan["content_hash"] = content_hash(plan)
        stored = self._record_plan(fixture, plan)
        inquiry = plan["inquiries"][0]
        target_args = {
            **args, "plan_ref": stored["plan_id"],
            "inquiry_ref": inquiry_ref_for(inquiry_content_hash(inquiry)),
        }
        with patch(
            "dalton_core.mission_document_research."
            "financial_note_targets_for_registration",
            return_value=[target],
        ):
            admitted = authority.admit_from_plan(**target_args)
            self.assertEqual(admitted["status_marker"], "fresh")
            self.assertNotEqual(admitted["id"], legacy["id"])
            self.assertEqual(
                admitted["planner_inquiry"]["directed_document"]["evidence_target"],
                target,
            )
            self.assertEqual(
                authority.admit_from_plan(**target_args)["status_marker"], "duplicate")

        with patch(
            "dalton_core.mission_document_research."
            "financial_note_targets_for_registration",
            return_value=[],
        ):
            with self.assertRaisesRegex(
                MissionDocumentResearchError, "differs from current filing authority"
            ):
                authority.resolve_for_execution(admitted["id"])
            # A target cannot retroactively alter or invalidate the old generic
            # qualitative admission, whose exact identity omitted it.
            self.assertEqual(authority.resolve_for_execution(legacy["id"])["id"], legacy["id"])

    def test_projected_cli_prompt_records_full_state_plan_that_generic_admission_accepts(self):
        fixture, authority, args, registration, _launcher = self._fixture()
        existing_plan = json.loads(fixture.store.connection.execute(
            "SELECT plan_json FROM coverage_mission_research_plans WHERE plan_id=?",
            (args["plan_ref"],),
        ).fetchone()[0])
        inquiry = existing_plan["inquiries"][0]
        selected_document = {
            "company_ref": COMPANY,
            "document_ref": registration["document_ref"],
            "document_version_hash": registration["content_hash"],
            "authority_ref": registration["id"],
            "authority_hash": registration["content_hash"],
            "source_ref": registration["source_ref"],
            "source_content_hash": registration["normalized_text"]["text_sha256"],
            "title": "Managed services accounting policy",
            "readable": True, "completeness": "complete",
            "operations": ["search_registered_document", "read_registered_document"],
        }
        documents = []
        for index in range(12):
            if index == 0:
                document = dict(selected_document)
            else:
                digest = f"{index:064x}"
                document = {
                    **selected_document,
                    "document_ref": f"wiki-document:other:{index}",
                    "document_version_hash": digest,
                    "authority_ref": f"registered-document:sha256:{digest}",
                    "authority_hash": digest,
                    "source_content_hash": digest,
                }
            document.update({
                "original_preview": (f"verified original {index} " * 260),
                "preview_proof_ref": f"document-read-proof:{index:032x}",
                "preview_proof_hash": f"{index + 20:064x}",
            })
            documents.append(document)
        full_state = {
            "schema_version": "0.1",
            "goal": {"mission_version_ref": fixture.mission["id"]},
            "companies": [{
                "company_ref": COMPANY, "ticker": "TEST", "stage": "initial_screen",
                "gaps": [], "figures": {"total": 0}, "items": [],
                "readable_documents": documents,
            }],
            "document_research_policy": {
                "max_query_terms": 16, "max_query_term_chars": 240,
            },
            "as_of": NOW.isoformat(timespec="microseconds"),
        }
        full_state["content_hash"] = content_hash(full_state)
        full_prompt_bytes = len(build_prompt(full_state).encode("utf-8"))
        input_bound = full_prompt_bytes - 12_000
        projected = project_state_for_prompt(
            full_state, max_input_bytes=input_bound)
        self.assertGreater(
            projected["prompt_projection"]["preview_triplets_omitted"], 0)

        raw_response = {
            "schema_version": "0.1",
            "assessment": "Read the selected original document.",
            "directives": [],
            "inquiries": [{
                key: value for key, value in inquiry.items()
                if key not in {"rank", "repair_target_hash"}
            }],
            "sufficiency": [],
        }
        adapter = RouteBoundCountingFakeAdapter(raw_response)
        model_config_path = fixture.state / "projected-planner-model-config.json"
        model_config = json.loads(
            (fixture.state / DRAFT_MODEL_CONFIG_NAME).read_text(encoding="utf-8"))
        model_config["purpose_call_budgets"] = {"plan": {
            "max_input_tokens": input_bound,
            "max_output_tokens": 4_000,
            "max_cost_usd": 1.0,
            "timeout_seconds": 120,
        }}
        model_config_path.write_text(
            json.dumps(model_config, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        fixture.store.connection.commit()

        def cockpit(config, **kwargs):
            return CockpitModel(
                config, adapter_factory=lambda _router: adapter,
                clock=fixture.harness.clock, **kwargs,
            )

        with (
            patch("dalton_core.research_planner_cli.build_state", return_value=full_state),
            patch("dalton_core.research_planner_cli.CockpitModel", side_effect=cockpit),
        ):
            summary = run_planner(
                state_dir=fixture.state,
                model_config_path=model_config_path,
                summary_dir=fixture.state / "projected-planner-summary",
                scheduler_db=Path(fixture.store.path),
                plans_dir=fixture.state / "discovery-plans",
                dry_run=False,
            )

        self.assertEqual(summary["plan_status"], "fresh")
        self.assertGreater(summary["prompt_input"]["projection"][
            "preview_triplets_omitted"], 0)
        stored = fixture.store.connection.execute(
            "SELECT plan_json,work_order_ref FROM coverage_mission_research_plans "
            "WHERE plan_id=?", (summary["plan_ref"],),
        ).fetchone()
        recorded_plan = json.loads(stored["plan_json"])
        self.assertEqual(recorded_plan["state_hash"], full_state["content_hash"])
        archived_work = fixture.harness.scheduler().work_order_authority(
            stored["work_order_ref"])
        self.assertIn('"prompt_projection":', archived_work["work_order"]["question"])

        admitted = authority.admit_from_plan(
            **{**args, "plan_ref": summary["plan_ref"]})
        self.assertEqual(admitted["status_marker"], "fresh")
        self.assertEqual(admitted["plan_hash"], recorded_plan["content_hash"])
        self.assertEqual(admitted["document_authority_ref"], registration["id"])
        self.assertEqual(adapter.calls, 1)

    def test_execution_resolution_preserves_caller_owned_ledger_transaction(self):
        fixture, authority, args, _registration, _launcher = self._fixture()
        admitted = authority.admit_from_plan(**args)
        connection = fixture.store.connection
        connection.execute(
            "CREATE TABLE mission_document_resolution_sentinel(value TEXT NOT NULL)"
        )
        connection.commit()

        connection.execute("BEGIN IMMEDIATE")
        try:
            connection.execute(
                "INSERT INTO mission_document_resolution_sentinel VALUES(?)",
                ("must-rollback",),
            )
            resolved = authority.resolve_for_execution(admitted["id"])
            active = authority.active_budget_mission(admitted["id"])
            self.assertEqual(resolved["content_hash"], admitted["content_hash"])
            self.assertEqual(active["id"], admitted["mission_version_ref"])
            self.assertTrue(connection.in_transaction)
        finally:
            connection.rollback()

        self.assertEqual(
            connection.execute(
                "SELECT COUNT(*) FROM mission_document_resolution_sentinel"
            ).fetchone()[0],
            0,
        )

    def test_foreign_question_or_registration_and_stale_source_refused(self):
        fixture, authority, args, registration, launcher = self._fixture()
        with self.assertRaisesRegex(MissionDocumentResearchError, "ResearchQuestionVersion"):
            authority.admit_from_plan(**{**args, "question_version_ref": "missing"})
        wrong = {**registration, "document_ref": "wiki-document:other"}
        wrong_body = {key: value for key, value in wrong.items() if key != "content_hash"}
        wrong["content_hash"] = content_hash(wrong_body)
        original = authority.registration_resolver
        authority.registration_resolver = lambda _ref: wrong
        with self.assertRaisesRegex(MissionDocumentResearchError, "registration"):
            authority.admit_from_plan(**args)
        authority.registration_resolver = original
        launcher.manifest = {**launcher.manifest, "text_sha256": "f" * 64}
        with self.assertRaises(MissionDocumentResearchError):
            authority.admit_from_plan(**args)
        self.assertEqual(fixture.store.connection.execute(
            "SELECT count(*) FROM mission_document_research_admissions"
        ).fetchone()[0], 0)

    def test_plan_inquiry_and_mission_source_are_not_caller_fields(self):
        fixture, authority, args, _registration, _launcher = self._fixture()
        for changed in (
            {**args, "inquiry_ref": "research-task-inquiry:" + "f" * 32},
            {**args, "document_authority_ref": "registered-document:sha256:" + "f" * 64},
        ):
            with self.subTest(changed=changed), self.assertRaises(MissionDocumentResearchError):
                authority.admit_from_plan(**changed)
        self.assertEqual(fixture.store.connection.execute(
            "SELECT count(*) FROM mission_document_research_admissions"
        ).fetchone()[0], 0)

    def test_rationale_only_new_plan_reuses_paid_execution_identity(self):
        fixture, authority, args, _registration, _launcher = self._fixture()
        first = authority.admit_from_plan(**args)
        original = json.loads(fixture.store.connection.execute(
            "SELECT plan_json FROM coverage_mission_research_plans WHERE plan_id=?",
            (args["plan_ref"],),
        ).fetchone()[0])
        second_plan = {key: value for key, value in original.items() if key != "content_hash"}
        second_plan["state_hash"] = "3" * 64
        second_plan["inquiries"][0]["directed_document"]["query_rationale"] = (
            "Equivalent prose explaining the same exact query."
        )
        second_plan["content_hash"] = content_hash(second_plan)
        stored = self._record_plan(fixture, second_plan)
        duplicate = authority.admit_from_plan(**{**args, "plan_ref": stored["plan_id"]})
        self.assertEqual(duplicate["id"], first["id"])
        self.assertEqual(duplicate["status_marker"], "duplicate")
        self.assertEqual(fixture.store.connection.execute(
            "SELECT count(*) FROM mission_document_research_admissions"
        ).fetchone()[0], 1)

    def test_manifest_only_registration_cannot_claim_company_scope(self):
        fixture, authority, args, _registration, _launcher = self._fixture()
        direct = authority.registry.register(
            source_ref=COMPANY_WIKI_SOURCE_REF,
            document_ref=authority.registration_resolver(args["document_authority_ref"])["document_ref"],
            purpose=PURPOSE,
            acquisition_ticket_ref="feed-run:mission-document:wiki",
        )
        original = json.loads(fixture.store.connection.execute(
            "SELECT plan_json FROM coverage_mission_research_plans WHERE plan_id=?",
            (args["plan_ref"],),
        ).fetchone()[0])
        plan = {key: value for key, value in original.items() if key != "content_hash"}
        plan["state_hash"] = "4" * 64
        plan["inquiries"][0]["directed_document"]["document_version_hash"] = direct["content_hash"]
        plan["content_hash"] = content_hash(plan)
        stored = self._record_plan(fixture, plan)
        inquiry = plan["inquiries"][0]
        authority.registration_resolver = lambda _ref: direct
        with self.assertRaisesRegex(
            MissionDocumentResearchError, "another mission or company"
        ):
            authority.admit_from_plan(
                plan_ref=stored["plan_id"],
                inquiry_ref=inquiry_ref_for(inquiry_content_hash(inquiry)),
                question_version_ref=args["question_version_ref"],
                document_authority_ref=direct["id"],
            )

    def test_self_consistent_plan_without_planner_formal_cannot_authorize_paid_work(self):
        fixture, authority, args, _registration, _launcher = self._fixture()
        original = json.loads(fixture.store.connection.execute(
            "SELECT plan_json FROM coverage_mission_research_plans WHERE plan_id=?",
            (args["plan_ref"],),
        ).fetchone()[0])
        for marker, decider in (("5", "automation:foreign"), ("6", "automation:test")):
            plan = {key: value for key, value in original.items() if key != "content_hash"}
            plan["state_hash"] = marker * 64
            plan["content_hash"] = content_hash(plan)
            stored = CoverageMissionAuthority(fixture.store).record_research_plan(
                plan, decided_by=decider,
            )
            with self.subTest(decider=decider), self.assertRaisesRegex(
                MissionDocumentResearchError, "planner plan lacks automation execution authority"
            ):
                authority.admit_from_plan(**{**args, "plan_ref": stored["plan_id"]})

    def test_source_neutral_executor_searches_models_verifies_and_stages_once(self):
        fixture, authority, args, _registration, _launcher = self._fixture()
        admission = authority.admit_from_plan(**args)
        executor, draft, verifier = self._executor(fixture, authority)
        outcomes = [executor.run_once(admission["id"]) for _ in range(9)]
        self.assertEqual([item["status"] for item in outcomes], [
            "admitted", "succeeded", "admitted", "succeeded", "admitted",
            "succeeded", "admitted", "complete", "complete",
        ])
        self.assertEqual((draft.calls, verifier.calls), (1, 1))
        self.assertEqual(outcomes[-1]["research_status"], "candidate_staged")
        material = json.loads(fixture.harness.staging.connection.execute(
            "SELECT record_json FROM candidate_source_materials"
        ).fetchone()[0])
        self.assertEqual(material["source_ref"], COMPANY_WIKI_SOURCE_REF)
        self.assertEqual(
            material["normalized_payload"]["search_proof"]["request"]["registration"],
            admission["request"]["registration"],
        )
        self.assertEqual(fixture.store.connection.execute(
            "SELECT count(*) FROM mission_document_research_outcomes"
        ).fetchone()[0], 1)
        observations = read_mission_document_research_observations(
            fixture.store.connection, mission_version_ref=fixture.mission["id"])
        self.assertEqual(len(observations), 1)
        self.assertEqual(observations[0]["outcome"], "candidate_staged")
        self.assertEqual(observations[0]["result_envelope_ref"],
                         fixture.harness.scheduler().formal_result(
                             observations[0]["work_order_ref"])["result_envelope_id"])
        self.assertEqual(observations[0]["question"], admission["planner_inquiry"]["question"])
        self.assertEqual(fixture.budget.connection.execute(
            "SELECT count(*) FROM thesis_impact_day_admissions WHERE "
            "work_order_ref LIKE 'work:mission-document-research-%'"
        ).fetchone()[0], 2)
        works = effective_mission_document_work_orders(
            authority, executor.scheduler, admission["id"],
            draft_worker=executor.draft_worker, verifier_worker=executor.verifier_worker)
        model_authority = exact_mission_document_model_execution_authority(
            works[1], executor.scheduler.formal_result(works[1]["id"]),
            executor.draft_worker,
        )
        self.assertEqual(model_authority["model_result"]["output"]["status"], "answered")
        self.assertEqual(model_authority["execution_proof"]["cost_status"], "actual")
        self.assertIsNotNone(
            model_authority["execution_proof"]["budget_settlement_ref"])

    def test_candidate_material_identity_scopes_shared_search_proof_and_replays_legacy(self):
        fixture, authority, args, _registration, _launcher = self._fixture()
        admission = authority.admit_from_plan(**args)
        executor, _draft, _verifier = self._executor(fixture, authority)
        outcomes = [executor.run_once(admission["id"]) for _ in range(7)]
        self.assertEqual([item["status"] for item in outcomes], [
            "admitted", "succeeded", "admitted", "succeeded", "admitted",
            "succeeded", "admitted",
        ])
        works = effective_mission_document_work_orders(
            authority, executor.scheduler, admission["id"],
            draft_worker=executor.draft_worker,
            verifier_worker=executor.verifier_worker,
        )
        verifier_proof = executor.scheduler.formal_result(
            works[2]["id"])["result_envelope"]["outputs"]
        common = {
            "proof": works[3]["metadata"]["retrieval_proof"],
            "draft_proof": works[3]["metadata"]["draft_proof"],
            "verifier_proof": verifier_proof,
            "draft_work": works[1], "verifier_work": works[2],
            "created_at": works[3]["created_at"],
        }

        # An already staged pre-versioning bundle remains an exact, closed
        # replay.  The fallback verifies every record; it does not bless an
        # arbitrary row merely because its material id has the legacy shape.
        legacy = _build_candidate_bundle(
            admission=admission, material_identity_version="0.1-legacy", **common)
        legacy_resolver = _MissionDocumentCandidateAuthority(
            question=admission["planner_inquiry"]["question"],
            proof=common["proof"], draft_proof=common["draft_proof"],
            verifier_proof=common["verifier_proof"], admission=admission,
            material_identity_version="0.1-legacy",
        )
        legacy_key = f"mission-document-research-candidate:{admission['id']}"
        legacy_result = fixture.harness.staging.stage(
            material=legacy["material"],
            source_verification=legacy["source_verification"],
            evidence=legacy["evidence"], claim=legacy["claim"],
            idempotency_key=legacy_key,
            verification_mode=MISSION_DOCUMENT_AUTHORITY_MODE,
            authority_resolver=legacy_resolver,
        )
        legacy_records = {
            "authority_ref": admission["id"],
            "question_version_ref": admission["question_version_ref"],
            "question_version_hash": admission["question_version_hash"],
            "research_status": "candidate_staged",
            "candidate_evidence_ref": legacy_result["candidate_evidence_ref"],
            "candidate_evidence_hash": legacy_result["candidate_evidence_hash"],
            "candidate_claim_ref": legacy_result["candidate_claim_ref"],
            "candidate_claim_hash": legacy_result["candidate_claim_hash"],
        }
        self.assertEqual(
            executor._validate_records(admission, works, legacy_records),
            legacy_records,
        )
        resumed = executor.run_once(admission["id"])
        self.assertEqual(resumed["status"], "complete")
        self.assertEqual(executor.scheduler.formal_result(
            works[3]["id"])["terminal_state"], "succeeded")
        self.assertEqual(fixture.harness.staging.connection.execute(
            "SELECT count(*) FROM candidate_stage_requests WHERE idempotency_key=?",
            (legacy_key,),
        ).fetchone()[0], 1)

        second = copy.deepcopy(admission)
        second["id"] = "mission-document-research-admission:shared-proof-second"
        second["question_version_ref"] = "research-question-version:shared-proof-second"
        second["question_version_hash"] = "8" * 64
        second["content_hash"] = content_hash({
            "prior_admission_hash": admission["content_hash"],
            "admission_ref": second["id"],
            "question_version_ref": second["question_version_ref"],
        })
        versioned = _build_candidate_bundle(admission=second, **common)
        self.assertNotEqual(versioned["material"]["id"], legacy["material"]["id"])
        second_resolver = _MissionDocumentCandidateAuthority(
            question=second["planner_inquiry"]["question"],
            proof=common["proof"], draft_proof=common["draft_proof"],
            verifier_proof=common["verifier_proof"], admission=second,
        )
        second_key = f"mission-document-research-candidate:{second['id']}"
        first = fixture.harness.staging.stage(
            material=versioned["material"],
            source_verification=versioned["source_verification"],
            evidence=versioned["evidence"], claim=versioned["claim"],
            idempotency_key=second_key,
            verification_mode=MISSION_DOCUMENT_AUTHORITY_MODE,
            authority_resolver=second_resolver,
        )
        replay = fixture.harness.staging.stage(
            material=versioned["material"],
            source_verification=versioned["source_verification"],
            evidence=versioned["evidence"], claim=versioned["claim"],
            idempotency_key=second_key,
            verification_mode=MISSION_DOCUMENT_AUTHORITY_MODE,
            authority_resolver=second_resolver,
        )
        self.assertEqual((first["write_status"], replay["write_status"]),
                         ("fresh", "duplicate"))
        self.assertEqual(fixture.harness.staging.connection.execute(
            "SELECT count(*) FROM candidate_source_materials"
        ).fetchone()[0], 2)

    def test_model_worker_rejects_substituted_question_before_budget_or_adapter(self):
        fixture, authority, args, _registration, _launcher = self._fixture()
        admission = authority.admit_from_plan(**args)
        executor, draft, _verifier = self._executor(fixture, authority)
        for _ in range(3):
            executor.run_once(admission["id"])
        work = executor._derive_work(admission, executor._blueprints(admission), 1)
        work["question"] = "Use some other document and mission"
        work["metadata"]["prompt_hash"] = content_hash(work["question"])
        work["metadata"]["model_request_binding_hash"] = "0" * 64
        with self.assertRaisesRegex(AnnualReportQualitativeError, "authority is invalid"):
            executor.draft_worker._work(work)
        self.assertEqual(draft.calls, 0)
        self.assertEqual(fixture.budget.connection.execute(
            "SELECT count(*) FROM thesis_impact_day_admissions"
        ).fetchone()[0], 0)

    def test_budget_refusal_is_terminal_with_no_model_call(self):
        fixture, authority, args, _registration, _launcher = self._fixture()
        admission = authority.admit_from_plan(**args)
        executor, draft, verifier = self._executor(fixture, authority)
        fixture.budget.admit(
            policy_version_id="budget-policy:mission-annual:1",
            day=NOW.date().isoformat(), work_order_ref="work:other-budget-consumer",
            attempt_number=1, phase="assessment", route_decision_ref="route:other",
            reserved_micros=9_500_000,
        )
        for _ in range(3):
            executor.run_once(admission["id"])
        result = executor.run_once(admission["id"])
        self.assertEqual(result["status"], "failed")
        self.assertEqual((draft.calls, verifier.calls), (0, 0))

    def test_stage_claim_precedes_candidate_side_effect(self):
        fixture, authority, args, _registration, _launcher = self._fixture()
        admission = authority.admit_from_plan(**args)
        executor, draft, verifier = self._executor(fixture, authority)
        for _ in range(7):
            executor.run_once(admission["id"])
        work = executor._derive_work(admission, executor._blueprints(admission), 3)
        foreign = executor.scheduler.claim("worker:foreign", work_order_id=work["id"])
        self.assertIsNotNone(foreign)
        self.assertEqual(executor.run_once(admission["id"])["status"], "pending")
        self.assertEqual(
            fixture.harness.staging.counts()["candidate_stage_requests"], 0
        )

    def test_expired_stage_lease_is_revalidated_before_candidate_side_effect(self):
        fixture, authority, args, _registration, _launcher = self._fixture()
        admission = authority.admit_from_plan(**args)
        expired = []
        holder = {}

        def expire_at_seam(seam):
            if seam == "before_staging_lease_validation":
                expired.append(seam)
                fixture.harness.clock.value += timedelta(seconds=31)
                stage = holder["executor"]._derive_work(
                    admission, holder["executor"]._blueprints(admission), 3)
                self.assertIsNotNone(holder["executor"].scheduler.claim(
                    "worker:foreign", work_order_id=stage["id"]))

        executor, _draft, _verifier = self._executor(
            fixture, authority, fault_injector=expire_at_seam)
        holder["executor"] = executor
        for _ in range(7):
            executor.run_once(admission["id"])
        result = executor.run_once(admission["id"])
        self.assertEqual(result["status"], "pending")
        self.assertEqual(expired, ["before_staging_lease_validation"])
        self.assertEqual(
            fixture.harness.staging.counts()["candidate_stage_requests"], 0
        )
        stage = executor._derive_work(admission, executor._blueprints(admission), 3)
        self.assertEqual(executor.scheduler.status(stage["id"])["state"], "leased")

    def test_observation_reader_rejects_self_consistent_semantic_relabel(self):
        fixture, authority, args, _registration, _launcher = self._fixture()
        original = json.loads(fixture.store.connection.execute(
            "SELECT plan_json FROM coverage_mission_research_plans WHERE plan_id=?",
            (args["plan_ref"],),
        ).fetchone()[0])
        plan = {key: value for key, value in original.items() if key != "content_hash"}
        plan["state_hash"] = "8" * 64
        plan["inquiries"][0]["directed_document"]["query_terms"] = ["unobtainium"]
        plan["content_hash"] = content_hash(plan)
        stored = self._record_plan(fixture, plan)
        inquiry = plan["inquiries"][0]
        admission = authority.admit_from_plan(**{
            **args, "plan_ref": stored["plan_id"],
            "inquiry_ref": inquiry_ref_for(inquiry_content_hash(inquiry)),
        })
        executor, draft, verifier = self._executor(fixture, authority)
        for _ in range(3):
            executor.run_once(admission["id"])
        row = fixture.store.connection.execute(
            "SELECT observation_id,record_json FROM mission_document_research_observations"
        ).fetchone()
        forged = json.loads(row["record_json"])
        forged["question"] = "A different company's unrelated question"
        forged["tried_query_terms"] = ["different", "terms"]
        body = dict(forged)
        body.pop("content_hash")
        forged["content_hash"] = content_hash(body)
        fixture.store.connection.execute(
            "DROP TRIGGER mission_document_research_observations_no_update")
        fixture.store.connection.execute(
            "UPDATE mission_document_research_observations SET record_json=?,content_hash=? "
            "WHERE observation_id=?",
            (json.dumps(forged, sort_keys=True, separators=(",", ":")),
             forged["content_hash"], row["observation_id"]),
        )
        fixture.store.connection.commit()
        with self.assertRaisesRegex(
            MissionDocumentResearchExecutorError, "execution drifted"
        ):
            read_mission_document_research_observations(fixture.store.connection)

    def test_query_miss_is_typed_feedback_and_does_not_claim_no_answer(self):
        fixture, authority, args, _registration, _launcher = self._fixture()
        original = json.loads(fixture.store.connection.execute(
            "SELECT plan_json FROM coverage_mission_research_plans WHERE plan_id=?",
            (args["plan_ref"],),
        ).fetchone()[0])
        plan = {key: value for key, value in original.items() if key != "content_hash"}
        plan["state_hash"] = "7" * 64
        plan["inquiries"][0]["directed_document"]["query_terms"] = ["unobtainium"]
        plan["inquiries"][0]["directed_document"]["query_rationale"] = (
            "Try one bounded term and report a miss honestly."
        )
        plan["content_hash"] = content_hash(plan)
        stored = self._record_plan(fixture, plan)
        inquiry = plan["inquiries"][0]
        admission = authority.admit_from_plan(**{
            **args, "plan_ref": stored["plan_id"],
            "inquiry_ref": inquiry_ref_for(inquiry_content_hash(inquiry)),
        })
        executor, draft, verifier = self._executor(fixture, authority)
        results = [executor.run_once(admission["id"]) for _ in range(3)]
        self.assertEqual([item["status"] for item in results], [
            "admitted", "succeeded", "complete",
        ])
        self.assertEqual(results[-1]["research_status"], "query_miss")
        self.assertEqual((draft.calls, verifier.calls), (0, 0))
        feedback = read_mission_document_research_observations(
            fixture.store.connection, mission_version_ref=fixture.mission["id"])
        self.assertEqual(len(feedback), 1)
        self.assertIn("does not prove", feedback[0]["meaning"])
        self.assertEqual(feedback[0]["tried_query_terms"], ["unobtainium"])
        replay = executor.run_once(admission["id"])
        self.assertEqual(replay["feedback_ref"], results[-1]["feedback_ref"])

    def test_insufficient_evidence_skips_verifier_and_records_exact_missing_need(self):
        fixture, authority, args, _registration, _launcher = self._fixture()
        admission = authority.admit_from_plan(**args)
        executor, draft, verifier = self._executor(fixture, authority)
        draft.candidate_wire = {
            "schema_version": "0.1", "status": "insufficient_evidence",
            "answer": "The excerpts do not define the contract boundary.",
            "candidate": None,
            "missing": ["the contract clause that defines the service period"],
        }
        results = [executor.run_once(admission["id"]) for _ in range(5)]
        self.assertEqual(results[-1]["research_status"], "no_verified_claim")
        self.assertEqual((draft.calls, verifier.calls), (1, 0))
        observations = read_mission_document_research_observations(
            fixture.store.connection, mission_version_ref=fixture.mission["id"])
        self.assertEqual(observations[0]["outcome"], "no_verified_claim")
        self.assertEqual(observations[0]["missing_evidence"], [
            "the contract clause that defines the service period"
        ])
        self.assertIsNotNone(observations[0]["draft_proof_ref"])

    def test_zero_cost_capacity_failure_uses_fresh_work_and_completes(self):
        fixture, authority, args, _registration, _launcher = self._fixture()
        self._enable_recovery(fixture)
        admission = authority.admit_from_plan(**args)
        statement = "Managed services revenue is recognized over time."
        draft = CapacityOnceAdapter({
            "schema_version": "0.1", "status": "answered", "answer": statement,
            "candidate": {"normalized_statement": statement,
                          "metric_or_aspect": "managed services revenue recognition",
                          "period": "current policy", "basis": "reported",
                          "cited_match_indexes": [0]}, "missing": [],
        })
        executor, draft, verifier = self._executor(
            fixture, authority, draft_adapter=draft)
        results = []
        original_authority = None
        for _ in range(14):
            results.append(executor.run_once(admission["id"]))
            if len(results) == 3:
                original_authority = executor.scheduler.work_order_authority(
                    results[-1]["work_order_ref"])
            if results[-1].get("research_status") == "candidate_staged":
                break
        self.assertEqual(results[-1]["research_status"], "candidate_staged")
        self.assertEqual((draft.calls, verifier.calls), (4, 1))
        links = fixture.store.connection.execute(
            "SELECT * FROM mission_document_research_recovery_links").fetchall()
        self.assertEqual(len(links), 1)
        original = executor._blueprints(admission)[1]
        self.assertEqual(executor.scheduler.status(original["id"])["state"], "failed")
        self.assertEqual(executor.scheduler.work_order_authority(original["id"]),
                         original_authority)
        self.assertNotEqual(links[0]["recovery_work_order_ref"], original["id"])
        effective = effective_mission_document_work_orders(
            authority, executor.scheduler, admission["id"],
            draft_worker=executor.draft_worker, verifier_worker=executor.verifier_worker)
        self.assertEqual(effective[1]["id"], links[0]["recovery_work_order_ref"])
        self.assertEqual(effective[2]["metadata"]["upstream_work_order_ref"], effective[1]["id"])
        settlements = fixture.budget.connection.execute(
            "SELECT actual_micros FROM thesis_impact_day_settlements ORDER BY created_at"
        ).fetchall()
        self.assertEqual(sorted(row[0] for row in settlements), [0, 0, 0, 2000, 2000])
        observation = next(item for item in read_mission_document_research_observations(
            fixture.store.connection) if item["outcome"] == "recovery_required")
        self.assertEqual(observation["recovery"]["proof"]["classification"],
                         "proved_zero_cost_no_send")

    def test_recovery_enqueue_crash_restarts_without_second_work_or_charge(self):
        fixture, authority, args, _registration, _launcher = self._fixture()
        self._enable_recovery(fixture)
        admission = authority.admit_from_plan(**args)
        statement = "Managed services revenue is recognized over time."
        adapter = CapacityOnceAdapter({
            "schema_version": "0.1", "status": "answered", "answer": statement,
            "candidate": {"normalized_statement": statement, "metric_or_aspect": "revenue",
                          "period": "current", "basis": "reported",
                          "cited_match_indexes": [0]}, "missing": [],
        })
        fired = []
        executor, _draft, _verifier = self._executor(
            fixture, authority, draft_adapter=adapter,
            fault_injector=lambda seam: (fired.append(seam),
                                         (_ for _ in ()).throw(RuntimeError("crash")))[1])
        for _ in range(6):
            executor.run_once(admission["id"])
        with self.assertRaisesRegex(RuntimeError, "crash"):
            executor.run_once(admission["id"])
        self.assertEqual(fired, ["after_recovery_enqueue"])
        self.assertEqual(fixture.store.connection.execute(
            "SELECT count(*) FROM mission_document_research_recovery_links").fetchone()[0], 0)
        self.assertEqual(fixture.budget.connection.execute(
            "SELECT count(*) FROM thesis_impact_day_admissions").fetchone()[0], 3)
        restarted, _draft, _verifier = self._executor(
            fixture, authority, draft_adapter=adapter)
        result = restarted.run_once(admission["id"])
        self.assertEqual(result["reason"], "fresh_work_recovery")
        self.assertEqual(fixture.store.connection.execute(
            "SELECT count(*) FROM mission_document_research_recovery_links").fetchone()[0], 1)
        self.assertEqual(fixture.budget.connection.execute(
            "SELECT count(*) FROM thesis_impact_day_admissions").fetchone()[0], 3)

    def test_model_authority_roll_preserves_prior_recovery_spend(self):
        fixture, authority, args, _registration, _launcher = self._fixture()
        self._enable_recovery(fixture, maximum=1)
        admission = authority.admit_from_plan(**args)
        statement = "Managed services revenue is recognized over time."
        adapter = AlwaysCapacityAdapter({
            "schema_version": "0.1", "status": "answered", "answer": statement,
            "candidate": {"normalized_statement": statement,
                          "metric_or_aspect": "revenue", "period": "current",
                          "basis": "reported", "cited_match_indexes": [0]},
            "missing": [],
        })
        executor, _draft, _verifier = self._executor(
            fixture, authority, draft_adapter=adapter)
        recovery = self._run_until(
            executor, admission,
            lambda item: item.get("reason") == "fresh_work_recovery",
        )
        old_recovery_ref = recovery["work_order_ref"]
        self.assertEqual(executor.scheduler.status(old_recovery_ref)["state"], "ready")

    def test_model_roll_keeps_historical_failure_on_existing_recovery_door(self):
        fixture, authority, args, _registration, _launcher = self._fixture()
        self._enable_recovery(fixture, maximum=1)
        admission = authority.admit_from_plan(**args)
        adapter = AlwaysCapacityAdapter({"unused": True})
        executor, _draft, _verifier = self._executor(
            fixture, authority, draft_adapter=adapter)
        failed = self._run_until(
            executor, admission, lambda item: item.get("status") == "failed")
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(fixture.store.connection.execute(
            "SELECT count(*) FROM mission_document_research_recovery_links"
        ).fetchone()[0], 0)
        calls = adapter.calls
        current_execution = copy.deepcopy(admission["model_execution"])
        current_authority = copy.deepcopy(admission["model_authority"])
        current_authority["draft"]["routing_policy_hash"] = "2" * 64
        authority.model_execution_resolver = lambda: (
            current_execution, current_authority
        )

        inspected = executor.inspect_model_authority_epoch_recovery(admission["id"])

        draft_status = next(
            item for item in inspected["stages"] if item["stage"] == "draft")
        self.assertEqual(draft_status["status"], "historical_failure")
        self.assertEqual(draft_status["action"], "existing_bounded_recovery_policy")
        self.assertEqual(adapter.calls, calls)
        self.assertEqual(fixture.store.connection.execute(
            "SELECT count(*) FROM mission_document_research_recovery_links"
        ).fetchone()[0], 0)
        self.assertEqual(fixture.store.connection.execute(
            "SELECT count(*) FROM "
            "mission_document_research_model_authority_epoch_rebinds"
        ).fetchone()[0], 0)

        recovery = executor.run_once(admission["id"])

        self.assertEqual(recovery["reason"], "fresh_work_recovery")
        self.assertEqual(adapter.calls, calls)
        self.assertEqual(fixture.store.connection.execute(
            "SELECT count(*) FROM mission_document_research_recovery_links"
        ).fetchone()[0], 1)
        self.assertEqual(fixture.store.connection.execute(
            "SELECT count(*) FROM "
            "mission_document_research_model_authority_epoch_rebinds"
        ).fetchone()[0], 1)

    def test_intermediate_epoch_recovery_chain_survives_a_second_model_roll(self):
        """Live 6a2bcd: ``recovery link authority drifted`` after two rolls.

        The verifier first ran -- and failed, and spent its automatic retry --
        under an intermediate model-authority epoch (09-22).  A later roll
        (09-25) renamed the stage again, so its recovery chain no longer
        started at any Work the executor could derive.  The chain's root is
        still this admission's own sealed stage Work, exactly as the lane
        already accepts it; the escalation it ended in must stay readable.
        """

        fixture, authority, args, _registration, _launcher = self._fixture()
        self._enable_recovery(fixture, maximum=2)
        admission = authority.admit_from_plan(**args)
        first_execution = copy.deepcopy(admission["model_execution"])
        first_authority = copy.deepcopy(admission["model_authority"])
        first_authority["verifier"]["routing_policy_hash"] = "3" * 64
        authority.model_execution_resolver = lambda: (first_execution, first_authority)
        adapter = AlwaysUnprovedSendAdapter({"unused": True})
        executor, draft, _verifier = self._executor(
            fixture, authority, verifier_adapter=adapter)
        escalated = self._run_until(
            executor, admission,
            lambda item: item.get("reason")
            == "unproved_send_failed_after_automatic_retry")
        self.assertEqual(escalated["stage"], "independent_qualitative_verifier")
        calls = (draft.calls, adapter.calls)
        second_execution = copy.deepcopy(admission["model_execution"])
        second_authority = copy.deepcopy(admission["model_authority"])
        second_authority["verifier"]["routing_policy_hash"] = "4" * 64
        authority.model_execution_resolver = lambda: (second_execution, second_authority)
        # And, as live, a signing rolled the budget envelope in between.
        self._roll_mission(fixture, budget={
            **fixture.mission["budget"], "max_daily_cost_usd": 9.0})

        again = executor.run_once(admission["id"])

        self.assertEqual(again["status"], "stopped")
        self.assertEqual(again["reason"], "unproved_send_failed_after_automatic_retry")
        self.assertEqual((draft.calls, adapter.calls), calls)

    def test_epoch_inspector_classifies_an_unenqueued_model_prefix(self):
        fixture, authority, args, _registration, _launcher = self._fixture()
        admission = authority.admit_from_plan(**args)
        executor, draft, verifier = self._executor(fixture, authority)
        executor.run_once(admission["id"])
        executor.run_once(admission["id"])

        inspected = executor.inspect_model_authority_epoch_recovery(admission["id"])

        draft_status = next(
            item for item in inspected["stages"] if item["stage"] == "draft")
        self.assertEqual(draft_status["status"], "not_enqueued")
        self.assertEqual(draft_status["action"], "enqueue")
        self.assertEqual((draft.calls, verifier.calls), (0, 0))

    def test_model_roll_never_rebinds_claimed_historical_recovery(self):
        fixture, authority, args, _registration, _launcher = self._fixture()
        self._enable_recovery(fixture, maximum=1)
        admission = authority.admit_from_plan(**args)
        adapter = AlwaysCapacityAdapter({"unused": True})
        executor, _draft, _verifier = self._executor(
            fixture, authority, draft_adapter=adapter)
        recovery = self._run_until(
            executor, admission,
            lambda item: item.get("reason") == "fresh_work_recovery",
        )
        old_recovery_ref = recovery["work_order_ref"]
        core_path = next(
            row["file"] for row in fixture.store.connection.execute(
                "PRAGMA database_list") if row["name"] == "main")
        competing_scheduler = Scheduler(
            core_path, clock=fixture.harness.clock)
        self.addCleanup(competing_scheduler.close)
        claimed = competing_scheduler.claim(
            "worker:historical-recovery", work_order_id=old_recovery_ref,
        )
        self.assertIsNotNone(claimed)
        current_execution = copy.deepcopy(admission["model_execution"])
        current_authority = copy.deepcopy(admission["model_authority"])
        current_authority["draft"]["routing_policy_hash"] = "1" * 64
        authority.model_execution_resolver = lambda: (
            current_execution, current_authority
        )
        calls = adapter.calls

        with self.assertRaisesRegex(
            MissionDocumentResearchExecutorError,
            "requires exact unused recovery Work",
        ):
            executor.run_once(admission["id"])
        exact_link_ref = fixture.store.connection.execute(
            "SELECT recovery_link_id FROM mission_document_research_recovery_links "
            "WHERE recovery_work_order_ref=?", (old_recovery_ref,),
        ).fetchone()["recovery_link_id"]
        with self.assertRaisesRegex(
            sqlite3.IntegrityError,
            "requires atomically unused recovery Work",
        ):
            with executor._transaction() as cur:
                cur.execute(
                    "INSERT INTO "
                    "mission_document_research_model_authority_epoch_rebinds "
                    "VALUES(?,?,?,?,?,?,?,?,?,?)",
                    ("rebind:lost-race", admission["id"], 2,
                     exact_link_ref, old_recovery_ref, "work:current",
                     "work:rebound", "{}", "f" * 64, admission["created_at"]),
                )

        self.assertEqual(adapter.calls, calls)
        self.assertEqual(fixture.store.connection.execute(
            "SELECT count(*) FROM "
            "mission_document_research_model_authority_epoch_rebinds"
        ).fetchone()[0], 0)

    def test_model_epoch_rebind_recovers_enqueue_before_row_crash(self):
        fixture, authority, args, _registration, _launcher = self._fixture()
        self._enable_recovery(fixture, maximum=1)
        admission = authority.admit_from_plan(**args)
        adapter = AlwaysCapacityAdapter({"unused": True})
        executor, _draft, _verifier = self._executor(
            fixture, authority, draft_adapter=adapter)
        recovery = self._run_until(
            executor, admission,
            lambda item: item.get("reason") == "fresh_work_recovery",
        )
        old_recovery_ref = recovery["work_order_ref"]
        current_execution = copy.deepcopy(admission["model_execution"])
        current_authority = copy.deepcopy(admission["model_authority"])
        current_authority["draft"]["routing_policy_hash"] = "0" * 64
        authority.model_execution_resolver = lambda: (
            current_execution, current_authority
        )
        fired = []
        crashing, _draft, _verifier = self._executor(
            fixture, authority, draft_adapter=adapter,
            fault_injector=lambda seam: (
                fired.append(seam),
                (_ for _ in ()).throw(RuntimeError("crash")),
            )[1] if seam == "after_model_authority_epoch_rebind_enqueue" else None,
        )

        with self.assertRaisesRegex(RuntimeError, "crash"):
            crashing.run_once(admission["id"])
        self.assertEqual(fired, ["after_model_authority_epoch_rebind_enqueue"])
        self.assertEqual(fixture.store.connection.execute(
            "SELECT count(*) FROM "
            "mission_document_research_model_authority_epoch_rebinds"
        ).fetchone()[0], 1)
        restarted, _draft, _verifier = self._executor(
            fixture, authority, draft_adapter=adapter)

        restarted.run_once(admission["id"])

        core_path = next(
            row["file"] for row in fixture.store.connection.execute(
                "PRAGMA database_list") if row["name"] == "main")
        competing_scheduler = Scheduler(
            core_path, clock=fixture.harness.clock)
        self.addCleanup(competing_scheduler.close)
        with self.assertRaisesRegex(
            sqlite3.IntegrityError,
            "epoch rebind already consumed recovery Work",
        ):
            competing_scheduler.claim(
                "worker:lost-race", work_order_id=old_recovery_ref)

        self.assertEqual(fixture.store.connection.execute(
            "SELECT count(*) FROM "
            "mission_document_research_model_authority_epoch_rebinds"
        ).fetchone()[0], 1)
        self.assertEqual(fixture.store.connection.execute(
            "SELECT count(*) FROM mission_document_research_recovery_links"
        ).fetchone()[0], 1)

        current_execution = copy.deepcopy(admission["model_execution"])
        current_authority = copy.deepcopy(admission["model_authority"])
        current_authority["draft"]["routing_policy_hash"] = "7" * 64
        authority.model_execution_resolver = lambda: (
            current_execution, current_authority
        )
        calls_before_roll = adapter.calls
        with self.assertRaisesRegex(
            MissionDocumentResearchExecutorError,
            "model authority recovery is bound to an unresolved prior epoch",
        ):
            restarted.run_once(admission["id"])
        self.assertEqual(adapter.calls, calls_before_roll)
        # The old link and its one mapping remain immutable audit.  A second
        # policy roll cannot migrate the same authorization through another
        # epoch or fabricate another recovery allowance.
        self.assertEqual(fixture.store.connection.execute(
            "SELECT count(*) FROM mission_document_research_recovery_links"
        ).fetchone()[0], 1)
        self.assertEqual(fixture.store.connection.execute(
            "SELECT count(*) FROM "
            "mission_document_research_model_authority_epoch_rebinds"
        ).fetchone()[0], 1)

    def test_model_epoch_rebind_recovers_mapping_before_enqueue_crash(self):
        fixture, authority, args, _registration, _launcher = self._fixture()
        self._enable_recovery(fixture, maximum=1)
        admission = authority.admit_from_plan(**args)
        adapter = AlwaysCapacityAdapter({"unused": True})
        executor, _draft, _verifier = self._executor(
            fixture, authority, draft_adapter=adapter)
        recovery = self._run_until(
            executor, admission,
            lambda item: item.get("reason") == "fresh_work_recovery",
        )
        old_recovery_ref = recovery["work_order_ref"]
        current_execution = copy.deepcopy(admission["model_execution"])
        current_authority = copy.deepcopy(admission["model_authority"])
        current_authority["draft"]["routing_policy_hash"] = "d" * 64
        authority.model_execution_resolver = lambda: (
            current_execution, current_authority
        )
        fired = []
        crashing, _draft, _verifier = self._executor(
            fixture, authority, draft_adapter=adapter,
            fault_injector=lambda seam: (
                fired.append(seam),
                (_ for _ in ()).throw(RuntimeError("crash")),
            )[1] if seam == "after_model_authority_epoch_rebind_reservation" else None,
        )

        with self.assertRaisesRegex(RuntimeError, "crash"):
            crashing.run_once(admission["id"])

        self.assertEqual(
            fired, ["after_model_authority_epoch_rebind_reservation"])
        row = fixture.store.connection.execute(
            "SELECT rebound_work_order_ref FROM "
            "mission_document_research_model_authority_epoch_rebinds"
        ).fetchone()
        self.assertIsNotNone(row)
        self.assertIsNone(
            crashing.scheduler.work_order_authority(row["rebound_work_order_ref"]))
        inspected = crashing.inspect_model_authority_epoch_recovery(admission["id"])
        draft_status = next(
            item for item in inspected["stages"] if item["stage"] == "draft")
        self.assertEqual(draft_status["status"], "reserved_rebound_missing")
        self.assertEqual(
            draft_status["action"], "enqueue_exact_reserved_rebound_work")
        link = json.loads(fixture.store.connection.execute(
            "SELECT record_json FROM mission_document_research_recovery_links "
            "WHERE recovery_work_order_ref=?", (old_recovery_ref,),
        ).fetchone()["record_json"])
        authorized = crashing.scheduler.work_order_authority(
            old_recovery_ref)["work_order"]
        competing_authority = copy.deepcopy(current_authority)
        competing_authority["draft"]["routing_policy_hash"] = "8" * 64
        authority.model_execution_resolver = lambda: (
            current_execution, competing_authority
        )
        competing_admission = authority.resolve_for_execution(admission["id"])
        competing_blueprints = executor_module._blueprints(competing_admission)
        competing_base = executor_module._derive(
            competing_admission, crashing.scheduler, authority.registry,
            competing_blueprints[:2], 1,
        )
        competing_record = executor_module._epoch_rebind_record(
            competing_admission, 1, link, authorized, competing_base)
        work_count = fixture.store.connection.execute(
            "SELECT count(*) FROM scheduler_work_orders"
        ).fetchone()[0]
        with self.assertRaisesRegex(
            MissionDocumentResearchExecutorError,
            "model authority epoch rebind drifted",
        ):
            crashing._append_model_authority_epoch_rebind(
                competing_admission, 1, link, authorized, competing_base)
        self.assertIsNone(crashing.scheduler.work_order_authority(
            competing_record["rebound_work_order_ref"]))
        self.assertEqual(fixture.store.connection.execute(
            "SELECT count(*) FROM scheduler_work_orders"
        ).fetchone()[0], work_count)
        authority.model_execution_resolver = lambda: (
            current_execution, current_authority
        )
        with self.assertRaisesRegex(
            sqlite3.IntegrityError,
            "epoch rebind already consumed recovery Work",
        ):
            crashing.scheduler.claim(
                "worker:lost-race", work_order_id=old_recovery_ref)
        restarted, _draft, _verifier = self._executor(
            fixture, authority, draft_adapter=adapter)

        restarted.run_once(admission["id"])

        self.assertIsNotNone(
            restarted.scheduler.work_order_authority(row["rebound_work_order_ref"]))
        self.assertEqual(fixture.store.connection.execute(
            "SELECT count(*) FROM "
            "mission_document_research_model_authority_epoch_rebinds"
        ).fetchone()[0], 1)

    def test_succeeded_epoch_rebound_is_reused_after_another_policy_roll(self):
        fixture, authority, args, _registration, _launcher = self._fixture()
        self._enable_recovery(fixture, maximum=1)
        admission = authority.admit_from_plan(**args)
        statement = "Managed services revenue is recognized over time."
        adapter = CapacityOnceAdapter({
            "schema_version": "0.1", "status": "answered", "answer": statement,
            "candidate": {"normalized_statement": statement,
                          "metric_or_aspect": "revenue", "period": "current",
                          "basis": "reported", "cited_match_indexes": [0]},
            "missing": [],
        })
        executor, _draft, _verifier = self._executor(
            fixture, authority, draft_adapter=adapter)
        self._run_until(
            executor, admission,
            lambda item: item.get("reason") == "fresh_work_recovery",
        )
        current_execution = copy.deepcopy(admission["model_execution"])
        first_authority = copy.deepcopy(admission["model_authority"])
        first_authority["draft"]["routing_policy_hash"] = "3" * 64
        authority.model_execution_resolver = lambda: (
            current_execution, first_authority
        )

        succeeded = executor.run_once(admission["id"])

        self.assertEqual(succeeded["status"], "succeeded")
        calls = adapter.calls
        rebound_ref = succeeded["work_order_ref"]
        second_authority = copy.deepcopy(first_authority)
        second_authority["draft"]["routing_policy_hash"] = "4" * 64
        authority.model_execution_resolver = lambda: (
            current_execution, second_authority
        )

        executor.run_once(admission["id"])

        self.assertEqual(adapter.calls, calls)
        self.assertEqual(fixture.store.connection.execute(
            "SELECT count(*) FROM "
            "mission_document_research_model_authority_epoch_rebinds"
        ).fetchone()[0], 1)
        effective = effective_mission_document_work_orders(
            authority, executor.scheduler, admission["id"],
            draft_worker=executor.draft_worker,
            verifier_worker=executor.verifier_worker,
        )
        self.assertEqual(effective[1]["id"], rebound_ref)

    def test_failed_epoch_rebound_can_append_its_next_bounded_link(self):
        fixture, authority, args, _registration, _launcher = self._fixture()
        self._enable_recovery(fixture, maximum=2)
        admission = authority.admit_from_plan(**args)
        adapter = AlwaysCapacityAdapter({"unused": True})
        executor, _draft, _verifier = self._executor(
            fixture, authority, draft_adapter=adapter)
        first = self._run_until(
            executor, admission,
            lambda item: item.get("reason") == "fresh_work_recovery",
        )
        first_recovery_ref = first["work_order_ref"]
        current_execution = copy.deepcopy(admission["model_execution"])
        current_authority = copy.deepcopy(admission["model_authority"])
        current_authority["draft"]["routing_policy_hash"] = "b" * 64
        authority.model_execution_resolver = lambda: (
            current_execution, current_authority
        )
        failed = self._run_until(
            executor, admission, lambda item: item.get("status") == "failed")
        rebound_ref = failed["work_order_ref"]
        self.assertNotEqual(rebound_ref, first_recovery_ref)
        second = executor.run_once(admission["id"])

        self.assertEqual(second["reason"], "fresh_work_recovery")
        self.assertNotEqual(second["work_order_ref"], rebound_ref)
        self.assertEqual(fixture.store.connection.execute(
            "SELECT count(*) FROM mission_document_research_recovery_links"
        ).fetchone()[0], 2)
        self.assertEqual(fixture.store.connection.execute(
            "SELECT count(*) FROM "
            "mission_document_research_model_authority_epoch_rebinds"
        ).fetchone()[0], 1)
        self.assertIsNotNone(
            executor.scheduler.work_order_authority(second["work_order_ref"]))

    def test_roll_reuses_succeeded_historical_recovery_leaf(self):
        fixture, authority, args, _registration, _launcher = self._fixture()
        self._enable_recovery(fixture, maximum=1)
        admission = authority.admit_from_plan(**args)
        statement = "Managed services revenue is recognized over time."
        adapter = CapacityOnceAdapter({
            "schema_version": "0.1", "status": "answered", "answer": statement,
            "candidate": {"normalized_statement": statement,
                          "metric_or_aspect": "revenue", "period": "current",
                          "basis": "reported", "cited_match_indexes": [0]},
            "missing": [],
        })
        executor, _draft, _verifier = self._executor(
            fixture, authority, draft_adapter=adapter)
        recovered = self._run_until(
            executor, admission,
            lambda item: (item.get("status") == "succeeded"
                          and item.get("stage") == "qualitative_model_draft"
                          and item.get("work_order_ref")
                          != adapter.failed_work_id),
        )
        recovered_ref = recovered["work_order_ref"]
        calls = adapter.calls
        current_execution = copy.deepcopy(admission["model_execution"])
        current_authority = copy.deepcopy(admission["model_authority"])
        current_authority["draft"]["routing_policy_hash"] = "6" * 64
        authority.model_execution_resolver = lambda: (
            current_execution, current_authority
        )

        result = executor.run_once(admission["id"])

        self.assertEqual(result["status"], "admitted")
        self.assertEqual(adapter.calls, calls)
        self.assertEqual(fixture.store.connection.execute(
            "SELECT count(*) FROM mission_document_research_recovery_links"
        ).fetchone()[0], 1)
        self.assertEqual(fixture.store.connection.execute(
            "SELECT count(*) FROM "
            "mission_document_research_model_authority_epoch_rebinds"
        ).fetchone()[0], 0)
        effective = effective_mission_document_work_orders(
            authority, executor.scheduler, admission["id"],
            draft_worker=executor.draft_worker,
            verifier_worker=executor.verifier_worker,
        )
        self.assertEqual(effective[1]["id"], recovered_ref)

    def test_zero_policy_and_unknown_send_state_take_their_own_doors(self):
        """A disabled policy creates nothing; an unproved send buys one retry."""

        from dalton_core.openclaw_model_adapter import BrokerConnectionError

        class UnknownAdapter(CountingFakeAdapter):
            def execute(self, work, route, selected):
                self.calls += 1
                raise BrokerConnectionError("socket failed after an unknown boundary")

        for maximum, expected_reason, expected_links in (
            (0, "fresh_work_recovery_disabled", 0),
            (2, "automatic_bounded_unproved_send_retry", 1),
        ):
            with self.subTest(maximum=maximum):
                fixture, authority, args, _registration, _launcher = self._fixture()
                self._enable_recovery(fixture, maximum=maximum)
                admission = authority.admit_from_plan(**args)
                adapter = (CapacityOnceAdapter({"unused": True}) if maximum == 0
                           else UnknownAdapter({"unused": True}))
                executor, _draft, _verifier = self._executor(
                    fixture, authority, draft_adapter=adapter)
                while True:
                    current = executor.run_once(admission["id"])
                    if current["status"] == "failed":
                        break
                result = executor.run_once(admission["id"])
                self.assertEqual(result["reason"], expected_reason)
                self.assertEqual(fixture.store.connection.execute(
                    "SELECT count(*) FROM mission_document_research_recovery_links"
                ).fetchone()[0], expected_links)
                self.assertEqual(read_mission_document_research_observations(
                    fixture.store.connection)[0]["outcome"], "recovery_required")

    def _run_until(self, executor, admission, predicate, *, limit=20):
        current = None
        for _ in range(limit):
            current = executor.run_once(admission["id"])
            if predicate(current):
                return current
        self.fail(f"fixture never reached the expected state: {current}")

    def test_paid_contract_failure_buys_exactly_one_automatic_retry(self):
        """One contract rejection is retried by the lane; the second asks a person."""

        fixture, authority, args, _registration, _launcher = self._fixture()
        self._enable_recovery(fixture, maximum=2)
        admission = authority.admit_from_plan(**args)
        adapter = CountingFakeAdapter({"schema_version": "0.1", "status": "answered"})
        executor, _draft, _verifier = self._executor(
            fixture, authority, draft_adapter=adapter,
        )
        while True:
            current = executor.run_once(admission["id"])
            if current["status"] == "failed":
                break
        result = executor.run_once(admission["id"])
        self.assertEqual(result["status"], "admitted")
        self.assertEqual(result["reason"], "automatic_bounded_contract_retry")
        failed = executor._derive_work(admission, executor._blueprints(admission), 1)
        rows = fixture.store.connection.execute(
            "SELECT record_json FROM "
            "mission_document_research_controlled_recovery_authorizations"
        ).fetchall()
        self.assertEqual(len(rows), 1)
        authorization = json.loads(rows[0]["record_json"])
        self.assertEqual(authorization["actor_ref"],
                         "automation:document-research-contract-retry")
        self.assertEqual(authorization["kind"], "automation_bounded_contract_retry")
        self.assertEqual(authorization["max_fresh_work_orders"], 1)
        self.assertEqual(authorization["max_cost_usd"],
                         failed["budget"]["max_cost_usd"])
        self.assertEqual(authorization["failed_work_order_ref"], failed["id"])
        self.assertEqual(authorization["max_automatic_contract_retries_per_day"], 20)
        links = fixture.store.connection.execute(
            "SELECT record_json FROM mission_document_research_recovery_links"
        ).fetchall()
        self.assertEqual(len(links), 1)
        link = json.loads(links[0]["record_json"])
        self.assertEqual(link["failure_proof"]["classification"],
                         "automation_bounded_contract_retry")
        self.assertEqual(link["failure_proof"]["paid_contract_proof"]["classification"],
                         "proved_paid_output_contract_failure")
        self.assertGreater(
            link["failure_proof"]["paid_contract_proof"]["actual_micros"], 0)
        observation = next(
            item for item in read_mission_document_research_observations(
                fixture.store.connection)
            if item["recovery"]["reason"] == "automatic_bounded_contract_retry")
        self.assertIn("one bounded automatic retry", observation["meaning"])
        # The retry's reply fails the contract as well: escalate, and never buy
        # a second automatic reply.
        escalated = self._run_until(
            executor, admission,
            lambda item: item.get("reason") == "contract_failed_after_automatic_retry")
        self.assertEqual(escalated["status"], "stopped")
        for _ in range(2):
            repeated = executor.run_once(admission["id"])
            self.assertEqual(repeated["reason"],
                             "contract_failed_after_automatic_retry")
        self.assertEqual(fixture.store.connection.execute(
            "SELECT count(*) FROM mission_document_research_recovery_links"
        ).fetchone()[0], 1)
        self.assertEqual(fixture.store.connection.execute(
            "SELECT count(*) FROM "
            "mission_document_research_controlled_recovery_authorizations"
        ).fetchone()[0], 1)
        escalation = next(
            item for item in read_mission_document_research_observations(
                fixture.store.connection)
            if item["recovery"]["reason"] == "contract_failed_after_automatic_retry")
        self.assertIn("only an owner authorization", escalation["meaning"])
        self.assertEqual(escalation["recovery"]["proof"]["classification"],
                         "proved_paid_output_contract_failure")

    def test_unproved_send_buys_exactly_one_automatic_retry_that_can_succeed(self):
        """The seventeen live holds: retried once by the lane, not by a person."""

        fixture, authority, args, _registration, _launcher = self._fixture()
        self._enable_recovery(fixture, maximum=2)
        admission = authority.admit_from_plan(**args)
        statement = "Managed services revenue is recognized over time."
        adapter = UnprovedSendOnceAdapter({
            "schema_version": "0.1", "status": "answered", "answer": statement,
            "candidate": {"normalized_statement": statement,
                          "metric_or_aspect": "managed services revenue recognition",
                          "period": "current policy", "basis": "reported",
                          "cited_match_indexes": [0]}, "missing": [],
        })
        executor, _draft, _verifier = self._executor(
            fixture, authority, draft_adapter=adapter)
        while True:
            current = executor.run_once(admission["id"])
            if current["status"] == "failed":
                break
        failed = executor._derive_work(admission, executor._blueprints(admission), 1)
        result = executor.run_once(admission["id"])
        self.assertEqual(result["status"], "admitted")
        self.assertEqual(result["reason"], "automatic_bounded_unproved_send_retry")
        rows = fixture.store.connection.execute(
            "SELECT record_json FROM "
            "mission_document_research_controlled_recovery_authorizations"
        ).fetchall()
        self.assertEqual(len(rows), 1)
        authorization = json.loads(rows[0]["record_json"])
        self.assertEqual(authorization["actor_ref"],
                         "automation:document-research-unproved-send-retry")
        self.assertEqual(authorization["kind"],
                         "automation_bounded_unproved_send_retry")
        self.assertEqual(authorization["max_fresh_work_orders"], 1)
        self.assertEqual(authorization["max_cost_usd"],
                         failed["budget"]["max_cost_usd"])
        self.assertEqual(authorization["failed_work_order_ref"], failed["id"])
        self.assertEqual(
            authorization["max_automatic_unproved_send_retries_per_day"], 5)
        links = fixture.store.connection.execute(
            "SELECT record_json FROM mission_document_research_recovery_links"
        ).fetchall()
        self.assertEqual(len(links), 1)
        link = json.loads(links[0]["record_json"])
        self.assertEqual(link["failure_proof"]["classification"],
                         "automation_bounded_unproved_send_retry")
        self.assertEqual(
            link["failure_proof"]["unproved_send_record"]["classification"],
            "unproved_send_state")
        self.assertEqual(
            link["failure_proof"]["unproved_send_record"]["worst_case"],
            "earlier_send_may_have_been_sent_and_charged")
        observation = next(
            item for item in read_mission_document_research_observations(
                fixture.store.connection)
            if item["recovery"]["reason"] == "automatic_bounded_unproved_send_retry")
        # The worst case is stated in words, not left to be worked out.
        self.assertIn("one bounded automatic retry", observation["meaning"])
        self.assertIn("sent and charged", observation["meaning"])
        self.assertIn("one extra paid call", observation["meaning"])
        # The retry answers, so the admission finishes with no person involved.
        final = self._run_until(
            executor, admission,
            lambda item: item.get("research_status") == "candidate_staged")
        self.assertEqual(final["research_status"], "candidate_staged")
        self.assertEqual(fixture.store.connection.execute(
            "SELECT count(*) FROM mission_document_research_recovery_links"
        ).fetchone()[0], 1)

    def test_unproved_send_retry_that_fails_escalates_and_buys_nothing_more(self):
        fixture, authority, args, _registration, _launcher = self._fixture()
        self._enable_recovery(fixture, maximum=2)
        admission = authority.admit_from_plan(**args)
        adapter = AlwaysUnprovedSendAdapter({"unused": True})
        executor, _draft, _verifier = self._executor(
            fixture, authority, draft_adapter=adapter)
        admitted = self._run_until(
            executor, admission,
            lambda item: item.get("reason")
            == "automatic_bounded_unproved_send_retry")
        self.assertEqual(admitted["status"], "admitted")
        escalated = self._run_until(
            executor, admission,
            lambda item: item.get("reason")
            == "unproved_send_failed_after_automatic_retry")
        self.assertEqual(escalated["status"], "stopped")
        for _ in range(3):
            repeated = executor.run_once(admission["id"])
            self.assertEqual(repeated["reason"],
                             "unproved_send_failed_after_automatic_retry")
        self.assertEqual(fixture.store.connection.execute(
            "SELECT count(*) FROM mission_document_research_recovery_links"
        ).fetchone()[0], 1)
        self.assertEqual(fixture.store.connection.execute(
            "SELECT count(*) FROM "
            "mission_document_research_controlled_recovery_authorizations"
        ).fetchone()[0], 1)
        escalation = next(
            item for item in read_mission_document_research_observations(
                fixture.store.connection)
            if item["recovery"]["reason"]
            == "unproved_send_failed_after_automatic_retry")
        self.assertIn("already issued", escalation["meaning"])
        self.assertIn("only an owner authorization", escalation["meaning"])

    def test_provider_budget_refusal_is_held_not_retried_on_the_same_route(self):
        """Live: paid 0.54 USD, refused, retried automatically, paid again."""

        fixture, authority, args, _registration, _launcher = self._fixture()
        self._enable_recovery(fixture, maximum=2)
        admission = authority.admit_from_plan(**args)
        adapter = ProviderBudgetExceededAdapter({"unused": True})
        executor, _draft, _verifier = self._executor(
            fixture, authority, draft_adapter=adapter)
        stopped = self._run_until(
            executor, admission,
            lambda item: item.get("status") == "stopped")
        self.assertEqual(stopped["reason"],
                         executor_module.PROVIDER_BUDGET_EXCEEDED_NOT_RETRIED)
        calls = adapter.calls
        for _ in range(3):
            self.assertEqual(executor.run_once(admission["id"])["reason"],
                             executor_module.PROVIDER_BUDGET_EXCEEDED_NOT_RETRIED)
        self.assertEqual(adapter.calls, calls)
        self.assertEqual(fixture.store.connection.execute(
            "SELECT count(*) FROM mission_document_research_recovery_links"
        ).fetchone()[0], 0)
        self.assertEqual(fixture.store.connection.execute(
            "SELECT count(*) FROM "
            "mission_document_research_controlled_recovery_authorizations"
        ).fetchone()[0], 0)
        lane = MissionDocumentResearchCoordinator(
            store=fixture.store, launcher=None, clock=fixture.harness.clock)
        work_ref = next(
            item["work_order_ref"] for item in read_mission_document_research_observations(
                fixture.store.connection)
            if item["recovery"] and item["recovery"]["reason"]
            == executor_module.PROVIDER_BUDGET_EXCEEDED_NOT_RETRIED)
        self.assertEqual(lane._typed_recovery_state(admission, work_ref)["action"],
                         "recovery_required")
        # The owner's one-call door is the unproved-send door.
        from dalton_core.mission_document_research_lane import (
            _escalated_stage_ordinal, _recovery_doors,
        )
        self.assertEqual(_escalated_stage_ordinal(
            fixture.store.connection, admission,
            _recovery_doors()["unproved"]["reasons"]), 2)

    def test_unproved_send_retry_stops_at_its_own_daily_cap_and_waits(self):
        """The cap is a spending bound, so its answer is tomorrow, not a person."""

        fixture, authority, args, _registration, _launcher = self._fixture()
        self._enable_recovery(fixture, maximum=2)
        admission = authority.admit_from_plan(**args)
        statement = "Managed services revenue is recognized over time."
        draft = UnprovedSendOnceAdapter({
            "schema_version": "0.1", "status": "answered", "answer": statement,
            "candidate": {"normalized_statement": statement,
                          "metric_or_aspect": "managed services revenue recognition",
                          "period": "current policy", "basis": "reported",
                          "cited_match_indexes": [0]}, "missing": [],
        })
        verifier = UnprovedSendOnceAdapter({
            "schema_version": "0.1", "verdict": "pass",
            "verified_statement": statement, "findings": [],
        })
        executor, _draft, _verifier = self._executor(
            fixture, authority, draft_adapter=draft, verifier_adapter=verifier,
            max_automatic_unproved_send_retries_per_day=1)
        # The draft stage spends the day's single retry; the verifier stage's
        # own unproved failure then has to wait for the UTC reset.
        capped = self._run_until(
            executor, admission,
            lambda item: item.get("reason")
            == "automatic_unproved_send_retry_day_cap_reached")
        self.assertEqual(capped["status"], "waiting")
        self.assertEqual(
            capped["retry_at"],
            (NOW.replace(hour=0, minute=0, second=0, microsecond=0)
             + timedelta(days=1)).isoformat(timespec="microseconds"))
        self.assertEqual(fixture.store.connection.execute(
            "SELECT count(*) FROM "
            "mission_document_research_controlled_recovery_authorizations"
        ).fetchone()[0], 1)
        observation = next(
            item for item in read_mission_document_research_observations(
                fixture.store.connection)
            if item["recovery"]["reason"]
            == "automatic_unproved_send_retry_day_cap_reached")
        self.assertEqual(observation["recovery"]["day"], NOW.date().isoformat())
        self.assertIn("after the UTC day resets", observation["meaning"])
        # The same tick repeated is the same immutable row, and buys nothing.
        self.assertEqual(executor.run_once(admission["id"])["reason"],
                         "automatic_unproved_send_retry_day_cap_reached")
        self.assertEqual(fixture.store.connection.execute(
            "SELECT count(*) FROM mission_document_research_recovery_links"
        ).fetchone()[0], 1)
        # A new UTC day releases the cap and the retry is taken automatically.
        fixture.harness.clock.value = NOW + timedelta(days=1)
        released = executor.run_once(admission["id"])
        self.assertEqual(released["reason"], "automatic_bounded_unproved_send_retry")
        self.assertEqual(fixture.store.connection.execute(
            "SELECT count(*) FROM mission_document_research_recovery_links"
        ).fetchone()[0], 2)

    def test_pre_change_unproved_send_hold_is_picked_up_by_the_automatic_retry(self):
        """The seventeen live holds: recorded before the retry existed."""

        fixture, authority, args, _registration, _launcher = self._fixture()
        self._enable_recovery(fixture, maximum=2)
        admission = authority.admit_from_plan(**args)
        adapter = AlwaysUnprovedSendAdapter({"unused": True})
        executor, _draft, _verifier = self._executor(
            fixture, authority, draft_adapter=adapter)
        while True:
            current = executor.run_once(admission["id"])
            if current["status"] == "failed":
                break
        work = executor._derive_work(admission, executor._blueprints(admission), 1)
        formal = executor.scheduler.formal_result(work["id"])
        # Exactly what every earlier release wrote and then waited on forever.
        executor._recovery_observation(admission, work, formal, 1, {
            "status": "stopped", "reason": "send_state_unproved",
            "eligible": False, "used_fresh_work_orders": 0,
            "max_fresh_work_orders": 2, "retry_at": None,
            "deadline": (NOW + timedelta(hours=1)).isoformat(
                timespec="microseconds"),
            "proof": None,
        })
        lane = MissionDocumentResearchCoordinator(
            store=fixture.store, launcher=None, clock=fixture.harness.clock,
        )
        self.assertEqual(lane._typed_recovery_state(admission, work["id"]), {
            "action": "resume",
            "reason": "automatic_unproved_send_retry_available",
            "work_order_ref": work["id"],
        })
        # Re-entering spends the automatic retry rather than asking a person.
        self.assertEqual(executor.run_once(admission["id"])["reason"],
                         "automatic_bounded_unproved_send_retry")
        self.assertEqual(fixture.store.connection.execute(
            "SELECT count(*) FROM mission_document_research_recovery_links"
        ).fetchone()[0], 1)
        # The newer row supersedes the legacy verdict for the same Work.
        self.assertEqual(
            lane._typed_recovery_state(admission, work["id"])["reason"],
            "controlled_unproved_send_retry_admitted")

    def test_owner_authorizes_one_exact_unproved_send_recovery_without_a_model_call(self):
        """The owner door, on the state the automatic retry escalated to."""

        fixture, authority, args, _registration, _launcher = self._fixture()
        self._enable_recovery(fixture, maximum=2)
        admission = authority.admit_from_plan(**args)
        adapter = AlwaysUnprovedSendAdapter({"unused": True})
        executor, _draft, _verifier = self._executor(
            fixture, authority, draft_adapter=adapter)
        self._run_until(
            executor, admission,
            lambda item: item.get("reason")
            == "unproved_send_failed_after_automatic_retry")
        work, automatic = executor_module._effective_stage(
            authority, executor.scheduler, admission, 1,
            worker=executor.draft_worker)
        self.assertEqual([item["failure_proof"]["classification"]
                          for item in automatic],
                         ["automation_bounded_unproved_send_retry"])
        formal = executor.scheduler.formal_result(work["id"])
        body = {
            "schema_version": "0.1",
            "actor_ref": "operator:owner-authorized-document-recovery",
            "admission_ref": admission["id"],
            "admission_hash": admission["content_hash"],
            "stage_ordinal": 2, "failed_work_order_ref": work["id"],
            "failed_work_order_hash": content_hash(work),
            "formal_result_ref": _formal_ref(formal),
            "formal_result_hash": _formal_hash(formal),
            "max_fresh_work_orders": 1,
            "max_cost_usd": work["budget"]["max_cost_usd"],
            "authorized_at": (NOW + timedelta(minutes=1)).isoformat(),
        }
        authorization = {
            **body,
            "id": "mission-document-unproved-send-recovery-authorization:"
            + content_hash(body)[:32],
        }
        authorization["content_hash"] = content_hash(authorization)
        calls = adapter.calls
        result = executor.authorize_unproved_send_recovery(
            admission["id"], authorization)
        self.assertEqual(result["status"], "admitted")
        self.assertEqual(result["model_calls"], 0)
        self.assertEqual(adapter.calls, calls)
        self.assertEqual(executor.authorize_unproved_send_recovery(
            admission["id"], authorization), result)
        self.assertEqual(fixture.store.connection.execute(
            "SELECT count(*) FROM "
            "mission_document_research_controlled_recovery_authorizations"
        ).fetchone()[0], 2)
        _owned, owner_links = executor_module._effective_stage(
            authority, executor.scheduler, admission, 1,
            worker=executor.draft_worker)
        self.assertEqual([item["failure_proof"]["classification"]
                          for item in owner_links],
                         ["automation_bounded_unproved_send_retry",
                          "owner_authorized_unproved_send_retry"])
        changed = dict(authorization)
        changed["max_cost_usd"] = authorization["max_cost_usd"] + 1
        changed["content_hash"] = content_hash(
            {key: value for key, value in changed.items() if key != "content_hash"})
        with self.assertRaises(MissionDocumentResearchExecutorError):
            executor.authorize_unproved_send_recovery(admission["id"], changed)

    def _owner_door(self, adapter, *, door, reason):
        """Drive one admission to an escalated state and open its owner door."""

        from dalton_core.mission_document_research_lane import (
            authorize_owner_recovery,
        )

        fixture, authority, args, _registration, _launcher = self._fixture()
        self._enable_recovery(fixture, maximum=2)
        admission = authority.admit_from_plan(**args)
        executor, _draft, _verifier = self._executor(
            fixture, authority, draft_adapter=adapter)
        self._run_until(executor, admission,
                        lambda item: item.get("reason") == reason)
        work, _links = executor_module._effective_stage(
            authority, executor.scheduler, admission, 1,
            worker=executor.draft_worker)
        ceiling = float(work["budget"]["max_cost_usd"])
        self.assertGreater(ceiling, 0)
        # A cap below what this stage costs is a refusal, not a smaller buy:
        # the authorization must carry the failed Work's own ceiling.
        calls = adapter.calls
        refused = authorize_owner_recovery(
            executor, admission["id"], door=door, actor_ref="human:lumos",
            max_cost_usd=ceiling / 2)
        self.assertEqual(refused["status"], "refused")
        self.assertEqual(refused["reason"], "stage_budget_exceeds_authorized_cap")
        self.assertEqual(refused["max_cost_usd"], work["budget"]["max_cost_usd"])
        self.assertEqual(adapter.calls, calls)
        self.assertEqual(fixture.store.connection.execute(
            "SELECT count(*) FROM "
            "mission_document_research_controlled_recovery_authorizations"
        ).fetchone()[0], 1)
        granted = authorize_owner_recovery(
            executor, admission["id"], door=door, actor_ref="human:lumos",
            max_cost_usd=ceiling)
        self.assertEqual(granted["status"], "admitted")
        self.assertEqual(granted["model_calls"], 0)
        self.assertEqual(granted["stage_ordinal"], 2)
        self.assertEqual(granted["authorized_by"], "human:lumos")
        self.assertEqual(adapter.calls, calls)
        # Replayed -- a retried writer call, a re-run CLI -- it is the same
        # authorization row and the same answer, not a second purchase.
        self.assertEqual(
            authorize_owner_recovery(
                executor, admission["id"], door=door, actor_ref="human:lumos"),
            granted)
        self.assertEqual(fixture.store.connection.execute(
            "SELECT count(*) FROM "
            "mission_document_research_controlled_recovery_authorizations"
        ).fetchone()[0], 2)
        self.assertEqual(adapter.calls, calls)
        # And the other door refuses to touch a state that is not its own.
        other = "unproved" if door == "paid" else "paid"
        self.assertEqual(
            authorize_owner_recovery(
                executor, admission["id"], door=other,
                actor_ref="human:lumos")["status"],
            "not_escalated")
        return fixture, authority, executor, admission, granted

    def test_owner_paid_door_builds_the_exact_authorization_and_replays(self):
        """The escalation's own door, opened the way the owner can reach it."""

        _fixture, authority, executor, admission, granted = self._owner_door(
            CountingFakeAdapter({"schema_version": "0.1", "status": "answered"}),
            door="paid", reason="contract_failed_after_automatic_retry")
        self.assertTrue(granted["authorization_ref"].startswith(
            "mission-document-paid-recovery-authorization:"))
        _work, links = executor_module._effective_stage(
            authority, executor.scheduler,
            authority.resolve_for_execution(admission["id"]), 1,
            worker=executor.draft_worker)
        self.assertEqual([item["failure_proof"]["classification"] for item in links],
                         ["automation_bounded_contract_retry",
                          "owner_authorized_paid_contract_retry"])

    def test_owner_unproved_door_builds_the_exact_authorization_and_replays(self):
        _fixture, authority, executor, admission, granted = self._owner_door(
            AlwaysUnprovedSendAdapter({"unused": True}),
            door="unproved", reason="unproved_send_failed_after_automatic_retry")
        self.assertTrue(granted["authorization_ref"].startswith(
            "mission-document-unproved-send-recovery-authorization:"))
        _work, links = executor_module._effective_stage(
            authority, executor.scheduler,
            authority.resolve_for_execution(admission["id"]), 1,
            worker=executor.draft_worker)
        self.assertEqual([item["failure_proof"]["classification"] for item in links],
                         ["automation_bounded_unproved_send_retry",
                          "owner_authorized_unproved_send_retry"])

    def test_changed_ticket_identity_rebinds_when_the_admission_did_not_change(self):
        """The ten live holds: a moved ticket name, not a moved admission."""

        from dalton_core.lane_child_launcher import LaneChildRejected, write_owner_only
        from dalton_core.mission_document_research_launcher import (
            MissionDocumentResearchLauncher,
        )

        fixture, authority, args, _registration, _launcher = self._fixture()
        admission = authority.admit_from_plan(**args)
        staging = fixture.state / "rebind-staging.sqlite"
        staging.write_bytes(b"staging")
        planner_db = fixture.state / "rebind-core.sqlite"
        planner_db.write_bytes(b"core")
        planner_config = fixture.state / "rebind-planner-config.json"
        planner_config.write_text("{}\n", encoding="utf-8")
        document_config = fixture.state / "rebind-document-config.json"
        document_config.write_text('{"release": "one"}\n', encoding="utf-8")
        draft_config = fixture.state / "rebind-draft-model-config.json"
        draft_config.write_text("{}\n", encoding="utf-8")
        verifier_config = fixture.state / "rebind-verifier-model-config.json"
        verifier_config.write_text("{}\n", encoding="utf-8")
        launcher = MissionDocumentResearchLauncher(
            state_dir=fixture.state, staging_path=staging,
            planner_scheduler_db=planner_db,
            planner_model_config_path=planner_config,
            draft_model_config_path=draft_config,
            verifier_model_config_path=verifier_config,
            document_config_path=document_config,
        )
        self.addCleanup(launcher.close)
        configuration = launcher.configuration()
        signature = {
            "admission_ref": admission["id"],
            "admission_hash": admission["content_hash"],
            "configuration": configuration,
        }
        prior_ref = ("mission-document-research:"
                     + content_hash(signature)[:24])
        ticket_path = launcher._ticket_path(prior_ref)
        ticket_path.parent.mkdir(parents=True, exist_ok=True)
        write_owner_only(ticket_path, {
            "schema_version": "0.1", "id": prior_ref, **signature,
            "configuration_hash": content_hash(configuration),
            "started_at": NOW.isoformat(), "pid": 1, "command": ["true"],
            "status": "failed", "exit_code": 1,
            "completed_at": NOW.isoformat(),
        })
        summary = {
            "schema_version": "0.1", "created_at": NOW.isoformat(),
            "admission_ref": admission["id"],
            "admission_hash": admission["content_hash"],
            "status": "incomplete", "outcomes": [], "error": None,
        }
        write_owner_only(ticket_path.with_name("summary.json"),
                         {**summary, "content_hash": content_hash(summary)})
        # A release moves the document config bytes.  Nothing about the
        # admission changed, but the ticket identity is a digest of both.
        document_config.write_text('{"release": "two"}\n', encoding="utf-8")
        spawned = []

        def fake_spawn(*, digest, record, _controlled_reentry=None, **kwargs):
            spawned.append({"digest": digest, "record": dict(record),
                            "reentry": _controlled_reentry})
            return {"id": f"mission-document-research:{digest}",
                    "status": "running", **dict(record)}

        with patch.object(launcher, "spawn", side_effect=fake_spawn):
            rebound = launcher.resume(
                admission_ref=admission["id"],
                admission_hash=admission["content_hash"],
                prior_ticket_ref=prior_ref,
                authorization="test:exact-scheduler-replay")
        self.assertEqual(rebound["rebound_from_ticket_ref"], prior_ref)
        self.assertNotEqual(rebound["id"], prior_ref)
        self.assertEqual(len(spawned), 1)
        self.assertEqual(spawned[0]["record"]["rebound_from_ticket_ref"], prior_ref)
        # The one-shot re-entry claim is archived against the prior run.
        markers = sorted(ticket_path.parent.glob("controlled-reentry-*.json"))
        self.assertEqual(len(markers), 1)
        # ``spawn`` was stubbed, so nothing ever ran under that claim.  A claim
        # that bought nothing is not an attempt: the next tick completes it
        # rather than telling a person the lane already tried.  This is the
        # live 2026-09-18 shape -- three admissions held on
        # "controlled reentry was already attempted" having never re-entered.
        with patch.object(launcher, "spawn", side_effect=fake_spawn):
            again = launcher.resume(
                admission_ref=admission["id"],
                admission_hash=admission["content_hash"],
                prior_ticket_ref=prior_ref,
                authorization="test:exact-scheduler-replay")
        self.assertEqual(again["rebound_from_ticket_ref"], prior_ref)
        self.assertEqual(len(spawned), 2)
        self.assertEqual(
            len(sorted(ticket_path.parent.glob("controlled-reentry-*.json"))), 1)
        # Now a child really runs under that claim: the rebound ticket exists
        # and started after it.  From here the attempt has happened.
        rebound_path = launcher._ticket_path(rebound["id"])
        rebound_path.parent.mkdir(parents=True, exist_ok=True)
        moved = launcher.configuration()
        write_owner_only(rebound_path, {
            "schema_version": "0.1", "id": rebound["id"],
            "admission_ref": admission["id"],
            "admission_hash": admission["content_hash"],
            "configuration": moved,
            "configuration_hash": content_hash(moved),
            "rebound_from_ticket_ref": prior_ref,
            "started_at": (datetime.now(timezone.utc)
                           + timedelta(minutes=5)).isoformat(),
            "pid": 1, "command": ["true"], "status": "failed",
            "exit_code": 1, "completed_at": None,
        })
        with patch.object(launcher, "spawn", side_effect=fake_spawn):
            with self.assertRaisesRegex(
                LaneChildRejected, "already attempted",
            ):
                launcher.resume(
                    admission_ref=admission["id"],
                    admission_hash=admission["content_hash"],
                    prior_ticket_ref=prior_ref,
                    authorization="test:exact-scheduler-replay")
        self.assertEqual(len(spawned), 2)
        # The owner door: one more re-entry, once, recorded with who said so.
        granted = launcher.authorize_controlled_reentry(
            admission["id"], actor_ref="human:lumos",
            granted_at=NOW.isoformat())
        self.assertEqual(granted["status"], "granted")
        self.assertEqual(granted["actor_ref"], "human:lumos")
        with patch.object(launcher, "spawn", side_effect=fake_spawn):
            allowed = launcher.resume(
                admission_ref=admission["id"],
                admission_hash=admission["content_hash"],
                prior_ticket_ref=prior_ref,
                authorization="test:exact-scheduler-replay")
        self.assertEqual(allowed["rebound_from_ticket_ref"], prior_ref)
        self.assertEqual(len(spawned), 3)
        self.assertFalse(Path(granted["grant_path"]).exists())
        self.assertEqual(len(sorted(
            launcher.tickets_dir.glob("controlled-reentry-grant-*-used-*.json"))), 1)
        # And the grant is spent: the next one is refused again.
        with patch.object(launcher, "spawn", side_effect=fake_spawn):
            with self.assertRaisesRegex(
                LaneChildRejected, "already attempted",
            ):
                launcher.resume(
                    admission_ref=admission["id"],
                    admission_hash=admission["content_hash"],
                    prior_ticket_ref=prior_ref,
                    authorization="test:exact-scheduler-replay")
        self.assertEqual(len(spawned), 3)
        # A moved admission is a different thing entirely and is still refused.
        with patch.object(launcher, "spawn", side_effect=fake_spawn):
            with self.assertRaisesRegex(
                LaneChildRejected, "changed ticket identity",
            ):
                launcher.resume(
                    admission_ref=admission["id"], admission_hash="0" * 64,
                    prior_ticket_ref=prior_ref,
                    authorization="test:other-authorization")
        self.assertEqual(len(spawned), 3)

    def test_systemic_child_failure_is_completed_once_then_reaches_a_person(self):
        """The live 2026-09-18 escalations, at the real instants they happened.

        Six legacy admissions escalated to the owner saying the lane had
        already tried.  It had: the re-entered child really ran -- six
        milliseconds after the claim -- and died on ``requires the active
        mission``, because a source-plan change had rolled the mission version
        under them.  Nothing was sent and nothing was charged, so that attempt
        bought nothing; once the executor accepts the lineage the lane must
        complete it by itself rather than ask a person about a defect.  Once,
        though: a second death on the same condition is a real fault.
        """

        from dalton_core.lane_child_launcher import LaneChildRejected, write_owner_only
        from dalton_core.lane_reentry_claim import claim_path, systemic_path
        from dalton_core.mission_document_research_launcher import (
            MissionDocumentResearchLauncher,
        )

        fixture, authority, args, _registration, _launcher = self._fixture()
        admission = authority.admit_from_plan(**args)
        for name, body in (("systemic-staging.sqlite", b"staging"),
                           ("systemic-core.sqlite", b"core")):
            (fixture.state / name).write_bytes(body)
        for name in ("systemic-planner-config.json", "systemic-document-config.json",
                     "systemic-draft-model-config.json",
                     "systemic-verifier-model-config.json"):
            (fixture.state / name).write_text("{}\n", encoding="utf-8")
        launcher = MissionDocumentResearchLauncher(
            state_dir=fixture.state,
            staging_path=fixture.state / "systemic-staging.sqlite",
            planner_scheduler_db=fixture.state / "systemic-core.sqlite",
            planner_model_config_path=fixture.state / "systemic-planner-config.json",
            draft_model_config_path=fixture.state / "systemic-draft-model-config.json",
            verifier_model_config_path=(
                fixture.state / "systemic-verifier-model-config.json"),
            document_config_path=fixture.state / "systemic-document-config.json",
        )
        self.addCleanup(launcher.close)
        configuration = launcher.configuration()
        signature = {
            "admission_ref": admission["id"],
            "admission_hash": admission["content_hash"],
            "configuration": configuration,
        }
        prior_ref = "mission-document-research:" + content_hash(signature)[:24]
        ticket_path = launcher._ticket_path(prior_ref)
        ticket_path.parent.mkdir(parents=True, exist_ok=True)
        # The exact instants from legacy ticket 897e433770c53844a036f208.
        claimed_at = "2026-09-18T09:57:37.393651+00:00"
        started_at = "2026-09-18T09:57:37.399947+00:00"
        write_owner_only(ticket_path, {
            "schema_version": "0.1", "id": prior_ref, **signature,
            "configuration_hash": content_hash(configuration),
            "started_at": started_at, "pid": 1, "command": ["true"],
            "status": "failed", "exit_code": 1,
            "completed_at": "2026-09-18T09:57:51.919136+00:00",
        })
        summary = {
            "schema_version": "0.1",
            "created_at": "2026-09-18T09:57:51.919136+00:00",
            "admission_ref": admission["id"],
            "admission_hash": admission["content_hash"],
            "status": "failed", "outcomes": [],
            "error": ("MissionDocumentResearchError: directed document research "
                      "requires the active mission"),
        }
        write_owner_only(ticket_path.with_name("summary.json"),
                         {**summary, "content_hash": content_hash(summary)})
        authorization = "test:exact-scheduler-replay"
        write_owner_only(claim_path(launcher, prior_ref, authorization), {
            "schema_version": "0.3", "ticket_ref": prior_ref,
            "authorization": authorization, "claimed_at": claimed_at,
            "lane_input": None, "prior_log_base64": "",
            "prior_log_sha256": "0" * 64,
            "prior_summary_base64": "", "prior_summary_sha256": "0" * 64,
        })
        # Before the fix this was the end of the road: the claim was spent, so
        # the lane escalated and there was nothing automatic left.
        self.assertFalse(launcher.controlled_reentry_consumed(
            prior_ref, authorization))
        spawned = []

        def fake_spawn(*, digest, record, _controlled_reentry=None, **kwargs):
            spawned.append({"digest": digest, "reentry": _controlled_reentry})
            return {"id": f"mission-document-research:{digest}",
                    "status": "running", **dict(record)}

        with patch.object(launcher, "spawn", side_effect=fake_spawn):
            resumed = launcher.resume(
                admission_ref=admission["id"],
                admission_hash=admission["content_hash"],
                prior_ticket_ref=prior_ref, authorization=authorization)
        self.assertEqual(resumed["id"], prior_ref)
        self.assertEqual(len(spawned), 1)
        # Completed, not re-claimed: the marker's name is already taken.
        self.assertIsNone(spawned[0]["reentry"])
        recorded = systemic_path(launcher, prior_ref, authorization)
        self.assertTrue(recorded.is_file())
        self.assertIn("requires the active mission",
                      json.loads(recorded.read_text(encoding="utf-8"))["reason"])
        # And exactly once.  A second death on the same condition is a fault
        # the owner has to look at, which is what the owner door is for.
        self.assertTrue(launcher.controlled_reentry_consumed(
            prior_ref, authorization))
        with patch.object(launcher, "spawn", side_effect=fake_spawn):
            with self.assertRaisesRegex(LaneChildRejected, "already attempted"):
                launcher.resume(
                    admission_ref=admission["id"],
                    admission_hash=admission["content_hash"],
                    prior_ticket_ref=prior_ref, authorization=authorization)
        self.assertEqual(len(spawned), 1)

    def test_a_child_that_failed_on_its_own_work_is_not_completed_again(self):
        """Only a systemic condition buys the extra completion."""

        from dalton_core.lane_child_launcher import write_owner_only
        from dalton_core.lane_reentry_claim import claim_consumed, claim_path

        class _Launcher:
            def __init__(self, root):
                self.tickets_dir = root

            def _ticket_path(self, ticket_ref):
                return self.tickets_dir / ticket_ref.split(":")[-1] / "ticket.json"

        import tempfile

        root = Path(tempfile.mkdtemp(prefix="systemic-claim"))
        self.addCleanup(lambda: __import__("shutil").rmtree(root, ignore_errors=True))
        launcher = _Launcher(root)
        ticket_ref = "mission-document-research:abc"
        path = launcher._ticket_path(ticket_ref)
        path.parent.mkdir(parents=True, exist_ok=True)
        write_owner_only(path, {"id": ticket_ref, "started_at": "2026-09-18T09:57:38+00:00"})
        write_owner_only(claim_path(launcher, ticket_ref, "auth"),
                         {"claimed_at": "2026-09-18T09:57:37+00:00"})
        for error, consumed in (
            ("ResearchAutoCommitRejected: document qualitative rule admits "
             "no numeric statement", True),
            ("MissionDocumentResearchError: directed document research requires "
             "the active mission", False),
        ):
            write_owner_only(path.with_name("summary.json"),
                             {"status": "failed", "error": error})
            with self.subTest(error=error):
                self.assertEqual(
                    claim_consumed(launcher, ticket_ref, "auth"), consumed)

    def test_automatic_contract_retry_replays_its_own_authorization_after_a_crash(self):
        """One failed Work carries one authorization, even across a crash.

        Minting a second one on the next day would collide with the ledger's
        uniqueness and wedge the admission for good, so the stored row and its
        original instant are reused.
        """

        fixture, authority, args, _registration, _launcher = self._fixture()
        self._enable_recovery(fixture, maximum=2)
        admission = authority.admit_from_plan(**args)
        adapter = CountingFakeAdapter({"schema_version": "0.1", "status": "answered"})
        executor, _draft, _verifier = self._executor(
            fixture, authority, draft_adapter=adapter)
        while True:
            current = executor.run_once(admission["id"])
            if current["status"] == "failed":
                break
        enqueue = executor.scheduler.enqueue

        def crash(_work):
            raise RuntimeError("crash between the authorization and the Work")

        executor.scheduler.enqueue = crash
        with self.assertRaisesRegex(RuntimeError, "crash"):
            executor.run_once(admission["id"])
        executor.scheduler.enqueue = enqueue
        rows = fixture.store.connection.execute(
            "SELECT record_json FROM "
            "mission_document_research_controlled_recovery_authorizations"
        ).fetchall()
        self.assertEqual(len(rows), 1)
        authorized_at = json.loads(rows[0]["record_json"])["authorized_at"]
        self.assertEqual(fixture.store.connection.execute(
            "SELECT count(*) FROM mission_document_research_recovery_links"
        ).fetchone()[0], 0)
        fixture.harness.clock.value = NOW + timedelta(days=1)
        result = executor.run_once(admission["id"])
        self.assertEqual(result["reason"], "automatic_bounded_contract_retry")
        rows = fixture.store.connection.execute(
            "SELECT record_json FROM "
            "mission_document_research_controlled_recovery_authorizations"
        ).fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(json.loads(rows[0]["record_json"])["authorized_at"],
                         authorized_at)
        self.assertEqual(fixture.store.connection.execute(
            "SELECT count(*) FROM mission_document_research_recovery_links"
        ).fetchone()[0], 1)

    def test_automatic_contract_retry_that_answers_lets_the_admission_finish(self):
        fixture, authority, args, _registration, _launcher = self._fixture()
        self._enable_recovery(fixture, maximum=2)
        admission = authority.admit_from_plan(**args)
        statement = "Managed services revenue is recognized over time."
        adapter = ContractRejectOnceAdapter({
            "schema_version": "0.1", "status": "answered", "answer": statement,
            "candidate": {"normalized_statement": statement,
                          "metric_or_aspect": "managed services revenue recognition",
                          "period": "current policy", "basis": "reported",
                          "cited_match_indexes": [0]}, "missing": [],
        }, {"schema_version": "0.1", "status": "answered"})
        executor, _draft, _verifier = self._executor(
            fixture, authority, draft_adapter=adapter)
        final = self._run_until(
            executor, admission,
            lambda item: item.get("research_status") == "candidate_staged")
        self.assertEqual(final["research_status"], "candidate_staged")
        self.assertEqual(fixture.store.connection.execute(
            "SELECT count(*) FROM mission_document_research_recovery_links"
        ).fetchone()[0], 1)
        self.assertEqual(fixture.store.connection.execute(
            "SELECT count(*) FROM "
            "mission_document_research_controlled_recovery_authorizations"
        ).fetchone()[0], 1)
        work, links = executor_module._effective_stage(
            authority, executor.scheduler, admission, 1,
            worker=executor.draft_worker)
        self.assertEqual(len(links), 1)
        self.assertEqual(executor.scheduler.formal_result(work["id"])["terminal_state"],
                         "succeeded")

    def test_policy_roll_rebinds_one_paid_retry_without_new_authorization(self):
        fixture, authority, args, _registration, _launcher = self._fixture()
        self._enable_recovery(fixture, maximum=2)
        admission = authority.admit_from_plan(**args)
        adapter = CountingFakeAdapter({"schema_version": "0.1", "status": "answered"})
        executor, _draft, _verifier = self._executor(
            fixture, authority, draft_adapter=adapter)
        self._run_until(
            executor, admission, lambda item: item.get("status") == "failed")
        current_execution = copy.deepcopy(admission["model_execution"])
        current_authority = copy.deepcopy(admission["model_authority"])
        current_authority["draft"]["routing_policy_hash"] = "a" * 64
        authority.model_execution_resolver = lambda: (
            current_execution, current_authority
        )

        result = executor.run_once(admission["id"])

        self.assertEqual(result["reason"], "automatic_bounded_contract_retry")
        authorizations = fixture.store.connection.execute(
            "SELECT * FROM "
            "mission_document_research_controlled_recovery_authorizations"
        ).fetchall()
        links = fixture.store.connection.execute(
            "SELECT record_json FROM mission_document_research_recovery_links"
        ).fetchall()
        rebinds = fixture.store.connection.execute(
            "SELECT record_json FROM "
            "mission_document_research_model_authority_epoch_rebinds"
        ).fetchall()
        self.assertEqual((len(authorizations), len(links), len(rebinds)), (1, 1, 1))
        link = json.loads(links[0]["record_json"])
        rebind = json.loads(rebinds[0]["record_json"])
        self.assertEqual(rebind["recovery_authorization_ref"],
                         link["failure_proof"]["authorization_ref"])
        self.assertEqual(rebind["recovery_authorization_hash"],
                         link["failure_proof"]["authorization_hash"])
        self.assertEqual(rebind["recovery_authorization_ref"],
                         authorizations[0]["authorization_id"])
        for field in executor_module._EPOCH_REBIND_BUDGET_FIELDS:
            self.assertLessEqual(
                rebind["current_budget"][field], rebind["authorized_budget"][field])

        expanded = copy.deepcopy(rebind["current_base_work_order"])
        expanded["budget"]["max_output_tokens"] += 1
        with self.assertRaisesRegex(
            MissionDocumentResearchExecutorError, "expands an authorized budget",
        ):
            executor_module._epoch_rebind_identity(
                authority.resolve_for_execution(admission["id"]), 1, link,
                executor.scheduler.work_order_authority(
                    link["recovery_work_order_ref"])["work_order"], expanded,
            )

    def test_succeeded_rebound_reverifies_link_and_authorization_on_next_roll(self):
        statement = "Managed services revenue is recognized over time."
        good = {
            "schema_version": "0.1", "status": "answered", "answer": statement,
            "candidate": {"normalized_statement": statement,
                          "metric_or_aspect": "revenue", "period": "current",
                          "basis": "reported", "cited_match_indexes": [0]},
            "missing": [],
        }
        for target, trigger, error in (
            ("mission_document_research_recovery_links",
             "mission_document_research_recovery_links_no_update",
             "recovery link authority drifted"),
            ("mission_document_research_controlled_recovery_authorizations",
             "mission_document_research_controlled_recovery_authorizations_no_update",
             "(automatic contract retry|paid recovery) authorization drifted"),
        ):
            with self.subTest(target=target):
                fixture, authority, args, _registration, _launcher = self._fixture()
                self._enable_recovery(fixture, maximum=2)
                admission = authority.admit_from_plan(**args)
                adapter = ContractRejectOnceAdapter(
                    good, {"schema_version": "0.1", "status": "answered"})
                executor, _draft, _verifier = self._executor(
                    fixture, authority, draft_adapter=adapter)
                self._run_until(
                    executor, admission,
                    lambda item: item.get("status") == "failed")
                current_execution = copy.deepcopy(admission["model_execution"])
                v2_authority = copy.deepcopy(admission["model_authority"])
                v2_authority["draft"]["routing_policy_hash"] = "e" * 64
                authority.model_execution_resolver = lambda: (
                    current_execution, v2_authority
                )
                admitted = executor.run_once(admission["id"])
                self.assertEqual(
                    admitted["reason"], "automatic_bounded_contract_retry")
                succeeded = executor.run_once(admission["id"])
                self.assertEqual(succeeded["status"], "succeeded")
                calls = adapter.calls
                v3_authority = copy.deepcopy(v2_authority)
                v3_authority["draft"]["routing_policy_hash"] = "f" * 64
                authority.model_execution_resolver = lambda: (
                    current_execution, v3_authority
                )
                fixture.store.connection.execute(f"DROP TRIGGER {trigger}")
                row = fixture.store.connection.execute(
                    f"SELECT rowid,record_json FROM {target} LIMIT 1"
                ).fetchone()
                wire = json.loads(row["record_json"])
                wire["tampered"] = True
                fixture.store.connection.execute(
                    f"UPDATE {target} SET record_json=? WHERE rowid=?",
                    (canonical_json(wire), row["rowid"]),
                )

                with self.assertRaisesRegex(
                    MissionDocumentResearchExecutorError, error,
                ):
                    executor.run_once(admission["id"])

                self.assertEqual(adapter.calls, calls)

    def test_automatic_contract_retry_stops_at_the_daily_cap_and_waits_for_utc_reset(self):
        """The cap is a spending bound, so its answer is tomorrow, not a person."""

        fixture, authority, args, _registration, _launcher = self._fixture()
        self._enable_recovery(fixture, maximum=2)
        admission = authority.admit_from_plan(**args)
        statement = "Managed services revenue is recognized over time."
        draft = ContractRejectOnceAdapter({
            "schema_version": "0.1", "status": "answered", "answer": statement,
            "candidate": {"normalized_statement": statement,
                          "metric_or_aspect": "managed services revenue recognition",
                          "period": "current policy", "basis": "reported",
                          "cited_match_indexes": [0]}, "missing": [],
        }, {"schema_version": "0.1", "status": "answered"})
        verifier = RouteBoundCountingFakeAdapter(
            {"schema_version": "0.1", "verdict": "pass"})
        executor, _draft, _verifier = self._executor(
            fixture, authority, draft_adapter=draft, verifier_adapter=verifier,
            max_automatic_contract_retries_per_day=1)
        # The draft stage spends the day's single automatic retry; the verifier
        # stage's own contract rejection then has to wait for the UTC reset.
        capped = self._run_until(
            executor, admission,
            lambda item: item.get("reason")
            == "automatic_contract_retry_day_cap_reached")
        self.assertEqual(capped["status"], "waiting")
        self.assertEqual(
            capped["retry_at"],
            (NOW.replace(hour=0, minute=0, second=0, microsecond=0)
             + timedelta(days=1)).isoformat(timespec="microseconds"))
        self.assertEqual(fixture.store.connection.execute(
            "SELECT count(*) FROM "
            "mission_document_research_controlled_recovery_authorizations"
        ).fetchone()[0], 1)
        self.assertEqual(fixture.store.connection.execute(
            "SELECT count(*) FROM mission_document_research_recovery_links"
        ).fetchone()[0], 1)
        observation = next(
            item for item in read_mission_document_research_observations(
                fixture.store.connection)
            if item["recovery"]["reason"]
            == "automatic_contract_retry_day_cap_reached")
        self.assertEqual(observation["recovery"]["day"], NOW.date().isoformat())
        self.assertIn("after the UTC day resets", observation["meaning"])
        # The same tick repeated is the same immutable row, and buys nothing.
        self.assertEqual(executor.run_once(admission["id"])["reason"],
                         "automatic_contract_retry_day_cap_reached")
        # A new UTC day releases the cap and the retry is taken automatically.
        fixture.harness.clock.value = NOW + timedelta(days=1)
        released = executor.run_once(admission["id"])
        self.assertEqual(released["reason"], "automatic_bounded_contract_retry")
        self.assertEqual(fixture.store.connection.execute(
            "SELECT count(*) FROM mission_document_research_recovery_links"
        ).fetchone()[0], 2)

    def test_owner_authorizes_one_exact_paid_contract_recovery_without_calling_model(self):
        """The owner door still opens -- on the state the automatic retry escalated."""

        fixture, authority, args, _registration, _launcher = self._fixture()
        self._enable_recovery(fixture, maximum=2)
        admission = authority.admit_from_plan(**args)
        adapter = CountingFakeAdapter({"schema_version": "0.1", "status": "answered"})
        executor, _draft, _verifier = self._executor(
            fixture, authority, draft_adapter=adapter,
        )
        self._run_until(
            executor, admission,
            lambda item: item.get("reason") == "contract_failed_after_automatic_retry")
        work, automatic = executor_module._effective_stage(
            authority, executor.scheduler, admission, 1,
            worker=executor.draft_worker)
        self.assertEqual(len(automatic), 1)
        self.assertEqual(automatic[0]["failure_proof"]["classification"],
                         "automation_bounded_contract_retry")
        formal = executor.scheduler.formal_result(work["id"])
        body = {
            "schema_version": "0.1",
            "actor_ref": "operator:owner-authorized-document-recovery",
            "admission_ref": admission["id"], "admission_hash": admission["content_hash"],
            "stage_ordinal": 2, "failed_work_order_ref": work["id"],
            "failed_work_order_hash": content_hash(work),
            "formal_result_ref": _formal_ref(formal),
            "formal_result_hash": _formal_hash(formal),
            "max_fresh_work_orders": 1,
            "max_cost_usd": work["budget"]["max_cost_usd"],
            "authorized_at": (NOW + timedelta(minutes=1)).isoformat(),
        }
        authorization = {
            **body,
            "id": "mission-document-paid-recovery-authorization:"
            + content_hash(body)[:32],
        }
        authorization["content_hash"] = content_hash(authorization)
        calls = adapter.calls
        result = executor.authorize_paid_contract_recovery(
            admission["id"], authorization)
        self.assertEqual(result["status"], "admitted")
        self.assertEqual(result["model_calls"], 0)
        self.assertEqual(adapter.calls, calls)
        self.assertEqual(executor.authorize_paid_contract_recovery(
            admission["id"], authorization), result)
        # The automation's row and link stay; the owner's are appended to them.
        self.assertEqual(fixture.store.connection.execute(
            "SELECT count(*) FROM mission_document_research_controlled_recovery_authorizations"
        ).fetchone()[0], 2)
        self.assertEqual(fixture.store.connection.execute(
            "SELECT count(*) FROM mission_document_research_recovery_links"
        ).fetchone()[0], 2)
        _owned, owner_links = executor_module._effective_stage(
            authority, executor.scheduler, admission, 1,
            worker=executor.draft_worker)
        self.assertEqual([item["failure_proof"]["classification"] for item in owner_links],
                         ["automation_bounded_contract_retry",
                          "owner_authorized_paid_contract_retry"])
        changed = dict(authorization)
        changed["max_cost_usd"] = authorization["max_cost_usd"] + 1
        changed["content_hash"] = content_hash(
            {key: value for key, value in changed.items() if key != "content_hash"})
        with self.assertRaises(MissionDocumentResearchExecutorError):
            executor.authorize_paid_contract_recovery(admission["id"], changed)

    def test_owner_paid_recovery_extends_one_prior_generic_recovery(self):
        fixture, authority, args, _registration, _launcher = self._fixture()
        self._enable_recovery(fixture, maximum=2)
        admission = authority.admit_from_plan(**args)
        adapter = CapacityOnceAdapter({"schema_version": "0.1", "status": "answered"})
        executor, _draft, _verifier = self._executor(
            fixture, authority, draft_adapter=adapter)
        # First failure is proved no-send.  The generic recovery is then sent,
        # paid, and rejected by the output contract, matching live EPAM 6a2b;
        # the lane's own bounded retry follows and is rejected the same way.
        self._run_until(
            executor, admission,
            lambda item: item.get("reason") == "contract_failed_after_automatic_retry")
        work, prior = executor_module._effective_stage(
            authority, executor.scheduler, admission, 1,
            worker=executor.draft_worker)
        self.assertEqual([item["failure_proof"]["classification"] for item in prior],
                         ["proved_zero_cost_no_send",
                          "automation_bounded_contract_retry"])
        formal = executor.scheduler.formal_result(work["id"])
        body = {
            "schema_version": "0.1",
            "actor_ref": "operator:owner-authorized-document-recovery",
            "admission_ref": admission["id"], "admission_hash": admission["content_hash"],
            "stage_ordinal": 2, "failed_work_order_ref": work["id"],
            "failed_work_order_hash": content_hash(work),
            "formal_result_ref": _formal_ref(formal),
            "formal_result_hash": _formal_hash(formal),
            "max_fresh_work_orders": 1,
            "max_cost_usd": work["budget"]["max_cost_usd"],
            "authorized_at": (NOW + timedelta(minutes=1)).isoformat(),
        }
        authorization = {**body, "id":
            "mission-document-paid-recovery-authorization:" + content_hash(body)[:32]}
        authorization["content_hash"] = content_hash(authorization)
        calls = adapter.calls
        result = executor.authorize_paid_contract_recovery(
            admission["id"], authorization)
        self.assertEqual(result["model_calls"], 0)
        self.assertEqual(adapter.calls, calls)
        _work, links = executor_module._effective_stage(
            authority, executor.scheduler, admission, 1,
            worker=executor.draft_worker)
        self.assertEqual(len(links), 3)
        self.assertEqual(links[2]["prior_recovery_link_ref"], links[1]["id"])
        self.assertEqual(links[2]["recovery_number"], 3)
        self.assertEqual(links[2]["failure_proof"]["classification"],
                         "owner_authorized_paid_contract_retry")

    def test_sealed_historical_no_send_proof_admits_one_verifier_retry(self):
        fixture, authority, args, _registration, _launcher = self._fixture()
        self._enable_recovery(fixture, maximum=2)
        admission = authority.admit_from_plan(**args)
        adapter = HistoricalControlsFailureAdapter({"unused": True})
        executor, _draft, _verifier_adapter = self._executor(
            fixture, authority, verifier_adapter=adapter)
        verifier = executor.verifier_worker
        while True:
            current = executor.run_once(admission["id"])
            if current["status"] == "failed":
                break
        works = effective_mission_document_work_orders(
            authority, executor.scheduler, admission["id"],
            draft_worker=executor.draft_worker, verifier_worker=verifier)
        work = works[2]
        formal = executor.scheduler.formal_result(work["id"])
        envelope = formal["result_envelope"]
        route = verifier.router.get_decision(
            envelope["metadata"]["route_decision_ref"])
        record = {
            "admission_ref": admission["id"], "admission_hash": admission["content_hash"],
            "work_order_ref": work["id"], "work_order_hash": content_hash(work),
            "attempt_number": formal["attempt_number"],
            "formal_result_record_ref": _formal_ref(formal),
            "result_envelope_ref": envelope["id"],
            "result_envelope_hash": formal["result_envelope_hash"],
            "route_decision_ref": route["id"],
            "route_decision_hash": route["content_hash"],
            "policy_version_ref": route["policy_version_ref"],
            "profile_version_ref": route["selected_profile_version_ref"],
            "broker_response_hash": envelope["metadata"]["broker_response_hash"],
            "classification": "provider_call_definitely_not_sent",
            "basis": "historical_required_controls_pre_dispatch_branch",
            "dispatch_proof": None,
            "usage_telemetry": "all_token_fields_null_cost_unavailable",
        }
        receipt = {
            "schema_version": "historical-required-controls-nosend-sealed-proof:0.1",
            "status": "verified_read_only",
            "classification": "provider_call_definitely_not_sent",
            "historical_broker": {
                "git_commit": "a" * 40, "source_path": "/reviewed/broker.mjs",
                "source_sha256": "7" * 64,
                "snapshot_path": "/reviewed/snapshot.json",
                "snapshot_sha256": "8" * 64, "snapshot_tree_sha256": "6" * 64,
                "pre_dispatch_order_verified": True,
            },
            "input_sha256": "5" * 64, "live_mutation": False,
            "records": [record, {"admission_ref": "other:1"},
                        {"admission_ref": "other:2"}],
            "model_calls": 0, "scheduler_writes": 0,
        }
        receipt["content_hash"] = content_hash(receipt)
        raw = canonical_json(receipt).encode()
        path = fixture.state / "historical-nosend-proof.json"
        path.write_bytes(raw)
        path.chmod(0o600)
        with patch.object(executor_module, "_HISTORICAL_NOSEND_RECEIPT_SHA256",
                          hashlib.sha256(raw).hexdigest()), patch.object(
            executor_module, "_HISTORICAL_NOSEND_CONTENT_HASH",
            receipt["content_hash"]), patch.object(
            executor_module, "_HISTORICAL_BROKER_SOURCE_SHA256", "7" * 64), patch.object(
            executor_module, "_HISTORICAL_BROKER_SNAPSHOT_SHA256", "8" * 64):
            authorized = executor.authorize_historical_no_send_recovery(
                admission["id"], path, (NOW - timedelta(minutes=1)).isoformat())
            self.assertEqual(authorized["model_calls"], 0)
            self.assertEqual(executor.authorize_historical_no_send_recovery(
                admission["id"], path, (NOW - timedelta(minutes=1)).isoformat()),
                authorized)
            calls = adapter.calls
            admitted = executor.run_once(admission["id"])
            self.assertEqual(admitted["reason"], "fresh_work_recovery")
            self.assertEqual(adapter.calls, calls)
            replayed = executor.authorize_historical_no_send_recovery(
                admission["id"], path, (NOW - timedelta(minutes=1)).isoformat())
            self.assertEqual(replayed["work_order_ref"], admitted["work_order_ref"])
            raw_tampered = raw.replace(b"verified_read_only", b"verified_read_onlX")
            other = fixture.state / "tampered-proof.json"
            other.write_bytes(raw_tampered); other.chmod(0o600)
            with self.assertRaises(MissionDocumentResearchExecutorError):
                executor.authorize_historical_no_send_recovery(
                    admission["id"], other, (NOW + timedelta(minutes=2)).isoformat())

    def test_paid_contract_diagnostic_usage_mismatch_degrades_to_unknown_send(self):
        fixture, authority, args, _registration, _launcher = self._fixture()
        self._enable_recovery(fixture, maximum=2)
        admission = authority.admit_from_plan(**args)
        adapter = CountingFakeAdapter({"schema_version": "0.1", "status": "answered"})
        executor, _draft, _verifier = self._executor(
            fixture, authority, draft_adapter=adapter,
        )
        while True:
            current = executor.run_once(admission["id"])
            if current["status"] == "failed":
                break
        observability = executor.draft_worker.observability
        latest_usage = observability.latest_usage

        def mismatched(invocation_ref):
            return {**latest_usage(invocation_ref), "work_order_ref": "work:foreign"}

        with patch.object(observability, "latest_usage", side_effect=mismatched):
            result = executor.run_once(admission["id"])
        # The paid-contract proof degrades, so the paid door stays shut.  What
        # is left is an unproved send, and that has its own bounded door: one
        # retry, never the proved-paid replay.
        self.assertEqual(result["reason"], "automatic_bounded_unproved_send_retry")
        observation = read_mission_document_research_observations(
            fixture.store.connection)[0]
        self.assertEqual(observation["recovery"]["proof"]["classification"],
                         "unproved_send_state")
        authorization = json.loads(fixture.store.connection.execute(
            "SELECT record_json FROM "
            "mission_document_research_controlled_recovery_authorizations"
        ).fetchone()[0])
        self.assertEqual(authorization["kind"],
                         "automation_bounded_unproved_send_retry")

    def test_foreign_self_consistent_model_proof_is_not_formal_model_authority(self):
        fixture, authority, args, _registration, _launcher = self._fixture()
        admission = authority.admit_from_plan(**args)
        executor, _draft, verifier = self._executor(fixture, authority)
        for _ in range(3):
            executor.run_once(admission["id"])
        work = executor._derive_work(admission, executor._blueprints(admission), 1)
        claim = executor.scheduler.claim("worker:foreign", work_order_id=work["id"])
        candidate = {
            "schema_version": "0.1", "status": "answered", "answer": "forged",
            "candidate": {"normalized_statement": "forged", "metric_or_aspect": "revenue",
                          "period": "current", "basis": "reported",
                          "cited_match_indexes": [0]}, "missing": [],
        }
        proof = {
            "schema_version": "0.1", "id": "document-research-model-proof:foreign",
            "stage": "qualitative_model_draft", "work_order_ref": work["id"],
            "work_order_hash": content_hash(work), "prompt_hash": content_hash(work["question"]),
            "request_binding_hash": work["metadata"]["model_request_binding_hash"],
            "route_decision_ref": "route-decision:foreign",
            "model_invocation_ref": "invocation:foreign", "model_family": "foreign",
            "output": candidate, "output_hash": content_hash(candidate),
        }
        proof["content_hash"] = content_hash(proof)
        envelope = ResultEnvelope(
            schema_version="0.1", id="result-envelope:foreign",
            created_at=NOW.isoformat(), work_order_ref=work["id"],
            invocation_ref="invocation:foreign", status="succeeded", outputs=proof,
            actual_side_effects=(), usage_refs=(), artifact_refs=(), error=None,
            metadata={"route_decision_ref": "route-decision:foreign"},
        ).to_dict()
        executor.scheduler.complete(
            work["id"], claim["attempt"]["attempt_number"], "worker:foreign",
            claim["lease_token"], envelope, idempotency_key="foreign:model-proof",
            result_envelope_hash=content_hash(envelope))
        with self.assertRaisesRegex(
            MissionDocumentResearchExecutorError, "model formal authority is invalid"
        ):
            executor.run_once(admission["id"])
        self.assertEqual(verifier.calls, 0)
        self.assertEqual(fixture.budget.connection.execute(
            "SELECT count(*) FROM thesis_impact_day_admissions WHERE phase='verification'"
        ).fetchone()[0], 0)

    def test_observation_and_recovery_tables_refuse_untrusted_sql_writes(self):
        fixture, authority, args, _registration, _launcher = self._fixture()
        admission = authority.admit_from_plan(**args)
        executor, _draft, _verifier = self._executor(fixture, authority)
        with self.assertRaises(sqlite3.DatabaseError):
            fixture.store.connection.execute(
                "INSERT INTO mission_document_research_observations VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                ("forged", admission["id"], admission["content_hash"],
                 admission["mission_version_ref"], admission["company_ref"],
                 admission["inquiry_ref"], admission["inquiry_hash"], "query_miss",
                 "{}", "0" * 64, NOW.isoformat()),
            )
        fixture.store.connection.rollback()
        self.assertFalse(hasattr(__import__(
            "dalton_core.document_research_qualitative", fromlist=["stage_candidate"]
        ), "stage_candidate"))

    def test_recovery_deadline_stops_without_enqueuing_fresh_work(self):
        fixture, authority, args, _registration, _launcher = self._fixture()
        self._enable_recovery(fixture, maximum=2, elapsed=1)
        admission = authority.admit_from_plan(**args)
        adapter = CapacityOnceAdapter({"unused": True})
        executor, _draft, _verifier = self._executor(
            fixture, authority, draft_adapter=adapter)
        while True:
            result = executor.run_once(admission["id"])
            if result["status"] == "failed":
                break
        fixture.harness.clock.value += timedelta(seconds=2)
        stopped = executor.run_once(admission["id"])
        self.assertEqual(stopped["reason"], "fresh_work_recovery_deadline_exceeded")
        self.assertEqual(fixture.store.connection.execute(
            "SELECT count(*) FROM mission_document_research_recovery_links").fetchone()[0], 0)

    def test_recovery_fresh_work_limit_stops_repeated_capacity_failure(self):
        fixture, authority, args, _registration, _launcher = self._fixture()
        self._enable_recovery(fixture, maximum=1)
        admission = authority.admit_from_plan(**args)
        executor, adapter, _verifier = self._executor(
            fixture, authority, draft_adapter=AlwaysCapacityAdapter({"unused": True}))
        stopped = None
        for _ in range(20):
            result = executor.run_once(admission["id"])
            if result.get("reason") == "fresh_work_recovery_exhausted":
                stopped = result
                break
        self.assertIsNotNone(stopped)
        self.assertEqual(fixture.store.connection.execute(
            "SELECT count(*) FROM mission_document_research_recovery_links").fetchone()[0], 1)
        self.assertEqual(adapter.calls, 6)

    def test_atomic_day_budget_refusal_waits_for_refill_then_uses_fresh_work(self):
        fixture, authority, args, _registration, _launcher = self._fixture()
        self._enable_recovery(fixture, maximum=1, elapsed=7200)
        admission = authority.admit_from_plan(**args)
        other = fixture.budget.admit(
            policy_version_id="budget-policy:mission-annual:1",
            day=NOW.date().isoformat(), work_order_ref="work:other-settled-consumer",
            attempt_number=1, phase="assessment", route_decision_ref="route:other",
            reserved_micros=9_500_000,
        )
        fixture.budget.settle(other["admission_id"], actual_micros=9_500_000)
        executor, draft, verifier = self._executor(fixture, authority)
        for _ in range(4):
            failed = executor.run_once(admission["id"])
        self.assertEqual(failed["status"], "failed")
        waiting = executor.run_once(admission["id"])
        self.assertEqual(waiting["reason"], "fresh_work_recovery_backoff")
        self.assertEqual((draft.calls, verifier.calls), (0, 0))
        observation = read_mission_document_research_observations(
            fixture.store.connection, mission_version_ref=fixture.mission["id"],
        )[0]
        self.assertEqual(
            observation["recovery"]["deadline"],
            "2026-09-12T02:00:00.000000+00:00",
        )
        fixture.harness.clock.value += timedelta(hours=12)
        recovered = executor.run_once(admission["id"])
        self.assertEqual(recovered["reason"], "fresh_work_recovery")
        final = None
        for _ in range(10):
            final = executor.run_once(admission["id"])
            if final.get("research_status") == "candidate_staged":
                break
        self.assertEqual(final["research_status"], "candidate_staged")
        self.assertEqual((draft.calls, verifier.calls), (1, 1))
        link = json.loads(fixture.store.connection.execute(
            "SELECT record_json FROM mission_document_research_recovery_links"
        ).fetchone()[0])
        self.assertEqual(link["failure_proof"]["classification"],
                         "atomic_day_budget_refusal")

    def test_governance_roll_reuses_succeeded_atomic_day_recovery(self):
        (fixture, authority, admission, executor, draft, _verifier,
         recovery) = self._ready_atomic_day_recovery()
        succeeded = executor.run_once(admission["id"])
        self.assertEqual(succeeded["status"], "succeeded")
        self.assertEqual(succeeded["work_order_ref"], recovery["work_order_ref"])
        calls = draft.calls
        rolled = self._roll_mission(fixture, budget={
            **fixture.mission["budget"], "max_daily_cost_usd": 9.0,
        })
        self.assertNotEqual(rolled["id"], fixture.mission["id"])

        inspected = executor.inspect_model_authority_epoch_recovery(admission["id"])

        stage = next(item for item in inspected["stages"] if item["stage"] == "draft")
        self.assertEqual(stage["status"], "succeeded")
        self.assertEqual(stage["work_order_ref"], recovery["work_order_ref"])
        self.assertEqual(draft.calls, calls)

    def test_atomic_day_recovery_created_after_governance_roll_uses_current_binding(self):
        fixture, authority, args, _registration, _launcher = self._fixture()
        self._enable_recovery(fixture, maximum=1, elapsed=7200)
        admission = authority.admit_from_plan(**args)
        self._roll_mission(fixture, budget={
            **fixture.mission["budget"], "max_daily_cost_usd": 9.0,
        })
        other = fixture.budget.admit(
            policy_version_id="budget-policy:mission-annual:1",
            day=NOW.date().isoformat(), work_order_ref="work:other-after-roll",
            attempt_number=1, phase="assessment", route_decision_ref="route:other",
            reserved_micros=9_500_000,
        )
        fixture.budget.settle(other["admission_id"], actual_micros=9_500_000)
        executor, draft, _verifier = self._executor(fixture, authority)
        for _ in range(4):
            failed = executor.run_once(admission["id"])
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(
            executor.run_once(admission["id"])["reason"],
            "fresh_work_recovery_backoff",
        )
        fixture.harness.clock.value += timedelta(hours=12)
        recovery = executor.run_once(admission["id"])
        self.assertEqual(recovery["reason"], "fresh_work_recovery")

        succeeded = executor.run_once(admission["id"])
        inspected = executor.inspect_model_authority_epoch_recovery(admission["id"])

        self.assertEqual(succeeded["status"], "succeeded")
        stage = next(item for item in inspected["stages"] if item["stage"] == "draft")
        self.assertEqual(stage["status"], "succeeded")
        self.assertEqual(stage["work_order_ref"], recovery["work_order_ref"])
        self.assertEqual(draft.calls, 1)

    def test_governance_roll_preserves_failed_atomic_day_recovery_typed_door(self):
        adapter = CountingFakeAdapter({"schema_version": "0.1", "status": "answered"})
        (fixture, authority, admission, executor, draft, _verifier,
         recovery) = self._ready_atomic_day_recovery(draft_adapter=adapter)
        failed = self._run_until(
            executor, admission, lambda item: item.get("status") == "failed")
        self.assertEqual(failed["work_order_ref"], recovery["work_order_ref"])
        calls = draft.calls
        self._roll_mission(fixture, budget={
            **fixture.mission["budget"], "max_daily_cost_usd": 9.0,
        })

        inspected = executor.inspect_model_authority_epoch_recovery(admission["id"])

        stage = next(item for item in inspected["stages"] if item["stage"] == "draft")
        self.assertEqual(stage["status"], "historical_failure")
        self.assertEqual(stage["reason"], "paid_send_output_contract_failed")
        self.assertEqual(stage["action"], "existing_paid_contract_recovery_door")
        self.assertEqual(stage["work_order_ref"], recovery["work_order_ref"])
        self.assertEqual(draft.calls, calls)

    def test_historical_atomic_day_binding_tampering_is_rejected(self):
        for field in ("outer_budget", "pool", "hash"):
            with self.subTest(field=field):
                (fixture, authority, admission, executor, _draft, _verifier,
                 _recovery) = self._ready_atomic_day_recovery()
                self._roll_mission(fixture, budget={
                    **fixture.mission["budget"], "max_daily_cost_usd": 9.0,
                })
                link = json.loads(fixture.store.connection.execute(
                    "SELECT record_json FROM mission_document_research_recovery_links"
                ).fetchone()[0])
                rejection_ref = link["failure_proof"]["budget_authority_ref"]
                row = fixture.budget.connection.execute(
                    "SELECT record_json FROM thesis_impact_day_rejections "
                    "WHERE rejection_id=?", (rejection_ref,),
                ).fetchone()
                refusal = json.loads(row["record_json"])
                if field == "outer_budget":
                    refusal["mission_binding"]["outer_budget"] = {
                        **refusal["mission_binding"]["outer_budget"],
                        "max_daily_paid_calls": (
                            refusal["mission_binding"]["outer_budget"]
                            ["max_daily_paid_calls"] + 1),
                    }
                elif field == "pool":
                    refusal["mission_binding"]["pool"] = "coverage"
                if field != "hash":
                    body = dict(refusal)
                    body.pop("content_hash")
                    refusal["content_hash"] = content_hash(body)
                fixture.budget.connection.execute(
                    "DROP TRIGGER thesis_impact_day_rejections_no_update")
                fixture.budget.connection.execute(
                    "UPDATE thesis_impact_day_rejections SET record_json=?,content_hash=? "
                    "WHERE rejection_id=?",
                    (canonical_json(refusal),
                     ("0" * 64 if field == "hash" else refusal["content_hash"]),
                     rejection_ref),
                )

                inspected = executor.inspect_model_authority_epoch_recovery(
                    admission["id"])
                stage = next(
                    item for item in inspected["stages"] if item["stage"] == "draft")
                self.assertEqual(stage["status"], "blocked")
                self.assertEqual(stage["reason"], "recovery refusal proof drifted")

    def _completed_draft_then_roll(self):
        """A draft paid under mission v1, then a signing rolls the envelope."""

        fixture, authority, args, _registration, _launcher = self._fixture()
        admission = authority.admit_from_plan(**args)
        executor, draft, verifier = self._executor(fixture, authority)
        draft_done = self._run_until(
            executor, admission,
            lambda item: item.get("status") == "succeeded"
            and item.get("stage") == "qualitative_model_draft")
        self.assertEqual((draft.calls, verifier.calls), (1, 0))
        rolled = self._roll_mission(fixture, budget={
            **fixture.mission["budget"], "max_daily_cost_usd": 9.0,
            "max_daily_paid_calls": 7})
        self.assertNotEqual(rolled["id"], fixture.mission["id"])
        return fixture, authority, admission, executor, draft, verifier, draft_done

    def _draft_budget_binding(self, fixture, work_ref):
        return fixture.budget.connection.execute(
            "SELECT b.admission_id,b.record_json FROM model_mission_budget_bindings b "
            "JOIN thesis_impact_day_admissions a ON a.admission_id=b.admission_id "
            "WHERE a.work_order_ref=?", (work_ref,),
        ).fetchone()

    def test_completed_stage_bound_under_old_mission_version_reenters_after_roll(self):
        """Live 2026-09-25: every owner re-entry died on ``binding drifted``.

        6a2bcd's draft was paid under mission v14 / policy-17; the current
        envelope is policy-18 / governing mission v26.  The completed stage
        keeps the envelope it ran under, and the admission finishes without
        buying the draft again.
        """

        (fixture, authority, admission, executor, draft, verifier,
         draft_done) = self._completed_draft_then_roll()
        row = self._draft_budget_binding(fixture, draft_done["work_order_ref"])
        recorded = json.loads(row["record_json"])
        current = executor_module._expected_budget_binding(authority, admission, 1)
        # The envelope really did roll under the completed stage ...
        self.assertNotEqual(canonical_json(recorded), canonical_json(current))
        self.assertEqual(recorded["mission_version_ref"], current["mission_version_ref"])
        # ... and the completed stage is still exact authority.
        work = fixture.harness.scheduler().work_order_authority(
            draft_done["work_order_ref"])["work_order"]
        formal = fixture.harness.scheduler().formal_result(work["id"])
        proof = exact_mission_document_model_execution_authority(
            work, formal, executor.draft_worker)
        self.assertEqual(proof["execution_proof"]["budget_mission_binding_hash"],
                         content_hash(recorded))
        finished = self._run_until(
            executor, admission, lambda item: item.get("status") == "complete")
        self.assertEqual(finished["research_status"], "candidate_staged")
        self.assertEqual((draft.calls, verifier.calls), (1, 1))

    def test_binding_written_before_pool_enforcement_existed_is_still_exact(self):
        """Live 3953fd12 / 49d92fa7: drafts paid on 2026-09-11 carry no
        ``pool_enforcement``; the envelope gained that field afterwards."""

        (fixture, authority, admission, executor, _draft, _verifier,
         draft_done) = self._completed_draft_then_roll()
        older = json.loads(self._draft_budget_binding(
            fixture, draft_done["work_order_ref"])["record_json"])
        current = executor_module._expected_budget_binding(authority, admission, 1)
        self.assertNotIn("pool_enforcement", older)
        later = {**current, "pool_enforcement": "off"}
        authentic = executor_module._historical_budget_binding_authentic
        self.assertTrue(authentic(authority, later, older))
        # A field the envelope never had is not an older shape; it is refused.
        self.assertFalse(authentic(authority, later, {**older, "extra": 1}))
        self.assertFalse(authentic(authority, current, {**older, "pool_enforcement": "on"}))

    def test_rolled_binding_tampering_is_still_refused(self):
        tampers = {
            "mission_version_hash": lambda b: {**b, "mission_version_hash": "0" * 64},
            "other_mission": lambda b: {
                **b, "mission_ref": "coverage-mission:someone-else",
                "mission_version_ref": "coverage-mission-version:someone-else:1"},
            "policy_hash": lambda b: {**b, "outer_budget": {
                **b["outer_budget"], "governance_policy_version_hash": "1" * 64}},
            "mandate_version": lambda b: {**b, "outer_budget": {
                **b["outer_budget"], "mandate_version_ref": "mandate-version:forged:9"}},
            "caps_above_outer": lambda b: {
                **b, "max_daily_cost_micros": b["outer_budget"]["max_daily_cost_micros"] + 1},
            "pool": lambda b: {**b, "pool": "coverage"},
        }
        for name, tamper in tampers.items():
            with self.subTest(tamper=name):
                (fixture, authority, admission, executor, draft, verifier,
                 draft_done) = self._completed_draft_then_roll()
                row = self._draft_budget_binding(fixture, draft_done["work_order_ref"])
                forged = tamper(json.loads(row["record_json"]))
                connection = fixture.budget.connection
                connection.execute("DROP TRIGGER model_mission_budget_no_update")
                connection.execute(
                    "UPDATE model_mission_budget_bindings SET mission_ref=?,record_json=? "
                    "WHERE admission_id=?",
                    (forged["mission_ref"], canonical_json(forged), row["admission_id"]))
                connection.commit()
                work = fixture.harness.scheduler().work_order_authority(
                    draft_done["work_order_ref"])["work_order"]
                formal = fixture.harness.scheduler().formal_result(work["id"])
                with self.assertRaisesRegex(MissionDocumentResearchExecutorError,
                                            "model budget binding drifted"):
                    exact_mission_document_model_execution_authority(
                        work, formal, executor.draft_worker)
                with self.assertRaisesRegex(MissionDocumentResearchExecutorError,
                                            "model budget binding drifted"):
                    self._run_until(executor, admission,
                                    lambda item: item.get("status") == "complete")
                self.assertEqual((draft.calls, verifier.calls), (1, 0))

    def test_contract_retry_recovery_work_survives_an_envelope_roll(self):
        """Live ws-7d ...e5967f6d: ``historical recovery link authority drifted``.

        A RecoveryWork bought by the automatic contract retry succeeded under
        the old envelope.  Its link is intact; it is simply not an atomic
        day-budget recovery, which is no reason to refuse it.
        """

        fixture, authority, args, _registration, _launcher = self._fixture()
        self._enable_recovery(fixture, maximum=2)
        admission = authority.admit_from_plan(**args)
        executor, draft, verifier = self._executor(
            fixture, authority, draft_adapter=ContractRejectOnceAdapter({
                "schema_version": "0.1", "status": "answered",
                "answer": "Managed services revenue is recognized over time.",
                "candidate": {
                    "normalized_statement":
                        "Managed services revenue is recognized over time.",
                    "metric_or_aspect": "managed services revenue recognition",
                    "period": "current policy", "basis": "reported",
                    "cited_match_indexes": [0]}, "missing": []},
                {"schema_version": "0.1", "status": "answered"}))
        recovered = self._run_until(
            executor, admission,
            lambda item: item.get("status") == "succeeded"
            and item.get("stage") == "qualitative_model_draft")
        self.assertTrue(recovered["work_order_ref"].startswith(
            "work:mission-document-recovery-"))
        link = json.loads(fixture.store.connection.execute(
            "SELECT record_json FROM mission_document_research_recovery_links"
        ).fetchone()[0])
        self.assertEqual(link["failure_proof"]["classification"],
                         "automation_bounded_contract_retry")
        calls = draft.calls
        self._roll_mission(fixture, budget={
            **fixture.mission["budget"], "max_daily_cost_usd": 9.0})
        finished = self._run_until(
            executor, admission, lambda item: item.get("status") == "complete")
        self.assertEqual(finished["research_status"], "candidate_staged")
        self.assertEqual(draft.calls, calls)

    def test_pre_change_contract_hold_is_picked_up_by_the_automatic_retry(self):
        """The nineteen live holds: recorded before the retry existed, not spent."""

        fixture, authority, args, _registration, _launcher = self._fixture()
        self._enable_recovery(fixture, maximum=2)
        admission = authority.admit_from_plan(**args)
        adapter = CountingFakeAdapter({"schema_version": "0.1", "status": "answered"})
        executor, _draft, _verifier = self._executor(
            fixture, authority, draft_adapter=adapter)
        while True:
            current = executor.run_once(admission["id"])
            if current["status"] == "failed":
                break
        work = executor._derive_work(admission, executor._blueprints(admission), 1)
        formal = executor.scheduler.formal_result(work["id"])
        paid = executor._paid_contract_failure_proof(work, formal, 1)
        self.assertIsNotNone(paid)
        # Exactly what the old executor wrote and then waited on forever.
        executor._recovery_observation(admission, work, formal, 1, {
            "status": "stopped", "reason": "paid_send_output_contract_failed",
            "eligible": False, "used_fresh_work_orders": 0,
            "max_fresh_work_orders": 2, "retry_at": None,
            "deadline": (NOW + timedelta(hours=1)).isoformat(
                timespec="microseconds"),
            "proof": paid,
        })
        lane = MissionDocumentResearchCoordinator(
            store=fixture.store, launcher=None, clock=fixture.harness.clock,
        )
        self.assertEqual(lane._typed_recovery_state(admission, work["id"]), {
            "action": "resume", "reason": "automatic_contract_retry_available",
            "work_order_ref": work["id"],
        })
        # Re-entering spends the automatic retry rather than asking a person.
        self.assertEqual(executor.run_once(admission["id"])["reason"],
                         "automatic_bounded_contract_retry")
        self.assertEqual(fixture.store.connection.execute(
            "SELECT count(*) FROM mission_document_research_recovery_links"
        ).fetchone()[0], 1)
        # The newer row supersedes the legacy verdict for the same Work.
        self.assertEqual(
            lane._typed_recovery_state(admission, work["id"])["reason"],
            "controlled_contract_retry_admitted")

    def test_legacy_equal_deadline_day_budget_reopens_only_for_bounded_window(self):
        fixture, authority, args, _registration, _launcher = self._fixture()
        self._enable_recovery(fixture, maximum=1, elapsed=43200)
        admission = authority.admit_from_plan(**args)
        consumed = fixture.budget.admit(
            policy_version_id="budget-policy:mission-annual:1",
            day=NOW.date().isoformat(), work_order_ref="work:legacy-budget-consumer",
            attempt_number=1, phase="assessment", route_decision_ref="route:legacy",
            reserved_micros=9_500_000,
        )
        fixture.budget.settle(consumed["admission_id"], actual_micros=9_500_000)
        executor, draft, verifier = self._executor(fixture, authority)
        for _ in range(4):
            failed = executor.run_once(admission["id"])
        self.assertEqual(failed["status"], "failed")
        work = executor._derive_work(admission, executor._blueprints(admission), 1)
        formal = executor.scheduler.formal_result(work["id"])
        proof = executor._safe_failure_proof(admission, work, formal, 1)
        executor._recovery_observation(admission, work, formal, 1, {
            "status": "stopped", "reason": "fresh_work_recovery_deadline_exceeded",
            "eligible": False, "used_fresh_work_orders": 0,
            "max_fresh_work_orders": 1,
            "retry_at": "2026-09-12T00:00:00.000000+00:00",
            # The old executor used eligible_at >= deadline, so equality was
            # also persisted as stopped even though midnight was the first
            # instant at which the daily authority could admit a fresh Work.
            "deadline": "2026-09-12T00:00:00.000000+00:00",
            "proof": proof,
        })
        lane = MissionDocumentResearchCoordinator(
            store=fixture.store, launcher=None, clock=fixture.harness.clock,
        )
        fixture.harness.clock.value = datetime(
            2026, 9, 11, 23, 59, 59, tzinfo=timezone.utc,
        )
        self.assertEqual(
            lane._typed_recovery_state(admission, work["id"])["action"], "waiting",
        )
        fixture.harness.clock.value = datetime(
            2026, 9, 12, 0, 0, tzinfo=timezone.utc,
        )
        self.assertEqual(
            lane._typed_recovery_state(admission, work["id"])["action"], "resume",
        )
        fixture.harness.clock.value = datetime(
            2026, 9, 12, 11, 59, 59, tzinfo=timezone.utc,
        )
        self.assertEqual(
            lane._typed_recovery_state(admission, work["id"])["action"], "resume",
        )
        fixture.harness.clock.value = datetime(
            2026, 9, 12, 12, 1, tzinfo=timezone.utc,
        )
        expired = lane._typed_recovery_state(admission, work["id"])
        self.assertEqual(expired, {
            "action": "recovery_required",
            "reason": "fresh_work_recovery_day_window_exceeded",
            "work_order_ref": work["id"],
        })
        # A direct/restarted child at the same late instant converges to a new
        # immutable terminal observation instead of conflicting with the old
        # stopped record or creating a fresh Work.
        for _ in range(2):
            stopped = executor.run_once(admission["id"])
            self.assertEqual(
                stopped["reason"], "fresh_work_recovery_day_window_exceeded",
            )
        self.assertEqual((draft.calls, verifier.calls), (0, 0))
        observations = read_mission_document_research_observations(
            fixture.store.connection, mission_version_ref=fixture.mission["id"],
        )
        self.assertEqual(len(observations), 2)
        self.assertEqual(
            lane._typed_recovery_state(admission, work["id"])["action"],
            "recovery_required",
        )

    def test_daily_budget_window_is_anchored_once_across_recovery_chain(self):
        policy = {"max_fresh_work_orders": 3, "retry_backoff_seconds": 0,
                  "max_elapsed_seconds": 7200}
        first = {
            "classification": "atomic_day_budget_refusal",
            "failed_at": "2026-09-11T20:35:12.000000+00:00",
            "refusal_day": "2026-09-11",
        }
        later = {
            "classification": "atomic_day_budget_refusal",
            "failed_at": "2026-09-12T01:00:00.000000+00:00",
            "refusal_day": "2026-09-12",
        }
        from dalton_core.mission_document_research_executor import (
            _day_budget_recovery_deadline,
        )
        deadline = _day_budget_recovery_deadline(
            started=datetime(2026, 9, 11, 20, 35, 12, tzinfo=timezone.utc),
            policy=policy, proof=later,
            links=[{"failure_proof": first}],
        )
        self.assertEqual(
            deadline,
            datetime(2026, 9, 12, 2, 0, tzinfo=timezone.utc),
        )

    def test_adapter_proved_pre_send_failure_recovers_with_new_budget_identity(self):
        fixture, authority, args, _registration, _launcher = self._fixture()
        self._enable_recovery(fixture, maximum=1)
        admission = authority.admit_from_plan(**args)
        statement = "Managed services revenue is recognized over time."
        adapter = DefinitelyNotSentFirstWorkAdapter({
            "schema_version": "0.1", "status": "answered", "answer": statement,
            "candidate": {"normalized_statement": statement, "metric_or_aspect": "revenue",
                          "period": "current", "basis": "reported",
                          "cited_match_indexes": [0]}, "missing": [],
        })
        executor, _adapter, verifier = self._executor(
            fixture, authority, draft_adapter=adapter)
        for _ in range(3):
            executor.run_once(admission["id"])
        original = executor._derive_work(admission, executor._blueprints(admission), 1)
        admit = executor.draft_worker._before_model_call
        executor.draft_worker._before_model_call = (
            lambda work, route, profile, replayed:
            None if work.id == original["id"] else admit(work, route, profile, replayed)
        )
        final = None
        for _ in range(16):
            final = executor.run_once(admission["id"])
            if final.get("research_status") == "candidate_staged":
                break
        self.assertEqual(final["research_status"], "candidate_staged")
        self.assertEqual((adapter.calls, verifier.calls), (4, 1))
        self.assertEqual(fixture.budget.connection.execute(
            "SELECT count(*) FROM thesis_impact_day_admissions WHERE work_order_ref=?",
            (original["id"],)).fetchone()[0], 0)
        self.assertEqual(fixture.budget.connection.execute(
            "SELECT count(*) FROM thesis_impact_day_admissions").fetchone()[0], 2)
        link = json.loads(fixture.store.connection.execute(
            "SELECT record_json FROM mission_document_research_recovery_links"
        ).fetchone()[0])
        self.assertEqual(link["failure_proof"]["classification"],
                         "adapter_proved_definitely_not_sent")

    def test_foreign_failed_claim_cannot_forge_no_send_recovery(self):
        fixture, authority, args, _registration, _launcher = self._fixture()
        self._enable_recovery(fixture)
        admission = authority.admit_from_plan(**args)
        executor, draft, _verifier = self._executor(fixture, authority)
        for _ in range(3):
            executor.run_once(admission["id"])
        work = executor._derive_work(admission, executor._blueprints(admission), 1)
        claim = executor.scheduler.claim("worker:foreign", work_order_id=work["id"])
        receipt = {
            "schema_version": "0.1", "kind": "typed_transport_exception",
            "authority": "mission-document-model-worker", "state": "definitely_not_sent",
            "work_order_ref": work["id"], "route_decision_ref": "route:foreign",
            "transport_error_type": "BrokerDefinitelyNotSent",
        }
        receipt["content_hash"] = content_hash(receipt)
        envelope = ResultEnvelope(
            schema_version="0.1", id="result-envelope:foreign-no-send",
            created_at=NOW.isoformat(), work_order_ref=work["id"],
            invocation_ref="execution:foreign", status="failed", outputs={},
            actual_side_effects=(), usage_refs=(), artifact_refs=(),
            error={"code": "MODEL_ADAPTER_UNAVAILABLE", "message": "forged"},
            metadata={"route_decision_ref": "route:foreign",
                      "mission_document_no_send": receipt},
        ).to_dict()
        executor.scheduler.complete(
            work["id"], claim["attempt"]["attempt_number"], "worker:foreign",
            claim["lease_token"], envelope, idempotency_key="foreign:no-send",
            result_envelope_hash=content_hash(envelope))
        with self.assertRaisesRegex(
            MissionDocumentResearchExecutorError, "execution authority is foreign"
        ):
            executor.run_once(admission["id"])
        self.assertEqual(draft.calls, 0)
        self.assertEqual(fixture.store.connection.execute(
            "SELECT count(*) FROM mission_document_research_recovery_links").fetchone()[0], 0)

    def test_self_consistent_model_completion_with_unsettled_budget_cannot_advance(self):
        fixture, authority, args, _registration, _launcher = self._fixture()
        admission = authority.admit_from_plan(**args)
        executor, draft_adapter, verifier = self._executor(fixture, authority)
        for _ in range(3):
            executor.run_once(admission["id"])
        work_wire = executor._derive_work(admission, executor._blueprints(admission), 1)
        work = WorkOrder.from_dict(work_wire)
        claim = executor.scheduler.claim(
            executor.draft_worker.worker_ref, work_order_id=work.id)
        estimated_input = executor.draft_worker.token_counter(work.question)
        route = fixture.router.route(
            work, attempt_number=claim["attempt"]["attempt_number"],
            capability="research",
            policy_version_ref=fixture.draft_policy["policy_version_ref"],
            credential_slot_refs=[fixture.draft_profile["credential_slot_ref"]],
            required_modalities=["text"],
            required_context_tokens=estimated_input + work.budget["max_output_tokens"],
            estimated_input_tokens=estimated_input,
            estimated_output_tokens=work.budget["max_output_tokens"],
            idempotency_key="foreign:route-with-unsettled-budget",
            purpose=executor.draft_worker.purpose, tier="brain",
        )["decision"]
        profile = fixture.router.get_profile(route["selected_profile_version_ref"])
        executor.draft_worker._before_model_call(work, route, profile, False)
        invocation, result = draft_adapter.execute(work, route, profile)
        fixture.store.register_invocation(invocation.to_dict())
        record_model_accounting(
            fixture.harness.observability, invocation, route, profile,
            actor_ref=executor.draft_worker.worker_ref,
            namespace=executor.draft_worker.namespace,
        )
        formal_envelope = executor.draft_worker._successful_result(
            work, route, invocation, result, result.outputs["text"])
        executor.scheduler.complete(
            work.id, claim["attempt"]["attempt_number"], executor.draft_worker.worker_ref,
            claim["lease_token"], formal_envelope,
            idempotency_key="foreign:self-consistent-with-unsettled-budget",
            result_envelope_hash=content_hash(formal_envelope.to_dict()))
        with self.assertRaisesRegex(
            MissionDocumentResearchExecutorError, "budget settlement is unavailable"
        ):
            executor.run_once(admission["id"])
        self.assertEqual(verifier.calls, 0)
        self.assertEqual(fixture.harness.staging.counts()["candidate_stage_requests"], 0)

    def test_model_invocation_family_must_match_selected_route(self):
        fixture, authority, args, _registration, _launcher = self._fixture()
        admission = authority.admit_from_plan(**args)
        executor, draft_adapter, verifier = self._executor(fixture, authority)
        for _ in range(3):
            executor.run_once(admission["id"])
        work_wire = executor._derive_work(admission, executor._blueprints(admission), 1)
        work = WorkOrder.from_dict(work_wire)
        claim = executor.scheduler.claim(
            executor.draft_worker.worker_ref, work_order_id=work.id)
        estimated_input = executor.draft_worker.token_counter(work.question)
        route = fixture.router.route(
            work, attempt_number=claim["attempt"]["attempt_number"],
            capability="research",
            policy_version_ref=fixture.draft_policy["policy_version_ref"],
            credential_slot_refs=[fixture.draft_profile["credential_slot_ref"]],
            required_modalities=["text"],
            required_context_tokens=estimated_input + work.budget["max_output_tokens"],
            estimated_input_tokens=estimated_input,
            estimated_output_tokens=work.budget["max_output_tokens"],
            idempotency_key="foreign:route-family-drift",
            purpose=executor.draft_worker.purpose, tier="brain",
        )["decision"]
        profile = fixture.router.get_profile(route["selected_profile_version_ref"])
        executor.draft_worker._before_model_call(work, route, profile, False)
        invocation, result = draft_adapter.execute(work, route, profile)
        invocation_wire = invocation.to_dict()
        invocation_wire["model_family"] = "forged-unselected-family"
        invocation = type(invocation).from_dict(invocation_wire)
        fixture.store.register_invocation(invocation.to_dict())
        accounting = record_model_accounting(
            fixture.harness.observability, invocation, route, profile,
            actor_ref=executor.draft_worker.worker_ref,
            namespace=executor.draft_worker.namespace,
        )
        executor.draft_worker._after_accounting(work, route, accounting)
        formal_envelope = executor.draft_worker._successful_result(
            work, route, invocation, result, result.outputs["text"])
        executor.scheduler.complete(
            work.id, claim["attempt"]["attempt_number"], executor.draft_worker.worker_ref,
            claim["lease_token"], formal_envelope,
            idempotency_key="foreign:self-consistent-route-family-drift",
            result_envelope_hash=content_hash(formal_envelope.to_dict()))
        with self.assertRaisesRegex(
            MissionDocumentResearchExecutorError, "differs from selected route"
        ):
            executor.run_once(admission["id"])
        self.assertEqual(verifier.calls, 0)
        self.assertEqual(fixture.harness.staging.counts()["candidate_stage_requests"], 0)

    def test_estimated_cost_success_retains_reservation_and_can_advance(self):
        fixture, authority, args, _registration, _launcher = self._fixture()
        admission = authority.admit_from_plan(**args)
        statement = "Managed services revenue is recognized over time."
        adapter = EstimatedCostAdapter({
            "schema_version": "0.1", "status": "answered", "answer": statement,
            "candidate": {"normalized_statement": statement,
                          "metric_or_aspect": "revenue recognition",
                          "period": "current policy", "basis": "reported",
                          "cited_match_indexes": [0]}, "missing": [],
        })
        executor, _draft, verifier = self._executor(
            fixture, authority, draft_adapter=adapter)
        final = None
        for _ in range(9):
            final = executor.run_once(admission["id"])
        self.assertEqual(final["research_status"], "candidate_staged")
        self.assertEqual(verifier.calls, 1)
        draft = executor._derive_work(admission, executor._blueprints(admission), 1)
        exact = fixture.budget.admission(
            work_order_ref=draft["id"], attempt_number=1, phase="assessment")
        self.assertIsNotNone(exact)
        self.assertIsNone(exact["settlement"])
        model_authority = exact_mission_document_model_execution_authority(
            draft, executor.scheduler.formal_result(draft["id"]),
            executor.draft_worker,
        )["execution_proof"]
        self.assertEqual(model_authority["cost_status"], "estimated")
        self.assertIsNone(model_authority["budget_settlement_ref"])
        self.assertEqual(model_authority["reserved_micros"],
                         exact["admission"]["reserved_micros"])
        cost = fixture.harness.observability.connection.execute(
            "SELECT cost_status FROM observability_cost_entries c JOIN "
            "observability_usage_entries u ON u.usage_entry_id=c.usage_entry_ref "
            "WHERE u.work_order_ref=?", (draft["id"],)
        ).fetchone()
        self.assertEqual(cost["cost_status"], "estimated")


if __name__ == "__main__":
    unittest.main()
