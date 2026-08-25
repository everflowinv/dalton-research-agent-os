"""Adversarial tests for answer routing and the S5 bounded refresh path."""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from dalton_core.agenda import AgendaStore
from dalton_core.answer_routing import (
    ANSWER_REFRESH_OUTPUT_CONTRACT_REF,
    ANSWER_REFRESH_VERIFIER_REF,
    AnswerRefreshControlPlane,
    AnswerRoutingAuthority,
    AnswerRoutingConflict,
    AnswerRoutingValidationError,
)
from dalton_core.bounded_planner_loop import (
    BoundedPlannerAuthority,
    BoundedPlannerControlPlane,
)
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
        self.bounded_control = BoundedPlannerControlPlane(
            self.bounded, self.observability, self.scheduler
        )
        self.industry = IndustryResearchAuthority(self.store)
        self.answers = AnswerRoutingAuthority(
            self.store, self.agenda, self.backlog, self.bounded, self.industry
        )
        self.refresh = AnswerRefreshControlPlane(
            self.answers, self.bounded_control
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
        refresh_template: dict | None = None,
        refresh_cost_units: int = 1,
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
                "enabled": refresh_template is not None,
                "max_cost_units": (
                    refresh_cost_units if refresh_template is not None else 0
                ),
                "probe_template_bindings": (
                    [] if refresh_template is None else [{
                        "ref": refresh_template["id"],
                        "hash": refresh_template["content_hash"],
                    }]
                ),
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

    def refresh_template(self, *, suffix: str = "1") -> dict:
        return self.bounded.publish_probe_template(
            f"probe-template:answer-refresh:{suffix}",
            capability_ref="capability:sec-read-only",
            operation="refresh_sec_filing",
            runtime_profile_ref="runtime:sec-read-only:0.1",
            parameter_contract={
                "allowed_fields": ["source_ref", "locator", "query_terms"],
                "required_fields": ["source_ref", "locator", "query_terms"],
                "constants": {"source_ref": "source:sec-edgar"},
            },
            output_contract_ref=ANSWER_REFRESH_OUTPUT_CONTRACT_REF,
            verifier_ref=ANSWER_REFRESH_VERIFIER_REF,
            permission_scope="public_sec_read",
            declared_side_effects=["read:public-http"],
            cost={"cost_units": 1, "max_attempts": 1, "max_seconds": 10},
            actor_ref="human:owner",
        )

    def refresh_loop(self, template: dict, *, suffix: str = "1") -> dict:
        question = self.backlog.question(
            question_ref_for("mandate:answer-routing", "wanhua", QUESTION)
        )
        return self.bounded.create_loop(
            f"bounded-loop:answer-refresh:{suffix}",
            question_version_ref=question["head"]["id"],
            template_bindings=[{
                "coverage_item_ref": "latest-reported-revenue",
                "template_version_ref": template["id"],
                "parameters": {
                    "source_ref": "source:sec-edgar",
                    "locator": "latest 10-Q revenue disclosure",
                    "query_terms": ["revenue", "2026 Q2"],
                },
            }],
            required_coverage_items=["latest-reported-revenue"],
            budget={
                "max_rounds": 1, "max_cost_units": 1, "max_seconds": 10,
            },
            actor_ref="human:owner",
        )

    def refresh_ready(self) -> tuple[dict, dict, dict]:
        mandate = self.govern()
        self.answer_question()
        template = self.refresh_template()
        loop = self.refresh_loop(template)
        self.publish_answer_policy(
            mandate, max_age_days=1, refresh_template=template
        )
        return self.answers.subjects()[0], template, loop

    def complete_refresh_work(
        self,
        dispatch: dict,
        matches: list[dict],
        *,
        scheduler: Scheduler | None = None,
    ) -> dict:
        scheduler = scheduler or self.scheduler
        work_order_ref = dispatch["work_order_ref"]
        lease = scheduler.claim(
            "worker:answer-refresh", work_order_id=work_order_ref
        )
        self.assertIsNotNone(lease)
        result = {
            "schema_version": "0.1",
            "id": "result:answer-refresh:" + str(len(matches)),
            "created_at": "2026-08-25T12:00:00.000000+00:00",
            "work_order_ref": work_order_ref,
            "invocation_ref": "invocation:answer-refresh",
            "status": "succeeded",
            "outputs": {"matches": matches},
            "actual_side_effects": [],
            "usage_refs": [],
            "artifact_refs": [],
            "error": None,
            "metadata": {"fixture": True},
        }
        return scheduler.complete(
            work_order_ref,
            1,
            "worker:answer-refresh",
            lease["lease_token"],
            result,
            idempotency_key="complete:answer-refresh:" + str(len(matches)),
        )

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
        self.assertIn("refresh_disabled", routed["decision"]["reason_codes"])

    def test_stale_only_question_routes_to_exact_bounded_refresh(self) -> None:
        subject, template, loop = self.refresh_ready()
        before = self.table_counts()
        routed = self.answers.route(subject_binding=subject, question=QUESTION)
        decision = routed["decision"]
        self.assertEqual(decision["route"], "answer_after_refresh")
        self.assertEqual(
            decision["reason_codes"], ["stale_evidence", "refresh_required"]
        )
        self.assertTrue(decision["refresh_route_available"])
        self.assertEqual(
            decision["refresh_plan"]["template_version_ref"], template["id"]
        )
        self.assertEqual(
            decision["refresh_plan"]["loop_version_ref"], loop["id"]
        )
        self.assertEqual(decision["refresh_plan"]["cost_units"], 1)
        self.assertTrue(decision["refresh_plan"]["candidate_staging_required"])
        self.assertEqual(self.table_counts(), before)

    def test_refresh_dispatch_is_human_gated_budgeted_and_idempotent(self) -> None:
        subject, _, _ = self.refresh_ready()
        routed = self.answers.route(subject_binding=subject, question=QUESTION)
        decision = routed["decision"]
        with self.assertRaises(AnswerRoutingValidationError):
            self.refresh.dispatch(
                subject_binding=subject,
                question=QUESTION,
                route_decision_ref=decision["id"],
                route_decision_hash=decision["content_hash"],
                route_as_of=decision["created_at"],
                actor_ref="core:answer-refresh",
            )
        self.assertEqual(
            self.store.connection.execute(
                "SELECT COUNT(*) FROM answer_refresh_budget_reservations"
            ).fetchone()[0],
            0,
        )
        scheduler_before = self.store.connection.execute(
            "SELECT COUNT(*) FROM scheduler_work_orders"
        ).fetchone()[0]
        first = self.refresh.dispatch(
            subject_binding=subject,
            question=QUESTION,
            route_decision_ref=decision["id"],
            route_decision_hash=decision["content_hash"],
            route_as_of=decision["created_at"],
            actor_ref="human:owner",
        )
        second = self.refresh.dispatch(
            subject_binding=subject,
            question=QUESTION,
            route_decision_ref=decision["id"],
            route_decision_hash=decision["content_hash"],
            route_as_of=decision["created_at"],
            actor_ref="human:owner",
        )
        self.assertEqual(first["status"], "fresh")
        self.assertEqual(second["status"], "duplicate")
        self.assertEqual(
            first["dispatch"]["work_order_ref"],
            second["dispatch"]["work_order_ref"],
        )
        contracts = Path(__file__).resolve().parents[1] / "contracts"
        reservation = self.answers.refresh_reservation_for_decision(
            decision["id"]
        )
        dispatch = self.answers.refresh_dispatch_for_reservation(
            reservation["id"]
        )
        for name, wire in (
            ("answer-refresh-budget-reservation.schema.json", reservation),
            ("answer-refresh-dispatch-receipt.schema.json", dispatch),
        ):
            schema = json.loads((contracts / name).read_text(encoding="utf-8"))
            self.assertEqual(set(wire), set(schema["required"]), name)
            self.assertEqual(
                wire["schema_version"],
                schema["properties"]["schema_version"]["const"],
            )
        for table in (
            "answer_refresh_budget_reservations",
            "answer_refresh_dispatch_receipts",
            "bounded_research_plan_rounds",
        ):
            self.assertEqual(
                self.store.connection.execute(
                    f"SELECT COUNT(*) FROM {table}"
                ).fetchone()[0],
                1,
                table,
            )
        self.assertEqual(
            self.store.connection.execute(
                "SELECT COUNT(*) FROM scheduler_work_orders"
            ).fetchone()[0],
            scheduler_before + 1,
        )

    def test_refresh_dispatch_reuses_external_writer_scheduler(self) -> None:
        subject, _, _ = self.refresh_ready()
        external = Scheduler(Path(self.tmp.name) / "writer-scheduler.sqlite")
        self.addCleanup(external.close)
        external_control = BoundedPlannerControlPlane(
            self.bounded, self.observability, external
        )
        refresh = AnswerRefreshControlPlane(self.answers, external_control)
        routed = self.answers.route(subject_binding=subject, question=QUESTION)
        decision = routed["decision"]
        before_core = self.store.connection.execute(
            "SELECT COUNT(*) FROM scheduler_work_orders"
        ).fetchone()[0]
        first = refresh.dispatch(
            subject_binding=subject,
            question=QUESTION,
            route_decision_ref=decision["id"],
            route_decision_hash=decision["content_hash"],
            route_as_of=decision["created_at"],
            actor_ref="human:owner",
        )
        second = refresh.dispatch(
            subject_binding=subject,
            question=QUESTION,
            route_decision_ref=decision["id"],
            route_decision_hash=decision["content_hash"],
            route_as_of=decision["created_at"],
            actor_ref="human:owner",
        )
        self.assertEqual(first["status"], "fresh")
        self.assertEqual(second["status"], "duplicate")
        self.assertEqual(
            external.connection.execute(
                "SELECT COUNT(*) FROM scheduler_work_orders"
            ).fetchone()[0],
            1,
        )
        self.assertEqual(
            self.store.connection.execute(
                "SELECT COUNT(*) FROM scheduler_work_orders"
            ).fetchone()[0],
            before_core,
        )
        authority = external.work_order_authority(
            first["dispatch"]["work_order_ref"]
        )
        self.assertEqual(
            authority["work_order_hash"], first["dispatch"]["work_order_hash"]
        )
        self.complete_refresh_work(first["dispatch"], [], scheduler=external)
        finalized = refresh.finalize(
            first["dispatch"]["id"],
            candidate_staging_binding=None,
            actor_ref="human:owner",
        )
        self.assertEqual(finalized["status"], "fresh")
        self.assertEqual(
            finalized["outcome_receipt"]["terminal_state"],
            "coverage_complete_unobservable_candidate",
        )

    def test_refresh_dispatch_recovers_after_reserved_budget_crash(self) -> None:
        subject, _, _ = self.refresh_ready()
        routed = self.answers.route(subject_binding=subject, question=QUESTION)
        decision = routed["decision"]
        competing_as_of = (
            datetime.fromisoformat(decision["created_at"]) + timedelta(microseconds=1)
        ).isoformat()
        competing = self.answers.route(
            subject_binding=subject, question=QUESTION, as_of=competing_as_of
        )["decision"]
        self.assertNotEqual(competing["id"], decision["id"])

        def crash(seam: str) -> None:
            if seam == "after_reservation":
                raise RuntimeError("fixture crash after durable reservation")

        crashing = AnswerRefreshControlPlane(
            self.answers, self.bounded_control, fault_injector=crash
        )
        with self.assertRaises(RuntimeError):
            crashing.dispatch(
                subject_binding=subject,
                question=QUESTION,
                route_decision_ref=decision["id"],
                route_decision_hash=decision["content_hash"],
                route_as_of=decision["created_at"],
                actor_ref="human:owner",
            )
        with self.assertRaises(AnswerRoutingConflict):
            self.refresh.dispatch(
                subject_binding=subject,
                question=QUESTION,
                route_decision_ref=competing["id"],
                route_decision_hash=competing["content_hash"],
                route_as_of=competing["created_at"],
                actor_ref="human:owner",
            )
        recovered = self.refresh.dispatch(
            subject_binding=subject,
            question=QUESTION,
            route_decision_ref=decision["id"],
            route_decision_hash=decision["content_hash"],
            route_as_of=decision["created_at"],
            actor_ref="human:owner",
        )
        self.assertEqual(recovered["status"], "fresh")
        for table in (
            "answer_refresh_budget_reservations",
            "answer_refresh_dispatch_receipts",
            "bounded_research_plan_rounds",
        ):
            self.assertEqual(
                self.store.connection.execute(
                    f"SELECT COUNT(*) FROM {table}"
                ).fetchone()[0],
                1,
                table,
            )

    def test_refresh_no_match_finalizes_without_formal_writes(self) -> None:
        subject, _, _ = self.refresh_ready()
        routed = self.answers.route(subject_binding=subject, question=QUESTION)
        decision = routed["decision"]
        dispatched = self.refresh.dispatch(
            subject_binding=subject,
            question=QUESTION,
            route_decision_ref=decision["id"],
            route_decision_hash=decision["content_hash"],
            route_as_of=decision["created_at"],
            actor_ref="human:owner",
        )
        self.complete_refresh_work(dispatched["dispatch"], [])
        before = {
            table: self.store.connection.execute(
                f"SELECT COUNT(*) FROM {table}"
            ).fetchone()[0]
            for table in ("evidence_versions", "claim_versions", "thesis_versions")
        }
        finalized = self.refresh.finalize(
            dispatched["dispatch"]["id"],
            candidate_staging_binding=None,
            actor_ref="human:owner",
        )
        receipt = finalized["outcome_receipt"]
        self.assertEqual(
            receipt["terminal_state"],
            "coverage_complete_unobservable_candidate",
        )
        self.assertEqual(receipt["outcome_kind"], "not_found_in_scope")
        self.assertIsNone(receipt["candidate_staging_binding"])
        self.assertEqual(receipt["formal_authority_writes"], 0)
        persisted = self.answers.refresh_outcome_for_dispatch(
            dispatched["dispatch"]["id"]
        )
        schema = json.loads(
            (
                Path(__file__).resolve().parents[1]
                / "contracts/answer-refresh-outcome-receipt.schema.json"
            ).read_text(encoding="utf-8")
        )
        self.assertEqual(set(persisted), set(schema["required"]))
        for table, count in before.items():
            self.assertEqual(
                self.store.connection.execute(
                    f"SELECT COUNT(*) FROM {table}"
                ).fetchone()[0],
                count,
            )

    def test_refresh_finalize_recovers_after_terminal_before_receipt(self) -> None:
        subject, _, loop = self.refresh_ready()
        routed = self.answers.route(subject_binding=subject, question=QUESTION)
        decision = routed["decision"]
        dispatched = self.refresh.dispatch(
            subject_binding=subject,
            question=QUESTION,
            route_decision_ref=decision["id"],
            route_decision_hash=decision["content_hash"],
            route_as_of=decision["created_at"],
            actor_ref="human:owner",
        )
        self.complete_refresh_work(dispatched["dispatch"], [])

        def crash(seam: str) -> None:
            if seam == "after_terminal":
                raise RuntimeError("fixture crash after bounded terminal")

        crashing = AnswerRefreshControlPlane(
            self.answers, self.bounded_control, fault_injector=crash
        )
        with self.assertRaises(RuntimeError):
            crashing.finalize(
                dispatched["dispatch"]["id"],
                candidate_staging_binding=None,
                actor_ref="human:owner",
            )
        self.assertIsNotNone(self.bounded.terminal(loop["id"]))
        self.assertIsNone(
            self.answers.refresh_outcome_for_dispatch(
                dispatched["dispatch"]["id"]
            )
        )
        recovered = self.refresh.finalize(
            dispatched["dispatch"]["id"],
            candidate_staging_binding=None,
            actor_ref="human:owner",
        )
        self.assertEqual(recovered["status"], "fresh")
        self.assertEqual(
            recovered["outcome_receipt"]["terminal_state"],
            "coverage_complete_unobservable_candidate",
        )

    def test_observed_refresh_cannot_bypass_candidate_staging(self) -> None:
        subject, _, loop = self.refresh_ready()
        routed = self.answers.route(subject_binding=subject, question=QUESTION)
        decision = routed["decision"]
        dispatched = self.refresh.dispatch(
            subject_binding=subject,
            question=QUESTION,
            route_decision_ref=decision["id"],
            route_decision_hash=decision["content_hash"],
            route_as_of=decision["created_at"],
            actor_ref="human:owner",
        )
        self.complete_refresh_work(
            dispatched["dispatch"],
            [{"source_location": "accession:answer-refresh#revenue"}],
        )
        with self.assertRaises(AnswerRoutingConflict):
            self.refresh.finalize(
                dispatched["dispatch"]["id"],
                candidate_staging_binding=None,
                actor_ref="human:owner",
            )
        self.assertEqual(self.bounded.outcomes(loop["id"]), [])
        self.assertIsNone(self.bounded.terminal(loop["id"]))

    def test_refresh_day_budget_exhaustion_fails_closed(self) -> None:
        subject, template, _ = self.refresh_ready()
        routed = self.answers.route(subject_binding=subject, question=QUESTION)
        decision = routed["decision"]
        self.refresh.dispatch(
            subject_binding=subject,
            question=QUESTION,
            route_decision_ref=decision["id"],
            route_decision_hash=decision["content_hash"],
            route_as_of=decision["created_at"],
            actor_ref="human:owner",
        )
        self.refresh_loop(template, suffix="2")
        exhausted = self.answers.route(subject_binding=subject, question=QUESTION)
        self.assertEqual(
            exhausted["decision"]["route"], "recommend_agenda_item"
        )
        self.assertIn(
            "refresh_budget_exhausted", exhausted["decision"]["reason_codes"]
        )
        self.assertIsNone(exhausted["decision"]["refresh_plan"])

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

    def test_s5_policy_rejects_malformed_refresh_or_enabled_adhoc(self) -> None:
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

    def test_refresh_policy_rejects_probe_outside_closed_contract(self) -> None:
        mandate = self.govern()
        template = self.bounded.publish_probe_template(
            "probe-template:answer-refresh:wrong-verifier",
            capability_ref="capability:sec-read-only",
            operation="refresh_sec_filing",
            runtime_profile_ref="runtime:sec-read-only:0.1",
            parameter_contract={
                "allowed_fields": ["source_ref", "locator", "query_terms"],
                "required_fields": ["source_ref", "locator", "query_terms"],
                "constants": {"source_ref": "source:sec-edgar"},
            },
            output_contract_ref=ANSWER_REFRESH_OUTPUT_CONTRACT_REF,
            verifier_ref="verifier:unapproved",
            permission_scope="public_sec_read",
            declared_side_effects=["read:public-http"],
            cost={"cost_units": 1, "max_attempts": 1, "max_seconds": 10},
            actor_ref="human:owner",
        )
        with self.assertRaises(AnswerRoutingConflict):
            self.publish_answer_policy(
                mandate,
                refresh_template=template,
                idempotency_key="answer-policy:wrong-verifier",
                version_id="answer-policy-version:wrong-verifier",
            )


if __name__ == "__main__":
    unittest.main()
