import copy
import json
import unittest
from unittest.mock import patch

from dalton_core.coverage_mission import CoverageMissionAuthority
from dalton_core.document_research import (
    CoreAcquiredDocumentSourceAdapter, DocumentResearchRegistry,
    PublicWebDocumentSourceAdapter, build_document_research_policy,
)
from dalton_core.financial_note_evidence import (
    FinancialNoteEvidenceError,
    _execution_checkpoint,
    _period_kind,
    financial_note_evidence_binding,
    resolve_financial_note_evidence,
    resolve_financial_note_evidence_ref,
    validate_financial_note_target,
)
from dalton_core.mission_document_research import MissionDocumentResearchAuthority, PURPOSE
from dalton_core.research_question_backlog import ResearchQuestionBacklog
from dalton_core.research_task import inquiry_content_hash, inquiry_ref_for
from dalton_core.store import content_hash
from tests import test_mission_document_research_promotion as promotion_fixtures
from tests import test_mission_document_research as document_fixtures
from tests.test_mission_annual_research import COMPANY, MissionAnnualFixture


NOTE_TEXT = (
    "Note 3 Earnings Per Share. Net income attributable to the parent was 100. "
    "The diluted earnings numerator adds 5 of exchangeable noncontrolling "
    "interest and excludes other noncontrolling interests. Diluted weighted "
    "average shares were 10 and diluted earnings per share was 10.5."
)


class FinancialNoteTargetTests(unittest.TestCase):
    def target(self):
        return {
            "schema_version": "financial-note-target-0.1",
            "target_ref": "financial_note:diluted_eps_numerator:0.1",
            "kind": "diluted_eps_numerator",
            "statement_ingest_ref": "statement-ingest:" + "1" * 32,
            "statement_filing_hash": "2" * 64,
            "accession": "0001467373-25-000217", "form": "10-K",
            "applicability_kind": "annual",
            "periods": [{"period_start": "2024-09-01",
                         "period_end": "2025-08-31"}],
        }

    def test_closed_annual_target_round_trips(self):
        self.assertEqual(validate_financial_note_target(self.target()), self.target())

    def test_target_rejects_foreign_kind_unsorted_periods_and_extra_authority(self):
        cases = []
        foreign = self.target(); foreign["kind"] = "cash_flow_note"
        cases.append(foreign)
        unsorted = self.target(); unsorted["periods"] = [
            {"period_start": "2024-09-01", "period_end": "2025-08-31"},
            {"period_start": "2023-09-01", "period_end": "2024-08-31"},
        ]
        cases.append(unsorted)
        extra = self.target(); extra["amount"] = "7685673000"
        cases.append(extra)
        for value in cases:
            with self.subTest(value=value), self.assertRaises(FinancialNoteEvidenceError):
                validate_financial_note_target(value)

    def test_small_binding_retains_exact_period_and_full_record_hash(self):
        body = {
            "schema_version": "financial-note-evidence-authority-0.1",
            "target_ref": "financial_note:diluted_eps_numerator:0.1",
            "company_ref": "company:sec-cik:0001467373",
            "mission_version_ref": "mission:1", "mission_version_hash": "1" * 64,
            "admission_ref": "admission:1", "admission_hash": "2" * 64,
            "promotion_ref": "promotion:1", "promotion_hash": "3" * 64,
            "evidence_version_ref": "evidence:1", "evidence_version_hash": "4" * 64,
            "claim_version_ref": "claim:1", "claim_version_hash": "5" * 64,
            "statement_ingest_ref": "statement-ingest:1",
            "statement_filing_hash": "6" * 64,
            "accession": "0001467373-25-000217", "form": "10-K",
            "applicability_kind": "annual",
            "periods": [{"period_start": "2024-09-01", "period_end": "2025-08-31"}],
            "registration_ref": "registration:1", "registration_hash": "7" * 64,
            "source_authority_ref": "source:1", "source_authority_hash": "8" * 64,
            "source_content_hash": "9" * 64,
            "search_proof_ref": "search:1", "search_proof_hash": "a" * 64,
            "passages": [], "normalized_statement": "The note describes the numerator.",
            "execution_checkpoint": {"promotion_execution_proof_hash": "b" * 64,
                                     "stages": [], "accounting_proof_hashes": [],
                                     "accounting_replay": "promotion_checkpoint_only"},
        }
        record = {**body, "ref": "financial-note-evidence:admission:1"}
        record["content_hash"] = content_hash(record)
        binding = financial_note_evidence_binding(record)
        self.assertEqual(binding["applicability_kind"], "annual")
        self.assertEqual(binding["periods"], body["periods"])
        tampered = copy.deepcopy(record)
        tampered["periods"][0]["period_end"] = "2025-08-30"
        with self.assertRaisesRegex(FinancialNoteEvidenceError, "drifted"):
            financial_note_evidence_binding(tampered)

    def test_resolver_uses_shared_inclusive_period_boundaries(self):
        self.assertEqual(_period_kind("2025-01-01", "2025-10-18"), "annual")
        self.assertEqual(_period_kind("2024-01-01", "2025-01-14"), "annual")
        self.assertEqual(_period_kind("2025-01-01", "2025-03-21"), "quarter")
        self.assertEqual(_period_kind("2025-01-01", "2025-04-10"), "quarter")
        self.assertIsNone(_period_kind("2025-01-01", "2025-10-17"))
        self.assertIsNone(_period_kind("2025-01-01", "2025-04-11"))


class FinancialNoteExecutionCheckpointTests(unittest.TestCase):
    def test_real_promoted_execution_replays_immutable_core_and_router_rows(self):
        case = promotion_fixtures.DocumentPromotionTests()
        try:
            fixture, executor, _admission, _works, _records, _outcome, *_ = (
                case._completed())
            promotion = json.loads(fixture.store.connection.execute(
                "SELECT record_json FROM mission_document_research_promotions"
            ).fetchone()[0])
            receipt = fixture.store.connection.execute(
                "SELECT candidate_evidence_ref,candidate_claim_ref FROM reviewed_candidate_commits"
            ).fetchone()
            bundle = executor.staging.exact_candidate_bundle(
                evidence_ref=receipt["candidate_evidence_ref"],
                claim_ref=receipt["candidate_claim_ref"],
                idempotency_key=("mission-document-research-candidate:"
                                 + promotion["admission_ref"]),
            )
            checkpoint = _execution_checkpoint(
                fixture.store.connection, fixture.router.connection,
                promotion["execution_proof"], material=bundle["material"])
            self.assertEqual(len(checkpoint["stages"]), 4)
            self.assertEqual(len(checkpoint["accounting_proof_hashes"]), 2)
        finally:
            case.doCleanups()


class FinancialNoteResolverTests(unittest.TestCase):
    @staticmethod
    def _replace_statement_filing(fixture):
        connection = fixture.store.connection
        old = connection.execute(
            "SELECT dispatch_id FROM coverage_mission_statement_filings"
        ).fetchone()
        connection.execute("DROP TRIGGER coverage_mission_statement_filings_no_delete")
        connection.execute("DELETE FROM coverage_mission_statement_filings")
        connection.execute(
            "CREATE TRIGGER coverage_mission_statement_filings_no_delete "
            "BEFORE DELETE ON coverage_mission_statement_filings "
            "BEGIN SELECT RAISE(ABORT,'statement filings are append-only'); END")
        identity = {
            "company_ref": COMPANY, "cik": fixture.source.accession[:10],
            "accession": fixture.source.accession, "form": "10-K", "line_count": 2,
        }
        ingest = "statement-ingest:" + content_hash(identity)[:32]
        rows = [
            {"statement": "income", "concept": "us-gaap:EarningsPerShareDiluted",
             "label": "Diluted earnings per share", "level": 0,
             "parent_concept": None, "is_breakdown": False,
             "dimension_axis": None, "dimension_member": None, "dimension_count": 0,
             "period_start": "2024-09-01", "period_end": "2025-08-31",
             "value": "10.5", "unit": "USDPerShare", "balance": None},
            {"statement": "income",
             "concept": "us-gaap:WeightedAverageNumberOfDilutedSharesOutstanding",
             "label": "Diluted weighted average shares", "level": 0,
             "parent_concept": None, "is_breakdown": False,
             "dimension_axis": None, "dimension_member": None, "dimension_count": 0,
             "period_start": "2024-09-01", "period_end": "2025-08-31",
             "value": "10", "unit": "shares", "balance": None},
        ]
        body = {
            **identity, "entity_name": "Test issuer", "filed": "2025-10-01",
            "report_date": "2025-08-31",
            "source_record_refs": ["raw-sink:" + "1" * 64],
            "governance_ref": "governance:test", "governance_hash": "2" * 64,
        }
        filing_hash = content_hash({**body, "statement_lines_hash": content_hash(rows)})
        coverage = CoverageMissionAuthority(fixture.store)
        with coverage._transaction() as cursor:
            cursor.execute(
                "UPDATE coverage_mission_statement_dispatches SET "
                "mission_version_ref=?,mission_version_hash=?,company_ref=?,form='10-K',"
                "status='succeeded' WHERE dispatch_id=?",
                (fixture.mission["id"], fixture.mission["content_hash"], COMPANY,
                 old["dispatch_id"]))
            cursor.execute(
                "INSERT INTO coverage_mission_statement_filings VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (ingest, old["dispatch_id"], COMPANY, identity["cik"], body["entity_name"],
                 identity["accession"], "10-K", body["filed"], body["report_date"], 2,
                 json.dumps(body["source_record_refs"], separators=(",", ":")),
                 body["governance_ref"], body["governance_hash"],
                 fixture.mission["created_at"], filing_hash))
            for ordinal, line in enumerate(rows):
                cursor.execute(
                    "INSERT INTO coverage_mission_statement_lines("
                    "line_id,ingest_id,statement,ordinal,concept,label,level,parent_concept,"
                    "is_breakdown,dimension_axis,dimension_member,dimension_count,period_start,"
                    "period_end,value,unit,balance) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (f"{ingest}#{ordinal}", ingest, line["statement"], ordinal,
                     line["concept"], line["label"], line["level"], line["parent_concept"],
                     int(line["is_breakdown"]), line["dimension_axis"],
                     line["dimension_member"], line["dimension_count"], line["period_start"],
                     line["period_end"], line["value"], line["unit"], line["balance"]))
        return ingest, filing_hash

    def _completed_typed(self):
        with patch("tests.test_research_plan_annual_report.TEXT", NOTE_TEXT):
            fixture = MissionAnnualFixture(self, company_in_mandate=True, auto_commit=True)
        ingest, filing_hash = self._replace_statement_filing(fixture)
        document_ref = f"sec:filing:{fixture.source.accession}"

        class Launcher:
            @staticmethod
            def read_completed_manifest(ticket_ref, selected_ref):
                if ticket_ref != fixture.source.ticket_ref:
                    raise RuntimeError("foreign ticket")
                return fixture.source.manifest

            @staticmethod
            def locate_completed_manifest_binding(selected_ref):
                if selected_ref != document_ref:
                    raise RuntimeError("foreign document")
                return {"ticket_ref": fixture.source.ticket_ref,
                        "manifest": fixture.source.manifest}

        web_adapter = PublicWebDocumentSourceAdapter(
            source_ref="source:public-web", launcher=Launcher(), core=fixture.store,
            spool=fixture.source.spool, receipt_reader=fixture.source.reader,
            max_source_chars=10_000, max_pdf_pages=100,
            max_decompressed_bytes=1_000_000)
        base_registration, source_text = web_adapter.materialize(
            document_ref=fixture.source.manifest["url_ref"],
            acquisition_ticket_ref=fixture.source.ticket_ref)
        base_body = {key: value for key, value in base_registration.items()
                     if key not in {"id", "content_hash"}}
        base_body.update({"source_ref": "source:sec-edgar", "document_ref": document_ref})
        base_digest = content_hash(base_body)
        sec_registration = {
            **base_body, "id": "registered-document:sha256:" + base_digest,
        }
        sec_registration["content_hash"] = content_hash({
            key: value for key, value in sec_registration.items() if key != "content_hash"})

        class SecAdapter:
            source_ref = "source:sec-edgar"

            @staticmethod
            def materialize(*, document_ref, acquisition_ticket_ref):
                if (document_ref != sec_registration["document_ref"]
                        or acquisition_ticket_ref != sec_registration["acquisition_ticket_ref"]):
                    raise RuntimeError("foreign SEC original")
                return dict(sec_registration), source_text

        adapter = SecAdapter()
        policy = build_document_research_policy(
            policy_ref="policy:financial-note:test:0.1", allowed_purposes=[PURPOSE],
            allowed_access_policy_refs=["policy:access:public-web"],
            max_question_chars=2_000, max_query_terms=10, max_query_term_chars=160,
            max_results=8, max_context_before_chars=80,
            max_context_after_chars=260, max_read_chars=10_000)
        registry = DocumentResearchRegistry(
            adapters={"source:sec-edgar": adapter}, policy=policy,
            acquired_document_adapter=CoreAcquiredDocumentSourceAdapter(
                core=fixture.store, adapters={"source:sec-edgar": adapter}))
        acquired_ref = fixture.store.connection.execute(
            "SELECT record_id FROM coverage_mission_discovered_documents "
            "WHERE document_ref=? AND status='acquired'", (document_ref,)).fetchone()[0]
        registration = registry.register_acquired_document(
            record_id=acquired_ref, purpose=PURPOSE)
        target = {
            "schema_version": "financial-note-target-0.1",
            "target_ref": "financial_note:diluted_eps_numerator:0.1",
            "kind": "diluted_eps_numerator", "statement_ingest_ref": ingest,
            "statement_filing_hash": filing_hash, "accession": fixture.source.accession,
            "form": "10-K", "applicability_kind": "annual",
            "periods": [{"period_start": "2024-09-01", "period_end": "2025-08-31"}],
        }
        inquiry = {
            "rank": 0, "company_ref": COMPANY,
            "question": "What disclosed adjustment defines the diluted EPS numerator?",
            "wants": "Return only the exact numerator attribution evidence.",
            "because": "The filed note governs diluted EPS.",
            "directed_document": {
                "strategy_version": "directed-document:0.1",
                "document_ref": registration["document_ref"],
                "document_version_hash": registration["content_hash"],
                "query_terms": ["diluted earnings numerator", "noncontrolling interest"],
                "query_rationale": "Read the exact EPS note.",
                "evidence_target": target,
            },
        }
        plan = {
            "schema_version": "0.1", "task_ref": "task:research-plan-directives:0.1",
            "created_at": fixture.mission["created_at"], "state_hash": "e" * 64,
            "mission_version_ref": fixture.mission["id"],
            "assessment": "Read the exact EPS note.", "directives": [],
            "inquiries": [inquiry], "sufficiency": [],
        }
        plan["content_hash"] = content_hash(plan)
        stored = document_fixtures.MissionDocumentResearchTests()._record_plan(fixture, plan)
        question = ResearchQuestionBacklog(fixture.store).record_question(
            mandate_version_ref=fixture.mission["bindings"]["mandate_version"]["ref"],
            company_ref=COMPANY, question=inquiry["question"],
            answer_criteria=inquiry["wants"], source_refs=[registration["source_ref"]],
            actor_ref=fixture.mission["autonomy"]["automation_principal"],
            idempotency_key="financial-note:test:question",
            mission_binding={"ref": fixture.mission["id"],
                             "hash": fixture.mission["content_hash"]})
        authority = MissionDocumentResearchAuthority(
            fixture.store, registry=registry,
            registration_resolver=lambda ref: registration if ref == registration["id"] else None,
            model_execution_resolver=fixture.authority._model_authority,
            planner_scheduler_connection=fixture.harness.scheduler().connection,
            planner_router_connection=fixture.router.connection,
            clock=fixture.harness.clock)
        admission = authority.admit_from_plan(
            plan_ref=stored["plan_id"],
            inquiry_ref=inquiry_ref_for(inquiry_content_hash(inquiry)),
            question_version_ref=question["question_version_ref"],
            document_authority_ref=registration["id"])
        statement = "The diluted numerator adds exchangeable NCI and excludes other NCI."
        draft = document_fixtures.RouteBoundCountingFakeAdapter({
            "schema_version": "0.1", "status": "answered", "answer": statement,
            "candidate": {"normalized_statement": statement,
                          "metric_or_aspect": "diluted EPS numerator attribution",
                          "period": "year ended 2025-08-31", "basis": "filed note",
                          "cited_match_indexes": [0]}, "missing": []})
        verifier = document_fixtures.RouteBoundCountingFakeAdapter({
            "schema_version": "0.1", "verdict": "pass",
            "verified_statement": statement, "findings": []})
        executor, _, _ = document_fixtures.MissionDocumentResearchTests()._executor(
            fixture, authority, draft_adapter=draft, verifier_adapter=verifier)
        outcome = promotion_fixtures.DocumentPromotionTests._drive(executor, admission)
        self.assertEqual(outcome["research_status"], "canonical_claim_promoted")
        return fixture, executor, admission, target

    def test_exact_typed_promoted_note_resolves_without_numeric_invention(self):
        fixture, executor, admission, target = self._completed_typed()
        result = resolve_financial_note_evidence(
            core_connection=fixture.store.connection,
            router_connection=fixture.router.connection,
            staging_connection=executor.staging.connection,
            registry=executor.registry, admission_ref=admission["id"])
        self.assertEqual(result["target_ref"], target["target_ref"])
        self.assertEqual(result["periods"], target["periods"])
        self.assertEqual(result["applicability_kind"], "annual")
        self.assertEqual(len(result["passages"]), 1)
        self.assertNotIn("amount", result)
        self.assertEqual(financial_note_evidence_binding(result)["content_hash"],
                         result["content_hash"])
        self.assertEqual(resolve_financial_note_evidence_ref(
            core_connection=fixture.store.connection,
            router_connection=fixture.router.connection,
            staging_connection=executor.staging.connection,
            registry=executor.registry, evidence_ref=result["ref"]), result)

    def test_statement_line_tamper_invalidates_promoted_note_authority(self):
        fixture, executor, admission, _target = self._completed_typed()
        connection = fixture.store.connection
        connection.execute("DROP TRIGGER coverage_mission_statement_lines_no_update")
        connection.execute(
            "UPDATE coverage_mission_statement_lines SET unit='percent' "
            "WHERE concept='us-gaap:EarningsPerShareDiluted'"
        )
        with self.assertRaisesRegex(
                FinancialNoteEvidenceError, "filing authority|current filing authority"):
            resolve_financial_note_evidence(
                core_connection=connection,
                router_connection=fixture.router.connection,
                staging_connection=executor.staging.connection,
                registry=executor.registry, admission_ref=admission["id"])

    def test_legacy_promoted_result_cannot_be_retagged_as_financial_note(self):
        case = promotion_fixtures.DocumentPromotionTests()
        try:
            fixture, executor, admission, *_ = case._completed()
            with self.assertRaisesRegex(FinancialNoteEvidenceError, "target"):
                resolve_financial_note_evidence(
                    core_connection=fixture.store.connection,
                    router_connection=fixture.router.connection,
                    staging_connection=executor.staging.connection,
                    registry=executor.registry, admission_ref=admission["id"])
        finally:
            case.doCleanups()

    def test_recomputed_promotion_hash_does_not_hide_foreign_invocation(self):
        case = promotion_fixtures.DocumentPromotionTests()
        try:
            fixture, executor, _admission, _works, _records, _outcome, *_ = (
                case._completed())
            promotion = json.loads(fixture.store.connection.execute(
                "SELECT record_json FROM mission_document_research_promotions"
            ).fetchone()[0])
            receipt = fixture.store.connection.execute(
                "SELECT candidate_evidence_ref,candidate_claim_ref FROM reviewed_candidate_commits"
            ).fetchone()
            bundle = executor.staging.exact_candidate_bundle(
                evidence_ref=receipt["candidate_evidence_ref"],
                claim_ref=receipt["candidate_claim_ref"],
                idempotency_key=("mission-document-research-candidate:"
                                 + promotion["admission_ref"]),
            )
            forged = copy.deepcopy(promotion["execution_proof"])
            forged["accounting_proofs"][0]["model_invocation_ref"] = "invocation:foreign"
            body = dict(forged["accounting_proofs"][0]); body.pop("content_hash")
            forged["accounting_proofs"][0]["content_hash"] = content_hash(body)
            proof_body = dict(forged); proof_body.pop("content_hash")
            forged["content_hash"] = content_hash(proof_body)
            with self.assertRaisesRegex(FinancialNoteEvidenceError,
                                        "route authority|execution identity"):
                _execution_checkpoint(
                    fixture.store.connection, fixture.router.connection, forged,
                    material=bundle["material"])
        finally:
            case.doCleanups()


if __name__ == "__main__":
    unittest.main()
