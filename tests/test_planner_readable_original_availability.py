"""A prompt excerpt is not an admission prerequisite or an answered question."""
from __future__ import annotations

import copy
import unittest

from dalton_core.document_research_strategy import STRATEGY_VERSION
from dalton_core.research_planner import (
    ResearchPlanError, build_prompt, plan_from_response, project_state_for_prompt,
)
from dalton_core.research_state import planning_core_hash
from tests import test_research_planner as fixtures
from tests import test_research_planner_replans_after_withdrawal as withdrawn_fixtures


class ReadableOriginalAvailabilityTests(unittest.TestCase):
    def state(self):
        self.document = {
            "document_ref": "sec:filing:synthetic-annual",
            "document_version_hash": "a" * 64,
            "authority_ref": "registered-document:synthetic-annual",
            "authority_hash": "a" * 64,
            "readable": True, "completeness": "complete",
            "operations": ["search_registered_document", "read_registered_document"],
        }
        return fixtures.state(
            readable_documents_by_company={fixtures.ACN: [self.document]},
            document_research_policy={"max_query_terms": 16, "max_query_term_chars": 240},
            document_research_settlement={"by_company": {fixtures.ACN: {
                "reopened": [{"document_ref": self.document["document_ref"],
                              "question": "How is revenue recognized?"}]}}},
        )

    def reply(self, **strategy_changes):
        strategy = {"strategy_version": STRATEGY_VERSION, **{
            key: self.document[key] for key in ("document_ref", "document_version_hash")},
            "query_terms": ["revenue recognition"],
            "query_rationale": "Read the accounting-policy note.", **strategy_changes}
        return fixtures.response(inquiries=[fixtures.inquiry(
            question="How is revenue recognized?", wants="Cited original policy text.",
            directed_document=strategy)])

    def test_no_preview_is_readable_but_does_not_answer_the_question(self):
        state = self.state()
        company = state["companies"][0]
        availability = company["document_execution_availability"]
        self.assertTrue(availability["has_readable_original"])
        self.assertFalse(availability["preview_required_for_reading"])
        self.assertEqual(availability["reopened_originals"], [(
            self.document["document_ref"], self.document["document_version_hash"])])
        before = copy.deepcopy(state)
        plan = plan_from_response(state, self.reply(), created_at=fixtures.NOW)
        self.assertEqual(plan["inquiries"][0]["directed_document"]["document_version_hash"], "a" * 64)
        self.assertEqual(state, before)  # no new Figure, Claim, budget or task
        with_preview = copy.deepcopy(state)
        with_preview["companies"][0]["readable_documents"][0].update({
            "original_preview": "Synthetic opening excerpt",
            "preview_proof_ref": "document-read-proof:synthetic", "preview_proof_hash": "b" * 64})
        self.assertEqual(planning_core_hash(state), planning_core_hash(with_preview))

    def test_missing_or_unverified_original_is_not_enabled_by_the_preview_policy(self):
        state = self.state()
        for changes in ({"document_version_hash": "b" * 64},
                        {"document_ref": "sec:filing:foreign"}):
            with self.subTest(changes=changes), self.assertRaisesRegex(ResearchPlanError, "absent or ambiguous"):
                plan_from_response(state, self.reply(**changes), created_at=fixtures.NOW)
        state["companies"][0]["readable_documents"][0]["readable"] = False
        with self.assertRaisesRegex(ResearchPlanError, "no verified directed-reading capability"):
            plan_from_response(state, self.reply(), created_at=fixtures.NOW)
        absent = fixtures.state()
        self.assertFalse(absent["companies"][0]["document_execution_availability"]["has_readable_original"])

    def test_compressed_input_preserves_availability_and_separates_scheduling_from_evidence(self):
        helper = fixtures.FeedbackProjectionTests()
        state = helper.worst_case(12)
        projected = project_state_for_prompt(helper.rebind(state), max_input_bytes=helper.BOUND)
        for company in projected["companies"]:
            self.assertFalse(company["document_execution_availability"]["preview_required_for_reading"])
        prompt = build_prompt(projected)
        self.assertLessEqual(len(prompt.encode()), helper.BOUND)
        self.assertIn("without a preview; no preview does not mean missing material", prompt)
        self.assertIn("withdrawn executions, not pending reads", prompt)
        self.assertIn("versions remain selectable, not verified numeric answers", prompt)
        self.assertIn("Numeric questions require directed-document-numeric:0.1 and ALL numeric_targets", prompt)


class WithdrawnExecutionTests(unittest.TestCase):
    def test_new_admission_keeps_withdrawn_authority_immutable_and_refuses_another_pending_copy(self):
        from dalton_core.mission_document_research import MissionDocumentResearchEquivalentPending
        document = withdrawn_fixtures._DocumentFixture().build(self)
        connection = document.fixture.store.connection
        original = tuple(connection.execute(
            "SELECT * FROM mission_document_research_admissions WHERE admission_id=?",
            (document.first["id"],)).fetchone())
        document.withdraw(document.first["id"])
        self.assertEqual(document.read()["open"], 0)
        new = document.authority.admit_from_plan(**document.rephrased("Read the current original under current authority."))
        self.assertNotEqual(new["id"], document.first["id"])
        self.assertEqual(tuple(connection.execute(
            "SELECT * FROM mission_document_research_admissions WHERE admission_id=?",
            (document.first["id"],)).fetchone()), original)
        with self.assertRaises(MissionDocumentResearchEquivalentPending):
            document.authority.admit_from_plan(**document.rephrased("Do not duplicate the new pending read."))


if __name__ == "__main__":
    unittest.main()
