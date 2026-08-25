"""Adversarial tests for the S4 read-only answer-routing authority."""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from dalton_core.agenda import AgendaStore
from dalton_core.answer_routing import (
    AnswerRoutingAuthority,
    AnswerRoutingConflict,
    AnswerRoutingValidationError,
)
from dalton_core.bounded_planner_loop import BoundedPlannerAuthority
from dalton_core.industry_research import IndustryResearchAuthority
from dalton_core.observability import ObservabilityStore
from dalton_core.research_plan import (
    ResearchPlanAuthority,
    ResearchPlanControlPlane,
)
from dalton_core.research_question_backlog import (
    ResearchQuestionBacklog,
    question_ref_for,
)
from dalton_core.scheduler import Scheduler
from dalton_core.store import DaltonStore
from tests.agenda_fixtures import register_perception


NOW = "2026-08-14T10:00:00.000000+00:00"
SIX_DAYS_LATER = "2026-08-20T10:00:00.000000+00:00"
LATER = "2026-09-14T10:00:00.000000+00:00"
QUESTION = "What is Wanhua's reported 2026 Q2 revenue?"


def agenda_policy() -> dict:
    return {
        "schema_version": "0.1",
        "enabled": True,
        "selected_count": 1,
        "max_model_calls_per_cycle": 1,
        "max_daily_cycles": 1,
        "max_daily_cost_usd": 0.5,
        "max_monthly_cost_usd": 10.0,
        "max_input_tokens": 8000,
        "max_output_tokens": 2000,
        "feature_weights": {
            "mandate_relevance": 4,
            "catalyst_urgency": 3,
            "evidence_staleness": 2,
            "decision_impact": 4,
        },
        "trial_company_refs": ["wanhua"],
        "cutover_enabled": False,
        "cutover_acceptance_threshold": None,
    }


def invocation(identifier: str) -> dict:
    return {
        "schema_version": "0.1",
        "id": identifier,
        "created_at": NOW,
        "work_order_ref": "work-order:" + identifier,
        "profile_ref": "profile:" + identifier,
        "granularity": "task",
        "capability": "research",
        "provider": "test-provider",
        "model": "test-model",
        "model_family": "test-family",
        "runtime_ref": "runtime:test",
        "actor_ref": "researcher:test",
        "usage": {"tokens": 1},
        "input_refs": [],
        "output_refs": [],
        "started_at": NOW,
        "completed_at": NOW,
        "side_effects": [],
        "parent_ref": None,
    }


class AnswerRoutingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.store = DaltonStore(Path(self.tmp.name) / "core.sqlite")
        self.observability = ObservabilityStore(self.store)
        self.agenda = AgendaStore(self.store)
        self.backlog = ResearchQuestionBacklog(self.store)
        self.plans = ResearchPlanAuthority(self.store)
        self.scheduler = Scheduler(connection=self.store.connection)
        self.plan_control = ResearchPlanControlPlane(
            self.plans, self.backlog, self.observability, self.scheduler
        )
        self.bounded = BoundedPlannerAuthority(self.store)
        self.industry = IndustryResearchAuthority(self.store)
        self.answers = AnswerRoutingAuthority(
            self.store, self.agenda, self.backlog, self.bounded, self.industry
        )
        self.addCleanup(self.store.close)
        self.addCleanup(self.tmp.cleanup)

    def table_counts(self) -> dict[str, int]:
        return {
            row["name"]: self.store.connection.execute(
                f"SELECT COUNT(*) FROM {row['name']}"
            ).fetchone()[0]
            for row in self.store.connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
        }

    def govern(self) -> dict:
        self.agenda.create_policy(
            agenda_policy(),
            effective_from=NOW,
            effective_until=LATER,
            actor_ref="human:owner",
            version_id="agenda-policy-version:answer-routing",
            idempotency_key="agenda-policy:answer-routing",
        )
        mandate = self.agenda.create_mandate(
            "mandate:answer-routing",
            objective="Answer admitted Wanhua research questions",
            scope_refs=["wanhua"],
            constraints={"mode": "shadow"},
            success_criteria={"formal_claims_required": True},
            effective_from=NOW,
            effective_until=LATER,
            actor_ref="human:owner",
            version_id="mandate-version:answer-routing",
            idempotency_key="mandate:answer-routing",
        )
        self.agenda.set_pause(
            False,
            reason="owner approved answer-routing test",
            actor_ref="human:owner",
            version_id="agenda-control-version:answer-routing",
            idempotency_key="agenda-resume:answer-routing",
        )
        return mandate

    def publish_answer_policy(
        self,
        mandate: dict,
        *,
        max_age_days: int = 30,
        version_id: str = "answer-policy-version:1",
        prior_version_ref: str | None = None,
        idempotency_key: str = "answer-policy:1",
    ) -> dict:
        return self.answers.publish_policy(
            policy_ref="answer-policy:wanhua",
            mandate_version_ref=mandate["id"],
            mandate_version_hash=mandate["content_hash"],
            thresholds={
                "min_driver_coverage_bps": 0,
                "max_evidence_age_days_by_source_type": {
                    "sec-filing": max_age_days,
                },
                "allowed_contested_claims": 0,
                "allowed_open_questions": 0,
                "allowed_unobservable_terminals": 0,
                "min_formal_claims": 1,
                "min_formal_evidence": 1,
            },
            refresh_route={
                "enabled": False,
                "max_cost_units": 0,
                "probe_template_bindings": [],
            },
            adhoc_research_route={
                "enabled": False,
                "max_cost_units": 0,
                "max_rounds": 0,
            },
            effective_from=NOW,
            effective_until=LATER,
            actor_ref="human:owner",
            version_id=version_id,
            prior_version_ref=prior_version_ref,
            idempotency_key=idempotency_key,
        )

    def answer_question(self) -> tuple[dict, dict, dict]:
        snapshot = register_perception(
            self.agenda, "perception:answer-routing", company="wanhua"
        )
        started = self.agenda.start_cycle(
            "agenda-cycle-key:answer-routing",
            perception_snapshot_ref=snapshot["snapshot_id"],
            perception_snapshot_hash=snapshot["content_hash"],
            mandate_version_ref="mandate-version:answer-routing",
            policy_version_ref="agenda-policy-version:answer-routing",
            company_ref="wanhua",
            actor_ref="core",
            cycle_id="agenda-cycle:answer-routing",
            idempotency_key="agenda-cycle:answer-routing",
        )
        self.agenda.add_candidates(
            started["cycle_id"],
            candidates=[{
                "candidate_id": "candidate:answer-routing",
                "company_ref": "wanhua",
                "question": QUESTION,
                "answer_criteria": "Use one formal reported revenue claim",
                "features": {
                    "mandate_relevance": 3,
                    "catalyst_urgency": 3,
                    "evidence_staleness": 2,
                    "decision_impact": 3,
                },
                "rationale": "test candidate",
                "source_refs": ["source:sec-filing"],
            }],
            actor_ref="core",
            idempotency_key="agenda-candidates:answer-routing",
        )
        decision = self.agenda.decide_cycle(
            started["cycle_id"],
            actor_ref="core",
            decision_id="agenda-decision:answer-routing",
            idempotency_key="agenda-decision:answer-routing",
        )
        recorded = self.backlog.record_question(
            mandate_version_ref="mandate-version:answer-routing",
            company_ref="wanhua",
            question=QUESTION,
            answer_criteria="Use one formal reported revenue claim",
            source_refs=["source:sec-filing"],
            actor_ref="core",
            idempotency_key="question:answer-routing",
        )
        question_ref = recorded["question_ref"]
        self.backlog.select_question(
            question_ref=question_ref,
            decision_ref=decision["id"],
            actor_ref="core",
            idempotency_key="question-select:answer-routing",
        )
        question = self.backlog.question(question_ref)
        plan = self.plans.create_plan(
            question_ref=question_ref,
            question_version_ref=question["head"]["id"],
            decision_ref=decision["id"],
            issuer_cik="320193",
            form="10-Q",
            filing_date_from="2026-01-01",
            filing_date_to="2026-08-14",
            actor_ref="core:planner",
            idempotency_key="research-plan:answer-routing",
        )
        self.plans.approve_plan(
            plan_version_ref=plan["plan_version_ref"],
            decision="accepted",
            reason="test approval",
            actor_ref="human:owner",
            idempotency_key="research-plan-approval:answer-routing",
        )
        self.plan_control.start_plan(
            plan_version_ref=plan["plan_version_ref"],
            actor_ref="core:planner",
            idempotency_key="research-plan-start:answer-routing",
        )
        self.store.register_invocation(invocation("invocation:answer-routing"))
        evidence = self.store.register_evidence({
            "id": "evidence-version:answer-routing",
            "evidence_ref": "evidence:answer-routing",
            "created_at": NOW,
            "source_type": "sec-filing",
            "source_ref": "sec:wanhua:2026-q2",
            "retrieved_at": NOW,
            "valid_until": LATER,
            "source_lineage": ["sec:wanhua:2026-q2"],
            "independence_group": "sec:wanhua",
            "actor_ref": "researcher:test",
        })
        claim = self.store.register_claim({
            "id": "claim-version:answer-routing",
            "claim_ref": "claim:answer-routing",
            "created_at": NOW,
            "subject_ref": "wanhua",
            "metric_or_aspect": "revenue",
            "period": "2026Q2",
            "basis": "reported",
            "normalized_statement": "Wanhua reported 2026 Q2 revenue.",
            "claim_kind": "quantitative",
            "value": 1.0,
            "unit": "CNY",
            "producer_invocation_refs": ["invocation:answer-routing"],
            "actor_ref": "researcher:test",
        })
        relation = self.store.relate_evidence({
            "id": "evidence-relation:answer-routing",
            "evidence_version_ref": evidence["evidence_version_id"],
            "claim_version_ref": claim["claim_version_id"],
            "relation": "supports",
            "actor_ref": "researcher:test",
        })
        self.backlog.answer_question(
            question_ref=question_ref,
            claim_version_refs=[claim["claim_version_id"]],
            actor_ref="core",
            idempotency_key="question-answer:answer-routing",
        )
        self.assertEqual(
            question_ref,
            question_ref_for("mandate:answer-routing", "wanhua", QUESTION),
        )
        return claim, evidence, relation

    def ready(self, *, max_age_days: int = 30) -> dict:
        mandate = self.govern()
        self.publish_answer_policy(mandate, max_age_days=max_age_days)
        self.answer_question()
        return self.answers.subjects(as_of=NOW)[0]

    def test_exact_answered_question_routes_direct_without_writes(self) -> None:
        subject = self.ready()
        before = self.table_counts()
        routed = self.answers.route(
            subject_binding=subject, question=QUESTION, as_of=NOW
        )
        self.assertEqual(routed["decision"]["route"], "answer_direct")
        self.assertEqual(
            routed["decision"]["reason_codes"], ["answer_direct_ready"]
        )
        self.assertEqual(
            routed["decision"]["direct_claim_refs"],
            ["claim-version:answer-routing"],
        )
        self.assertEqual(
            routed["decision"]["direct_evidence_refs"],
            ["evidence-version:answer-routing"],
        )
        self.assertFalse(routed["decision"]["write_performed"])
        self.assertEqual(self.table_counts(), before)
        contracts = Path(__file__).resolve().parents[1] / "contracts"
        for name, wire in (
            (
                "answer-sufficiency-policy-version.schema.json",
                self.answers.policy("answer-policy-version:1"),
            ),
            ("answer-context-pack.schema.json", routed["context_pack"]),
            ("answer-route-decision.schema.json", routed["decision"]),
        ):
            schema = json.loads((contracts / name).read_text(encoding="utf-8"))
            self.assertEqual(set(wire), set(schema["required"]), name)
            self.assertEqual(
                wire["schema_version"],
                schema["properties"]["schema_version"]["const"],
            )

    def test_unmatched_question_recommends_agenda_without_writes(self) -> None:
        subject = self.ready()
        before = self.table_counts()
        routed = self.answers.route(
            subject_binding=subject,
            question="What should we research next?",
            as_of=NOW,
        )
        self.assertEqual(
            routed["decision"]["route"], "recommend_agenda_item"
        )
        self.assertIn(
            "question_not_admitted", routed["decision"]["reason_codes"]
        )
        self.assertFalse(
            routed["decision"]["agenda_recommendation"]["write_performed"]
        )
        self.assertEqual(self.table_counts(), before)

    def test_stale_evidence_fails_closed(self) -> None:
        subject = self.ready(max_age_days=1)
        routed = self.answers.route(
            subject_binding=subject, question=QUESTION, as_of=SIX_DAYS_LATER
        )
        self.assertEqual(
            routed["decision"]["route"], "recommend_agenda_item"
        )
        self.assertIn("stale_evidence", routed["decision"]["reason_codes"])

    def test_policy_rotation_invalidates_old_subject_binding(self) -> None:
        subject = self.ready()
        mandate = self.agenda.active_mandates(at=NOW)[0]
        self.publish_answer_policy(
            mandate,
            version_id="answer-policy-version:2",
            prior_version_ref="answer-policy-version:1",
            idempotency_key="answer-policy:2",
        )
        with self.assertRaises(AnswerRoutingConflict):
            self.answers.route(
                subject_binding=subject, question=QUESTION, as_of=NOW
            )
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.connection.execute(
                "UPDATE answer_sufficiency_policy_pointer "
                "SET content_hash=? WHERE mandate_ref=?",
                ("f" * 64, "mandate:answer-routing"),
            )

    def test_s4_policy_rejects_enabled_refresh_or_adhoc_routes(self) -> None:
        mandate = self.govern()
        with self.assertRaises(AnswerRoutingValidationError):
            self.answers.publish_policy(
                policy_ref="answer-policy:wanhua",
                mandate_version_ref=mandate["id"],
                mandate_version_hash=mandate["content_hash"],
                thresholds={
                    "min_driver_coverage_bps": 0,
                    "max_evidence_age_days_by_source_type": {"sec-filing": 30},
                    "allowed_contested_claims": 0,
                    "allowed_open_questions": 0,
                    "allowed_unobservable_terminals": 0,
                    "min_formal_claims": 1,
                    "min_formal_evidence": 1,
                },
                refresh_route={
                    "enabled": True,
                    "max_cost_units": 1,
                    "probe_template_bindings": [],
                },
                adhoc_research_route={
                    "enabled": False,
                    "max_cost_units": 0,
                    "max_rounds": 0,
                },
                effective_from=NOW,
                effective_until=LATER,
                actor_ref="human:owner",
                version_id="answer-policy-version:invalid",
                prior_version_ref=None,
                idempotency_key="answer-policy:invalid",
            )


if __name__ == "__main__":
    unittest.main()
