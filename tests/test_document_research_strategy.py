from __future__ import annotations

import unittest

from dalton_core.document_research_strategy import (
    FINANCIAL_NOTE_TARGET_REF,
    FINANCIAL_NOTE_TARGET_SCHEMA_VERSION,
    STRATEGY_VERSION,
)
from dalton_core.research_planner import ResearchPlanError, plan_from_response, build_prompt
from dalton_core.research_task import inquiry_content_hash, _parameters_for
from dalton_core.store import content_hash
from tests.test_research_planner import ACN, IBM, NOW, state, inquiry, response


def strategy(**overrides):
    return {"strategy_version": STRATEGY_VERSION, "document_ref": "sales-note:original-1",
            "document_version_hash": "a" * 64, "query_terms": ["revenue recognition"],
            "query_rationale": "检验按时点还是按进度确认收入。", **overrides}


def evidence_target(**overrides):
    return {
        "schema_version": FINANCIAL_NOTE_TARGET_SCHEMA_VERSION,
        "target_ref": FINANCIAL_NOTE_TARGET_REF,
        "kind": "diluted_eps_numerator",
        "statement_ingest_ref": "statement-ingest:" + "b" * 32,
        "statement_filing_hash": "c" * 64,
        "accession": "0001467373-25-000217",
        "form": "10-K",
        "applicability_kind": "annual",
        "periods": [{"period_start": "2024-09-01", "period_end": "2025-08-31"}],
        **overrides,
    }


def with_document(source="source:sales-notes", **overrides):
    document = {"document_ref": "sales-note:original-1", "document_version_hash": "a" * 64,
                "authority_hash": "a" * 64, "authority_ref": "registered-document:sha256:" + "a" * 64,
                "source_ref": source, "readable": True,
                "operations": ["search_registered_document", "read_registered_document"]}
    return state(readable_documents_by_company={ACN: [document]},
                 document_research_policy={"max_query_terms": 10, "max_query_term_chars": 160},
                 **overrides)


class DocumentStrategyTests(unittest.TestCase):
    def test_old_strategy_and_inquiry_identity_remain_byte_exact(self):
        original = inquiry(directed_document=strategy())
        planned = plan_from_response(
            with_document(), response(inquiries=[original]), created_at=NOW)
        self.assertEqual(planned["inquiries"][0]["directed_document"], strategy())
        self.assertEqual(
            inquiry_content_hash(original),
            "60bec2bf0389f4a83cfc4f48fae55aa313daea05c76854140aa34bdd8271164f",
        )

    def test_financial_note_target_must_be_selected_verbatim_from_document(self):
        current = with_document()
        document = current["companies"][0]["readable_documents"][0]
        document["evidence_targets"] = [evidence_target()]
        current["content_hash"] = content_hash(
            {key: value for key, value in current.items() if key != "content_hash"})
        selected = strategy(evidence_target=evidence_target())
        planned = plan_from_response(
            current, response(inquiries=[inquiry(directed_document=selected)]), created_at=NOW)
        self.assertEqual(planned["inquiries"][0]["directed_document"], selected)
        self.assertNotEqual(
            inquiry_content_hash(inquiry(directed_document=strategy())),
            inquiry_content_hash(inquiry(directed_document=selected)),
        )

        for target in (
            evidence_target(statement_filing_hash="d" * 64),
            evidence_target(form="10-Q"),
            evidence_target(periods=[
                {"period_start": "2025-01-01", "period_end": "2025-03-31"},
            ]),
            evidence_target(periods=[
                {"period_start": "20250101", "period_end": "2025-12-31"},
            ]),
            evidence_target(periods=[
                {"period_start": "2025-01-01", "period_end": "2025-12-31"},
                {"period_start": "2024-01-01", "period_end": "2024-12-31"},
            ]),
        ):
            with self.subTest(target=target), self.assertRaises(ResearchPlanError):
                plan_from_response(current, response(inquiries=[inquiry(
                    directed_document=strategy(evidence_target=target))]), created_at=NOW)

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
        self.assertIn("It is not numeric authority", build_prompt(current))
        self.assertIn("explicit authority gap", build_prompt(current))
