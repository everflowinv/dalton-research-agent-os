from __future__ import annotations

import hashlib
import json
import sqlite3
import unittest
from datetime import timedelta
from pathlib import Path

from dalton_core.contracts import ModelInvocation, ResultEnvelope, WorkOrder
from dalton_core.coverage_mission import CoverageMissionAuthority
from dalton_core.document_research import (
    CoreAcquiredDocumentSourceAdapter, DocumentResearchRegistry, FeedDocumentSourceAdapter,
    build_document_research_policy,
)
from dalton_core.document_research_strategy import STRATEGY_VERSION
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
)
from dalton_core.mission_document_research_executor import (
    MissionDocumentResearchExecutor, MissionDocumentResearchExecutorError,
    effective_mission_document_work_orders,
    exact_mission_document_model_execution_authority,
    read_mission_document_research_observations,
)
from dalton_core.annual_report_qualitative import AnnualReportQualitativeError
from dalton_core.annual_report_runtime import (
    DRAFT_MODEL_CONFIG_NAME, VERIFIER_MODEL_CONFIG_NAME,
)
from dalton_core.raw_spool import RawSpool
from dalton_core.research_question_backlog import ResearchQuestionBacklog
from dalton_core.research_task import inquiry_content_hash, inquiry_ref_for
from dalton_core.store import content_hash
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


class MissionDocumentResearchTests(unittest.TestCase):
    def _executor(self, fixture, authority, *, draft_adapter=None,
                  verifier_adapter=None, fault_injector=None):
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
        return MissionDocumentResearchExecutor(
            authority=authority, scheduler=scheduler, registry=authority.registry,
            draft_worker=draft, verifier_worker=verifier,
            staging=fixture.harness.staging, actor_ref="automation:test",
            clock=fixture.harness.clock, fault_injector=fault_injector,
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

    def _fixture(self, *, auto_commit=False):
        fixture = MissionAnnualFixture(
            self, additional_connected_source=COMPANY_WIKI_SOURCE_REF,
            company_in_mandate=True, auto_commit=auto_commit,
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
        executor, _draft, _verifier = self._executor(fixture, authority)
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
        executor, _draft, _verifier = self._executor(fixture, authority)
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

    def test_zero_policy_and_unknown_send_state_do_not_create_recovery_work(self):
        from dalton_core.openclaw_model_adapter import BrokerConnectionError

        class UnknownAdapter(CountingFakeAdapter):
            def execute(self, work, route, selected):
                self.calls += 1
                raise BrokerConnectionError("socket failed after an unknown boundary")

        for maximum, expected_reason in ((0, "fresh_work_recovery_disabled"),
                                         (2, "send_state_unproved")):
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
                ).fetchone()[0], 0)
                self.assertEqual(read_mission_document_research_observations(
                    fixture.store.connection)[0]["outcome"], "recovery_required")

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
        self._enable_recovery(fixture, maximum=1, elapsed=172800)
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
        fixture.harness.clock.value += timedelta(days=1)
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
