from __future__ import annotations

import unittest

from dalton_core.document_research_strategy import STRATEGY_VERSION
from dalton_core.research_planner import ResearchPlanError, plan_from_response, build_prompt
from dalton_core.research_task import inquiry_content_hash, _parameters_for
from tests.test_research_planner import ACN, IBM, NOW, state, inquiry, response


def strategy(**overrides):
    return {"strategy_version": STRATEGY_VERSION, "document_ref": "sales-note:original-1",
            "document_version_hash": "a" * 64, "query_terms": ["revenue recognition"],
            "query_rationale": "检验按时点还是按进度确认收入。", **overrides}


def with_document(source="source:sales-notes", **overrides):
    document = {"document_ref": "sales-note:original-1", "document_version_hash": "a" * 64,
                "authority_hash": "a" * 64, "authority_ref": "registered-document:sha256:" + "a" * 64,
                "source_ref": source, "readable": True,
                "operations": ["search_registered_document", "read_registered_document"]}
    return state(readable_documents_by_company={ACN: [document]},
                 document_research_policy={"max_query_terms": 10, "max_query_term_chars": 160},
                 **overrides)


class DocumentStrategyTests(unittest.TestCase):
    def test_source_neutral_strategy_needs_no_dossier_failure(self):
        for source in ("source:sales-notes", "source:company-wiki", "source:alphaengine", "source:sec-edgar"):
            with self.subTest(source=source):
                planned = plan_from_response(with_document(source), response(inquiries=[inquiry(
                    question="公司如何确认收入？", directed_document=strategy())]), created_at=NOW)
                self.assertEqual(planned["inquiries"][0]["directed_document"], strategy())
                self.assertNotIn("repair_target_ref", planned["inquiries"][0])

    def test_wrong_company_version_or_unreadable_document_refused(self):
        cases = [(with_document(), inquiry(company_ref=IBM, directed_document=strategy())),
                 (with_document(), inquiry(directed_document=strategy(document_version_hash="b" * 64)))]
        unavailable = with_document()
        unavailable["companies"][0]["readable_documents"][0]["readable"] = False
        cases.append((unavailable, inquiry(directed_document=strategy())))
        for current, proposed in cases:
            with self.subTest(proposed=proposed):
                with self.assertRaises(ResearchPlanError):
                    plan_from_response(current, response(inquiries=[proposed]), created_at=NOW)

    def test_model_cannot_inject_path_or_route(self):
        for extra in ({"path": "/private/source"}, {"model": "arbitrary"}, {"source_ref": "other"}):
            with self.subTest(extra=extra), self.assertRaises(ResearchPlanError):
                plan_from_response(with_document(), response(inquiries=[inquiry(
                    directed_document=strategy(**extra))]), created_at=NOW)

    def test_query_limits_follow_configuration(self):
        current = with_document()
        current["document_research_policy"]["max_query_term_chars"] = 200
        selected = strategy(query_terms=["x" * 180])
        plan_from_response(current, response(inquiries=[inquiry(directed_document=selected)]), created_at=NOW)
        current["document_research_policy"]["max_query_term_chars"] = 100
        with self.assertRaises(ResearchPlanError):
            plan_from_response(current, response(inquiries=[inquiry(directed_document=selected)]), created_at=NOW)

    def test_version_and_query_rekey_work_but_rationale_alone_does_not(self):
        original = inquiry(directed_document=strategy())
        digest = inquiry_content_hash(original)
        for change in ({"document_version_hash": "b" * 64}, {"query_terms": ["contract assets"]}):
            self.assertNotEqual(digest, inquiry_content_hash(inquiry(directed_document=strategy(**change))))
        self.assertEqual(digest, inquiry_content_hash(inquiry(directed_document=strategy(
            query_rationale="不同措辞解释同一检索。"))))

    def test_directed_query_cannot_fall_through_to_a_broad_paid_probe(self):
        self.assertIsNone(_parameters_for({"operation": "get_company_facts"}, ACN,
                          inquiry=inquiry(directed_document=strategy()), inquiry_hash="a" * 64,
                          as_of="2026-09-11"))

    def test_document_availability_changes_state_identity(self):
        current = with_document()
        self.assertNotEqual(state()["content_hash"], current["content_hash"])
        self.assertIn("Claims summarize previous findings", build_prompt(current))
        self.assertIn("document's language", build_prompt(current))
