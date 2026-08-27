"""Company research view: deterministic projection, structured query, handoff."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from dalton_core.agenda import AgendaStore
from dalton_core.company_research_view import (
    CompanyResearchViewValidationError,
    build_company_research_view,
    query_company_research,
)
from dalton_core.context_materializer import ContextMaterializer
from dalton_core.observability import ObservabilityStore
from dalton_core.research_context import build_claim_index, build_reference_fixture_plan
from dalton_core.store import canonical_json, content_hash
from tests import test_industry_research as industry_fixture
from tests import test_weekly_brief as brief_fixture


INDUSTRY = industry_fixture.INDUSTRY
ACN = industry_fixture.ACN
OWNER = "human:coverage-owner"


class CompanyResearchViewTests(unittest.TestCase):
    def setUp(self) -> None:
        fixture = brief_fixture.WeeklyBriefAuthorityTests(
            methodName="test_first_issue_is_baseline_not_fake_weekly_delta_and_replays"
        )
        fixture.setUp()
        self.fixture = fixture
        self.addCleanup(fixture.doCleanups)
        self.store = fixture.fixture.store
        self.agenda = AgendaStore(self.store)

        self.mandate = self.agenda.create_mandate(
            "mandate:view-test", actor_ref=OWNER,
            objective="Admit the ACN thesis and open a follow-up question.",
            scope_refs=[INDUSTRY, ACN], constraints={}, success_criteria={},
            effective_from="2026-08-23T00:00:00+00:00", effective_until=None,
        )
        pack = fixture.fixture.driver_pack
        candidate = fixture.fixture.coverage.propose_thesis_admission(
            candidate_id="thesis-admission-candidate:acn:view",
            thesis_ref="thesis:acn:view",
            company_ref=ACN, industry_ref=INDUSTRY,
            template_ref="template:demand-conversion",
            driver_refs=["driver:demand-and-conversion"],
            mandate_version_ref=self.mandate["id"],
            mandate_version_hash=self.mandate["content_hash"],
            driver_pack_version_ref=pack["id"],
            driver_pack_version_hash=pack["content_hash"],
            content={
                "statement": "AI demand can support growth.",
                "mechanism": "Bookings convert into revenue.",
                "confidence": "low",
                "implied_expectation": "Demand signals are followed by revenue.",
                "claim_refs": [fixture.fixture.bookings["claim_version_id"]],
                "catalyst_refs": [],
                "falsifier_refs": ["falsifier:conversion-breaks"],
                "change_reason": "View test admission.",
            },
            actor_ref=OWNER, idempotency_key="thesis-admission-candidate:acn:view",
        )
        self.admitted = fixture.fixture.coverage.decide_thesis_admission(
            candidate_id=candidate["id"], candidate_hash=candidate["content_hash"],
            verdict="admit", rationale="Bound to the active mandate and pack.",
            decision_id="thesis-admission-decision:acn:view",
            actor_ref=OWNER, idempotency_key="thesis-admission-decision:acn:view",
        )

        from tests.test_industry_research import invocation
        adjudicator_invocation = dict(invocation("invocation:view-adjudicator"))
        adjudicator_invocation["model_family"] = "family:view-adjudicator"
        adjudicator_invocation["capability"] = "adjudicate"
        self.store.register_invocation(adjudicator_invocation)
        self.store.adjudicate_claim({
            "claim_version_ref": fixture.fixture.demand["claim_version_id"],
            "adjudicated_status": "corroborated",
            "rationale": "The filing exhibit supports the semantic claim.",
            "findings": [],
            "adjudicator_invocation_ref": "invocation:view-adjudicator",
            "subject_invocation_refs": ["invocation:industry-research"],
        })

        from dalton_core.research_question_backlog import ResearchQuestionBacklog
        self.questions = ResearchQuestionBacklog(self.store)
        self.question = self.questions.record_question(
            mandate_version_ref=self.mandate["id"], company_ref=ACN,
            question="Will AI demand convert into durable bookings?",
            answer_criteria="A same-filing bookings comparison.",
            source_refs=["source:sec-edgar"], actor_ref=OWNER,
            idempotency_key="view-test-question:1",
        )

        self.issue = fixture.publish()

    def test_view_is_deterministic_and_closed(self) -> None:
        first = build_company_research_view(self.store, ACN)
        second = build_company_research_view(self.store, ACN)
        self.assertEqual(canonical_json(first), canonical_json(second))
        expected = {
            "schema_version", "projection_kind", "id", "company_ref",
            "built_as_of", "thesis", "claims", "open_questions", "impact",
            "last_weekly_issue", "last_research_stop", "content_hash",
        }
        self.assertEqual(set(first), expected)
        body = {key: value for key, value in first.items() if key != "content_hash"}
        self.assertEqual(first["content_hash"], content_hash(body))
        self.assertEqual("company_research_view", first["projection_kind"])
        self.assertTrue(first["id"].startswith("company-research-view:"))

    def test_view_projects_thesis_claims_questions_and_issue(self) -> None:
        view = build_company_research_view(self.store, ACN)
        thesis = view["thesis"]
        self.assertEqual("current", thesis["status"])
        self.assertEqual("thesis:acn:view", thesis["thesis_ref"])
        self.assertEqual(
            self.admitted["thesis_version"]["id"], thesis["thesis_version_ref"]
        )
        self.assertEqual("low", thesis["confidence"])
        self.assertEqual("template:demand-conversion", thesis["template_ref"])
        self.assertEqual(["driver:demand-and-conversion"], thesis["driver_refs"])
        self.assertEqual(["falsifier:conversion-breaks"], thesis["falsifier_refs"])

        claims = {row["claim_ref"]: row for row in view["claims"]}
        self.assertEqual(2, len(claims))
        self.assertEqual("corroborated", claims["claim:acn:q3fy26:ai-demand"]["status"])
        self.assertEqual("proposed", claims["claim:acn:q3fy26:new-bookings"]["status"])
        bookings = claims["claim:acn:q3fy26:new-bookings"]
        self.assertEqual(
            self.fixture.fixture.bookings["content_hash"],
            bookings["claim_version_hash"],
        )
        self.assertEqual(
            industry_fixture.NOW, bookings["latest_evidence_retrieved_at"]
        )
        self.assertEqual(["sec-filing-exhibit"], bookings["source_types"])

        self.assertEqual(1, len(view["open_questions"]))
        self.assertEqual(
            "Will AI demand convert into durable bookings?",
            view["open_questions"][0]["question"],
        )
        self.assertEqual([], view["impact"])
        self.assertEqual(self.issue["id"], view["last_weekly_issue"]["issue_version_ref"])
        self.assertEqual(
            "brief_published", view["last_research_stop"]["kind"]
        )

    def test_unknown_company_is_empty_but_valid(self) -> None:
        view = build_company_research_view(self.store, "company:sec-cik:001688568")
        self.assertEqual("insufficient", view["thesis"]["status"])
        self.assertEqual([], view["claims"])
        self.assertEqual([], view["open_questions"])
        self.assertIsNone(view["built_as_of"])
        self.assertIsNone(view["last_research_stop"])
        body = {key: value for key, value in view.items() if key != "content_hash"}
        self.assertEqual(view["content_hash"], content_hash(body))

    def test_query_filters_and_validation(self) -> None:
        all_rows = query_company_research(self.store)
        self.assertEqual(2, len(all_rows))
        acn_rows = query_company_research(self.store, company_ref=ACN)
        self.assertEqual(2, len(acn_rows))
        bookings = query_company_research(self.store, aspect="metric:new-bookings")
        self.assertEqual(1, len(bookings))
        self.assertEqual("claim:acn:q3fy26:new-bookings", bookings[0]["claim_ref"])
        corroborated = query_company_research(self.store, status="corroborated")
        self.assertEqual(1, len(corroborated))
        quarter = query_company_research(self.store, period="FY2026Q3")
        self.assertEqual(2, len(quarter))
        empty = query_company_research(self.store, aspect="metric:missing")
        self.assertEqual([], empty)
        self.assertEqual(1, len(query_company_research(self.store, limit=1)))
        with self.assertRaises(CompanyResearchViewValidationError):
            query_company_research(self.store, status="unknown")
        with self.assertRaises(CompanyResearchViewValidationError):
            query_company_research(self.store, company_ref=" ")

    def test_view_claims_hand_off_to_context_materializer(self) -> None:
        view = build_company_research_view(self.store, ACN)
        specs = [
            {
                "kind": "claim",
                "ref": row["claim_version_ref"],
                "hash": row["claim_version_hash"],
                "priority": 10,
            }
            for row in view["claims"]
        ]
        observability = ObservabilityStore(self.store)
        materializer = ContextMaterializer(self.store, observability, None)
        plan = build_reference_fixture_plan(
            task_ref="work-order:view",
            task_hash=content_hash({"work_order_ref": "work-order:view"}),
            created_at=industry_fixture.NOW,
        )
        index = build_claim_index(ledger=self.store, created_at=industry_fixture.NOW)
        pack = materializer.build_authority_context_pack(
            specs, task_ref=plan["task_ref"], task_hash=plan["task_hash"],
            compiled_plan_ref=plan["id"], compiled_plan_hash=plan["content_hash"],
            claim_index_ref=index["id"], claim_index_hash=index["content_hash"],
            claim_index=index, created_at=industry_fixture.NOW,
            max_tokens=10_000, max_bytes=100_000,
        )
        result = materializer.materialize(
            pack, max_rendered_tokens=10_000, max_rendered_bytes=100_000,
            compiled_plan=plan, claim_index=index, created_at=industry_fixture.NOW,
        )
        manifest = result.manifest
        self.assertEqual(2, manifest["totals"]["selected_count"])
        self.assertEqual(0, manifest["totals"]["failure_count"])
        self.assertLessEqual(
            manifest["totals"]["rendered_tokens"], pack["budget"]["max_tokens"]
        )
        self.assertIn("claim:acn:q3fy26:new-bookings", result.rendered_text)
        self.assertIn("claim:acn:q3fy26:ai-demand", result.rendered_text)
        for spec in specs:
            self.assertIn(f'"ref":"{spec["ref"]}"', result.rendered_text)


if __name__ == "__main__":
    unittest.main()
