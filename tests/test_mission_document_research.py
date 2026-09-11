from __future__ import annotations

import hashlib
import json
import unittest
from pathlib import Path

from dalton_core.contracts import ResultEnvelope, WorkOrder
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
from dalton_core.document_research_qualitative import (
    MissionDocumentDraftWorker, MissionDocumentVerifierWorker,
)
from dalton_core.mission_document_research_executor import (
    MissionDocumentResearchExecutor, read_mission_document_research_observations,
)
from dalton_core.annual_report_qualitative import AnnualReportQualitativeError
from dalton_core.raw_spool import RawSpool
from dalton_core.research_question_backlog import ResearchQuestionBacklog
from dalton_core.research_task import inquiry_content_hash, inquiry_ref_for
from dalton_core.store import content_hash
from tests.test_document_research import FakeLauncher, FakeReceiptReader
from tests.test_mission_annual_research import (
    COMPANY, CountingFakeAdapter, MissionAnnualFixture, NOW,
)


class MissionDocumentResearchTests(unittest.TestCase):
    def _executor(self, fixture, authority):
        statement = "Managed services revenue is recognized over time."
        draft_adapter = CountingFakeAdapter({
            "schema_version": "0.1", "status": "answered", "answer": statement,
            "candidate": {"normalized_statement": statement,
                          "metric_or_aspect": "managed services revenue recognition",
                          "period": "current policy", "basis": "reported",
                          "cited_match_indexes": [0]}, "missing": [],
        })
        verifier_adapter = CountingFakeAdapter({
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
            clock=fixture.harness.clock,
        ), draft_adapter, verifier_adapter

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

    def _fixture(self):
        fixture = MissionAnnualFixture(
            self, additional_connected_source=COMPANY_WIKI_SOURCE_REF,
            company_in_mandate=True,
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


if __name__ == "__main__":
    unittest.main()
