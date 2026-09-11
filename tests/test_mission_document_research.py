from __future__ import annotations

import hashlib
import json
import unittest
from pathlib import Path

from dalton_core.coverage_mission import CoverageMissionAuthority
from dalton_core.document_research import (
    DocumentResearchRegistry, FeedDocumentSourceAdapter,
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
from dalton_core.raw_spool import RawSpool
from dalton_core.research_question_backlog import ResearchQuestionBacklog
from dalton_core.research_task import inquiry_content_hash, inquiry_ref_for
from dalton_core.store import content_hash
from tests.test_document_research import FakeLauncher, FakeReceiptReader
from tests.test_mission_annual_research import COMPANY, MissionAnnualFixture, NOW


class MissionDocumentResearchTests(unittest.TestCase):
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
        registry = DocumentResearchRegistry(
            adapters={COMPANY_WIKI_SOURCE_REF: adapter}, policy=policy
        )
        registration = registry.register(
            source_ref=COMPANY_WIKI_SOURCE_REF, document_ref=document_ref,
            purpose=PURPOSE, acquisition_ticket_ref=ticket_ref,
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
        stored_plan = CoverageMissionAuthority(fixture.store).record_research_plan(
            plan, decided_by=fixture.mission["autonomy"]["automation_principal"]
        )
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


if __name__ == "__main__":
    unittest.main()
