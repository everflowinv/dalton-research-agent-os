"""Adversarial tests for the ResearchQuestionBacklog authority slice.

Covers: stable content/scope/mandate identity and cross-cycle dedup, the
frozen open -> selected -> planned -> in_progress -> answered|blocked|retired
machine (fail closed), exact AgendaDecision selection links, formal
ClaimVersion answer bindings, deterministic Mandate progress projection,
tamper detection, idempotency/conflict, rollback, and the absence of any
plan/auto-accept authority.
"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from dalton_core.agenda import AgendaStore
from dalton_core.observability import ObservabilityStore
from dalton_core.research_plan import (
    ResearchPlanAuthority,
    ResearchPlanConflict,
    ResearchPlanControlPlane,
)
from dalton_core.research_question_backlog import (
    QUESTION_STATES,
    ResearchQuestionBacklog,
    ResearchQuestionConflict,
    ResearchQuestionNotFound,
    ResearchQuestionValidationError,
    question_ref_for,
)
from dalton_core.research_review import validate_claim_version_v0_2
from dalton_core.scheduler import Scheduler
from dalton_core.store import DaltonStore, canonical_json, content_hash
from tests.agenda_fixtures import register_perception


NOW = "2026-08-14T10:00:00.000000+00:00"
LATER = "2026-09-14T10:00:00.000000+00:00"
BACKLOG_TABLES = {
    "backlog_questions",
    "backlog_question_versions",
    "backlog_question_pointer",
    "backlog_question_events",
    "backlog_selection_links",
    "backlog_answer_bindings",
    "backlog_idempotency",
}


def policy():
    return {
        "schema_version": "0.1",
        "enabled": True,
        "selected_count": 2,
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


def invocation(identifier):
    return {
        "schema_version": "0.1", "id": identifier, "created_at": NOW,
        "work_order_ref": "work-order:" + identifier, "profile_ref": "profile:" + identifier,
        "granularity": "task", "capability": "research", "provider": identifier,
        "model": "model-" + identifier, "model_family": "family-" + identifier,
        "runtime_ref": "runtime", "actor_ref": "researcher",
        "usage": {"tokens": 1}, "input_refs": [], "output_refs": [],
        "started_at": NOW, "completed_at": None, "side_effects": [],
        "parent_ref": None,
    }


class ResearchQuestionBacklogTests(unittest.TestCase):
    def setUp(self):
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
        self.addCleanup(self.store.close)
        self.addCleanup(self.tmp.cleanup)

    def govern(self, *, mandate_ref="mandate:coverage-quality", scope_refs=("wanhua",), version_id="mandate-version:1"):
        p = self.agenda.create_policy(
            policy(), effective_from=NOW, effective_until=LATER,
            actor_ref="human:owner", version_id="agenda-policy-version:1",
            idempotency_key="policy:1",
        )
        m = self.agenda.create_mandate(
            mandate_ref,
            objective="Find the most decision-useful unanswered question",
            scope_refs=list(scope_refs), constraints={"mode": "shadow"},
            success_criteria={"human_feedback_required": True},
            effective_from=NOW, effective_until=LATER,
            actor_ref="human:owner", version_id=version_id,
            idempotency_key="mandate:" + version_id,
        )
        self.agenda.set_pause(
            False, reason="owner approved Phase 1 shadow", actor_ref="human:owner",
            version_id="agenda-control-version:2", idempotency_key="resume:1",
        )
        return p, m

    def perception(self, snapshot_id, *, company="wanhua"):
        return register_perception(self.agenda, snapshot_id, company=company)

    def start_decided_cycle(self, cycle_key, cycle_id, decision_id, *, questions, mandate_version_ref="mandate-version:1", snapshot_id=None):
        snapshot = self.perception(snapshot_id or ("perception:" + cycle_id))
        started = self.agenda.start_cycle(
            cycle_key,
            perception_snapshot_ref=snapshot["snapshot_id"],
            perception_snapshot_hash=snapshot["content_hash"],
            mandate_version_ref=mandate_version_ref,
            policy_version_ref="agenda-policy-version:1",
            company_ref="wanhua", actor_ref="core", cycle_id=cycle_id,
            idempotency_key="cycle:" + cycle_id,
        )
        candidates = []
        for index, item in enumerate(questions):
            candidates.append({
                "candidate_id": f"candidate:{cycle_id}:{index}",
                "company_ref": "wanhua",
                "question": item["question"],
                "answer_criteria": item["answer_criteria"],
                "features": {"mandate_relevance": 3, "catalyst_urgency": 2, "evidence_staleness": 1, "decision_impact": 3},
                "rationale": "display only",
                "source_refs": item.get("source_refs", ["evidence:a"]),
            })
        self.agenda.add_candidates(
            started["cycle_id"], candidates=candidates, actor_ref="core",
            idempotency_key="candidates:" + cycle_id,
        )
        decision = self.agenda.decide_cycle(
            started["cycle_id"], actor_ref="core", decision_id=decision_id,
            idempotency_key="decision:" + decision_id,
        )
        return started, decision

    def record(self, *, question, answer_criteria, mandate_version_ref="mandate-version:1", company_ref="wanhua", source_refs=None, actor_ref="core", idempotency_key="record:1"):
        return self.backlog.record_question(
            mandate_version_ref=mandate_version_ref, company_ref=company_ref,
            question=question, answer_criteria=answer_criteria,
            source_refs=source_refs or ["evidence:a"], actor_ref=actor_ref,
            idempotency_key=idempotency_key,
        )

    def plan_selected_question(self, question_ref, *, key="plan:helper"):
        question = self.backlog.question(question_ref)
        selection = question["selection_links"][-1]
        return self.plans.create_plan(
            question_ref=question_ref,
            question_version_ref=question["head"]["id"],
            decision_ref=selection["decision_ref"],
            issuer_cik="320193",
            form="10-Q",
            filing_date_from="2026-01-01",
            filing_date_to="2026-08-15",
            actor_ref="core:planner",
            idempotency_key=key,
        )

    def start_planned_question(self, plan, *, key="start:helper"):
        self.plans.approve_plan(
            plan_version_ref=plan["plan_version_ref"],
            decision="accepted",
            reason="test plan approval",
            actor_ref="human:owner",
            idempotency_key="approve:" + key,
        )
        return self.plan_control.start_plan(
            plan_version_ref=plan["plan_version_ref"],
            actor_ref="core:planner",
            idempotency_key=key,
        )

    def plan_and_start_question(self, question_ref, *, suffix="helper"):
        plan = self.plan_selected_question(question_ref, key="plan:" + suffix)
        started = self.start_planned_question(plan, key="start:" + suffix)
        return plan, started

    def formal_claim(self, claim_ref, *, value=1.5, kind="quantitative"):
        producer_id = "producer-" + claim_ref
        self.store.register_invocation(invocation(producer_id))
        return self.store.register_claim({
            "claim_ref": claim_ref, "subject_ref": "wanhua",
            "metric_or_aspect": "revenue", "period": "2026Q2",
            "basis": "reported", "normalized_statement": "claim " + claim_ref,
            "claim_kind": kind, "value": value, "unit": "USD",
            "producer_invocation_refs": [producer_id], "actor_ref": "researcher",
        })

    def formal_claim_v2(self, claim_ref, *, version=1):
        wire = {
            "schema_version": "0.2",
            "id": "claim-version:v2:" + claim_ref,
            "created_at": NOW,
            "claim_ref": claim_ref,
            "version": version,
            "subject_ref": "wanhua",
            "metric_or_aspect": "revenue",
            "period": {"start": "2026-01-01", "end": "2026-06-30"},
            "basis": "reported",
            "normalized_statement": "claim v2 " + claim_ref,
            "claim_kind": "quantitative",
            "value": "1.50",
            "unit": "USD",
            "currency": "USD",
            "scale": "one",
            "producer_execution_refs": ["execution:v2:" + claim_ref],
            "semantic_review_ref": "review:v2:" + claim_ref,
            "semantic_review_hash": "a" * 64,
            "candidate_origin_ref": "candidate-claim:v2:" + claim_ref,
            "candidate_origin_hash": "b" * 64,
            "actor_ref": "human:reviewer",
            "prior_version_ref": None,
        }
        wire["content_hash"] = content_hash(wire)
        wire = validate_claim_version_v0_2(wire)
        with self.store._transaction() as cur:
            cur.execute(
                "INSERT INTO claim_versions(claim_version_id,claim_ref,version_number,"
                "claim_json,content_hash,prior_version_id,created_at) VALUES(?,?,?,?,?,?,?)",
                (wire["id"], wire["claim_ref"], wire["version"], canonical_json(wire),
                 wire["content_hash"], wire["prior_version_ref"], wire["created_at"]),
            )
        return wire

    def table_counts(self):
        counts = {}
        for row in self.store.connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall():
            counts[row["name"]] = self.store.connection.execute(
                f"SELECT COUNT(*) FROM {row['name']}"
            ).fetchone()[0]
        return counts

    def assert_only_backlog_tables_changed(self, before):
        after = self.table_counts()
        changed = {name for name in after if after[name] != before.get(name, 0)}
        self.assertTrue(
            changed <= BACKLOG_TABLES,
            f"non-backlog authorities changed: {sorted(changed - BACKLOG_TABLES)}",
        )

    # ------------------------------------------------------------------ #
    # Happy path and identity
    # ------------------------------------------------------------------ #

    def test_full_lifecycle_to_answered(self):
        self.govern()
        started, decision = self.start_decided_cycle(
            "agenda:cycle:1", "agenda-cycle:1", "decision:1",
            questions=[{"question": "Question A?", "answer_criteria": "Answer A"}],
        )
        recorded = self.record(
            question="Question A?", answer_criteria="Answer A",
            idempotency_key="record:lifecycle",
        )
        self.assertEqual(recorded["status"], "fresh")
        self.assertEqual(recorded["state"], "open")
        question_ref = recorded["question_ref"]
        self.assertEqual(question_ref, question_ref_for("mandate:coverage-quality", "wanhua", "Question A?"))

        selected = self.backlog.select_question(
            question_ref=question_ref, decision_ref=decision["id"],
            actor_ref="core", idempotency_key="select:lifecycle",
        )
        self.assertEqual(selected["state"], "selected")
        link = selected["selection_link"]
        self.assertEqual(link["decision_ref"], decision["id"])
        self.assertEqual(link["cycle_ref"], started["cycle_id"])
        self.assertEqual(link["decision_hash"], decision["content_hash"])
        self.assertEqual(link["candidate_ref"], decision["selected_candidate_refs"][0])

        planned = self.plan_selected_question(question_ref, key="plan:lifecycle")
        self.assertEqual(planned["question_state"], "planned")

        started_exec = self.start_planned_question(
            planned, key="start:lifecycle"
        )
        self.assertEqual(started_exec["question_state"], "in_progress")

        claim = self.formal_claim("claim:lifecycle")
        claim_two = self.formal_claim("claim:lifecycle:2")
        answered = self.backlog.answer_question(
            question_ref=question_ref,
            claim_version_refs=[claim["claim_version_id"], claim_two["claim_version_id"]],
            actor_ref="core", idempotency_key="answer:lifecycle",
        )
        self.assertEqual(answered["state"], "answered")
        self.assertEqual(len(answered["answer_bindings"]), 2)
        hashes = {b["claim_version_ref"]: b["claim_version_hash"] for b in answered["answer_bindings"]}
        self.assertEqual(hashes[claim["claim_version_id"]], claim["content_hash"])
        self.assertEqual(hashes[claim_two["claim_version_id"]], claim_two["content_hash"])

        question = self.backlog.question(question_ref)
        self.assertEqual(question["state"], "answered")
        self.assertEqual([e["state"] for e in question["events"]],
                         ["open", "selected", "planned", "in_progress", "answered"])
        self.assertEqual(len(question["selection_links"]), 1)
        self.assertEqual(len(question["answer_bindings"]), 2)
        # The AgendaDecision is not an answer: no binding may reference it.
        for binding in question["answer_bindings"]:
            self.assertNotEqual(binding["claim_version_ref"], decision["id"])

    def test_identity_is_content_scope_mandate_bound(self):
        self.govern()
        self.agenda.create_mandate(
            "mandate:other", objective="Other", scope_refs=["other-co"],
            constraints={}, success_criteria={}, effective_from=NOW,
            effective_until=LATER, actor_ref="human:owner",
            version_id="mandate-version:2", idempotency_key="mandate:other-identity",
        )
        base = self.record(
            question="Q?", answer_criteria="A",
            idempotency_key="record:identity:base",
        )["question_ref"]
        other_text = self.record(
            question="Q??", answer_criteria="A",
            idempotency_key="record:identity:text",
        )["question_ref"]
        other_company = self.backlog.record_question(
            mandate_version_ref="mandate-version:2", company_ref="other-co",
            question="Q?", answer_criteria="A", source_refs=["s"],
            actor_ref="core", idempotency_key="record:identity:company",
        )["question_ref"]
        other_mandate = self.backlog.record_question(
            mandate_version_ref="mandate-version:2", company_ref="other-co",
            question="Q??", answer_criteria="A", source_refs=["s"],
            actor_ref="core", idempotency_key="record:identity:mandate",
        )["question_ref"]
        self.assertNotEqual(base, other_text)
        self.assertNotEqual(base, other_company)
        self.assertNotEqual(base, other_mandate)
        self.assertNotEqual(other_company, other_mandate)
        self.assertEqual(
            self.record(question="Q?", answer_criteria="A", idempotency_key="record:identity:again")["question_ref"],
            base,
        )

    def test_0_2_claim_binding(self):
        self.govern()
        self.start_decided_cycle(
            "agenda:cycle:v2", "agenda-cycle:v2", "decision:v2",
            questions=[{"question": "Q v2?", "answer_criteria": "A v2"}],
        )
        self.record(question="Q v2?", answer_criteria="A v2", idempotency_key="record:v2")
        question_ref = question_ref_for("mandate:coverage-quality", "wanhua", "Q v2?")
        self.backlog.select_question(
            question_ref=question_ref, decision_ref="decision:v2",
            actor_ref="core", idempotency_key="select:v2",
        )
        self.plan_and_start_question(question_ref, suffix="v2")
        claim = self.formal_claim_v2("claim:v2")
        answered = self.backlog.answer_question(
            question_ref=question_ref, claim_version_refs=[claim["id"]],
            actor_ref="core", idempotency_key="answer:v2",
        )
        self.assertEqual(answered["state"], "answered")
        self.assertEqual(answered["answer_bindings"][0]["claim_version_hash"], claim["content_hash"])

    # ------------------------------------------------------------------ #
    # Cross-cycle dedup and idempotency
    # ------------------------------------------------------------------ #

    def test_duplicate_across_cycles_keeps_single_identity(self):
        self.govern()
        self.start_decided_cycle(
            "agenda:cycle:1", "agenda-cycle:1", "decision:1",
            questions=[{"question": "Same Q?", "answer_criteria": "Same A"}],
        )
        first = self.record(question="Same Q?", answer_criteria="Same A", idempotency_key="record:cycle1")
        # A later Agenda cycle proposes the identical question again.
        self.start_decided_cycle(
            "agenda:cycle:2", "agenda-cycle:2", "decision:2",
            questions=[{"question": "Same Q?", "answer_criteria": "Same A"}],
            snapshot_id="perception:2",
        )
        second = self.record(question="Same Q?", answer_criteria="Same A", idempotency_key="record:cycle2")
        self.assertEqual(second["status"], "duplicate")
        self.assertEqual(second["question_ref"], first["question_ref"])
        self.assertEqual(second["question_version_ref"], first["question_version_ref"])
        question = self.backlog.question(first["question_ref"])
        self.assertEqual(len(question["events"]), 1)
        self.assertEqual(question["head"]["question_ref"], first["question_ref"])
        self.assertEqual(question["head"]["version"], 1)
        # The identity row and version row were written exactly once.
        self.assertEqual(
            self.store.connection.execute(
                "SELECT COUNT(*) FROM backlog_questions WHERE question_ref=?",
                (first["question_ref"],),
            ).fetchone()[0], 1,
        )
        self.assertEqual(
            self.store.connection.execute(
                "SELECT COUNT(*) FROM backlog_question_versions WHERE question_ref=?",
                (first["question_ref"],),
            ).fetchone()[0], 1,
        )

    def test_duplicate_question_with_different_content_conflicts(self):
        self.govern()
        self.record(question="Q?", answer_criteria="A", idempotency_key="record:q:1")
        with self.assertRaises(ResearchQuestionConflict):
            self.record(question="Q?", answer_criteria="A DIFFERENT", idempotency_key="record:q:2")

    def test_duplicate_replay_after_terminal_state(self):
        self.govern()
        self.start_decided_cycle(
            "agenda:cycle:1", "agenda-cycle:1", "decision:1",
            questions=[{"question": "Q?", "answer_criteria": "A"}],
        )
        self.record(question="Q?", answer_criteria="A", idempotency_key="record:1")
        question_ref = question_ref_for("mandate:coverage-quality", "wanhua", "Q?")
        self.backlog.select_question(question_ref=question_ref, decision_ref="decision:1", actor_ref="core", idempotency_key="select:1")
        # Re-recording in a later cycle cannot resurrect or duplicate an in-flight question.
        replay = self.record(question="Q?", answer_criteria="A", idempotency_key="record:2")
        self.assertEqual(replay["status"], "duplicate")
        self.assertEqual(replay["state"], "selected")

    def test_idempotent_replay_and_conflict(self):
        self.govern()
        first = self.record(question="Q?", answer_criteria="A", idempotency_key="record:idem")
        replay = self.record(question="Q?", answer_criteria="A", idempotency_key="record:idem")
        self.assertEqual(replay["status"], "duplicate")
        self.assertEqual(replay["question_ref"], first["question_ref"])
        conflict = self.record(
            question="Q? DIFFERENT", answer_criteria="A",
            idempotency_key="record:idem",
        )
        self.assertEqual(conflict["status"], "conflict")
        self.assertEqual(
            self.store.connection.execute(
                "SELECT COUNT(*) FROM backlog_questions"
            ).fetchone()[0], 1,
        )

    def test_idempotency_key_is_canonicalized(self):
        self.govern()
        first = self.record(
            question="Q?", answer_criteria="A", idempotency_key="  record:trim  "
        )
        replay = self.record(
            question="Q?", answer_criteria="A", idempotency_key="record:trim"
        )
        self.assertEqual(first["question_ref"], replay["question_ref"])
        self.assertEqual(replay["status"], "duplicate")
        self.assertEqual(
            self.store.connection.execute(
                "SELECT idempotency_key FROM backlog_idempotency"
            ).fetchone()[0],
            "record:trim",
        )

    def test_replayed_answer_returns_original_bindings(self):
        self.govern()
        self.start_decided_cycle(
            "agenda:cycle:1", "agenda-cycle:1", "decision:1",
            questions=[{"question": "Q?", "answer_criteria": "A"}],
        )
        self.record(question="Q?", answer_criteria="A", idempotency_key="record:1")
        question_ref = question_ref_for("mandate:coverage-quality", "wanhua", "Q?")
        self.backlog.select_question(question_ref=question_ref, decision_ref="decision:1", actor_ref="core", idempotency_key="select:1")
        self.plan_and_start_question(question_ref, suffix="replay")
        claim = self.formal_claim("claim:replay")
        first = self.backlog.answer_question(
            question_ref=question_ref, claim_version_refs=[claim["claim_version_id"]],
            actor_ref="core", idempotency_key="answer:idem",
        )
        replay = self.backlog.answer_question(
            question_ref=question_ref, claim_version_refs=[claim["claim_version_id"]],
            actor_ref="core", idempotency_key="answer:idem",
        )
        self.assertEqual(replay["status"], "duplicate")
        self.assertEqual(
            replay["answer_bindings"][0]["claim_version_hash"],
            first["answer_bindings"][0]["claim_version_hash"],
        )
        self.assertEqual(
            self.store.connection.execute(
                "SELECT COUNT(*) FROM backlog_answer_bindings"
            ).fetchone()[0], 1,
        )

    # ------------------------------------------------------------------ #
    # State machine
    # ------------------------------------------------------------------ #

    def test_illegal_transitions_fail_closed(self):
        self.govern()
        self.record(question="Q?", answer_criteria="A", idempotency_key="record:1")
        question_ref = question_ref_for("mandate:coverage-quality", "wanhua", "Q?")
        with self.assertRaises(TypeError):
            self.backlog.plan_question(question_ref=question_ref, actor_ref="core")
        with self.assertRaises(TypeError):
            self.backlog.start_question(question_ref=question_ref, actor_ref="core")
        with self.assertRaises(ResearchQuestionConflict):
            self.backlog.block_question(question_ref=question_ref, reason="r", actor_ref="core")
        with self.assertRaises(ResearchQuestionConflict):
            self.backlog.retire_question(question_ref=question_ref, reason="r", actor_ref="core")
        with self.assertRaises(ResearchQuestionConflict):
            self.backlog.answer_question(question_ref=question_ref, claim_version_refs=["x"], actor_ref="core")
        # No event rows were written by any illegal transition.
        self.assertEqual(
            self.store.connection.execute(
                "SELECT COUNT(*) FROM backlog_question_events"
            ).fetchone()[0], 1,
        )

    def test_out_of_order_and_repeat_transitions_fail_closed(self):
        self.govern()
        self.start_decided_cycle(
            "agenda:cycle:1", "agenda-cycle:1", "decision:1",
            questions=[{"question": "Q?", "answer_criteria": "A"}],
        )
        self.record(question="Q?", answer_criteria="A", idempotency_key="record:1")
        question_ref = question_ref_for("mandate:coverage-quality", "wanhua", "Q?")
        self.backlog.select_question(question_ref=question_ref, decision_ref="decision:1", actor_ref="core", idempotency_key="select:1")
        # Re-select by a second decision is an illegal out-of-order transition.
        with self.assertRaises(ResearchQuestionConflict):
            self.backlog.select_question(question_ref=question_ref, decision_ref="decision:1", actor_ref="core", idempotency_key="select:2")
        plan = self.plan_selected_question(question_ref, key="plan:1")
        replay = self.plan_selected_question(question_ref, key="plan:2")
        self.assertEqual(replay["status"], "duplicate")
        # in_progress -> answered, then answered is terminal.
        self.start_planned_question(plan, key="start:1")
        claim = self.formal_claim("claim:order")
        self.backlog.answer_question(
            question_ref=question_ref, claim_version_refs=[claim["claim_version_id"]],
            actor_ref="core", idempotency_key="answer:1",
        )
        for operation in (
            lambda: self.backlog.block_question(question_ref=question_ref, reason="r", actor_ref="core"),
            lambda: self.backlog.retire_question(question_ref=question_ref, reason="r", actor_ref="core"),
            lambda: self.backlog.answer_question(question_ref=question_ref, claim_version_refs=[claim["claim_version_id"]], actor_ref="core", idempotency_key="answer:2"),
        ):
            with self.assertRaises(ResearchQuestionConflict):
                operation()
        self.assertEqual(
            self.store.connection.execute(
                "SELECT COUNT(*) FROM backlog_question_events"
            ).fetchone()[0], 5,
        )

    def test_blocked_and_retired_are_terminal(self):
        self.govern()
        self.start_decided_cycle(
            "agenda:cycle:1", "agenda-cycle:1", "decision:1",
            questions=[{"question": "Q?", "answer_criteria": "A"}],
        )
        self.record(question="Q?", answer_criteria="A", idempotency_key="record:1")
        question_ref = question_ref_for("mandate:coverage-quality", "wanhua", "Q?")
        self.backlog.select_question(question_ref=question_ref, decision_ref="decision:1", actor_ref="core", idempotency_key="select:1")
        self.plan_and_start_question(question_ref, suffix="terminal")
        blocked = self.backlog.block_question(question_ref=question_ref, reason="source unavailable", actor_ref="core", idempotency_key="block:1")
        self.assertEqual(blocked["state"], "blocked")
        with self.assertRaises(ResearchQuestionConflict):
            self.backlog.retire_question(question_ref=question_ref, reason="r", actor_ref="core")

    # ------------------------------------------------------------------ #
    # Selection links to exact AgendaDecision authority
    # ------------------------------------------------------------------ #

    def test_cross_mandate_selection_rejected(self):
        self.govern()
        self.agenda.create_mandate(
            "mandate:other", objective="Other mandate", scope_refs=["wanhua"],
            constraints={}, success_criteria={}, effective_from=NOW,
            effective_until=LATER, actor_ref="human:owner",
            version_id="mandate-version:2", idempotency_key="mandate:other",
        )
        # The cycle freezes mandate-version:1 -> mandate:coverage-quality.
        self.start_decided_cycle(
            "agenda:cycle:1", "agenda-cycle:1", "decision:1",
            questions=[{"question": "Q?", "answer_criteria": "A"}],
        )
        # The question is recorded under the other mandate.
        other_ref = self.backlog.record_question(
            mandate_version_ref="mandate-version:2", company_ref="wanhua",
            question="Q?", answer_criteria="A", source_refs=["s"],
            actor_ref="core", idempotency_key="record:cross",
        )["question_ref"]
        with self.assertRaises(ResearchQuestionConflict):
            self.backlog.select_question(
                question_ref=other_ref, decision_ref="decision:1",
                actor_ref="core", idempotency_key="select:cross",
            )

    def test_decision_that_does_not_select_question_rejected(self):
        self.govern()
        self.start_decided_cycle(
            "agenda:cycle:1", "agenda-cycle:1", "decision:1",
            questions=[{"question": "Selected Q?", "answer_criteria": "Selected A"}],
        )
        self.record(
            question="Selected Q?", answer_criteria="Selected A",
            idempotency_key="record:selected",
        )
        self.record(
            question="Other Q?", answer_criteria="Other A",
            idempotency_key="record:other",
        )
        other_ref = question_ref_for("mandate:coverage-quality", "wanhua", "Other Q?")
        with self.assertRaises(ResearchQuestionConflict):
            self.backlog.select_question(
                question_ref=other_ref, decision_ref="decision:1",
                actor_ref="core", idempotency_key="select:other",
            )

    def test_candidate_with_different_source_binding_rejected(self):
        self.govern()
        self.start_decided_cycle(
            "agenda:cycle:source", "agenda-cycle:source", "decision:source",
            questions=[{
                "question": "Q?", "answer_criteria": "A",
                "source_refs": ["evidence:cycle"],
            }],
        )
        recorded = self.record(
            question="Q?", answer_criteria="A", source_refs=["evidence:backlog"],
            idempotency_key="record:source",
        )
        with self.assertRaises(ResearchQuestionConflict):
            self.backlog.select_question(
                question_ref=recorded["question_ref"],
                decision_ref="decision:source",
                actor_ref="core",
                idempotency_key="select:source",
            )
        self.assertEqual(self.backlog.question(recorded["question_ref"])["state"], "open")

    def test_fabricated_decision_ref_rejected(self):
        self.govern()
        self.record(question="Q?", answer_criteria="A", idempotency_key="record:1")
        question_ref = question_ref_for("mandate:coverage-quality", "wanhua", "Q?")
        with self.assertRaises(ResearchQuestionNotFound):
            self.backlog.select_question(
                question_ref=question_ref, decision_ref="agenda-decision:forged",
                actor_ref="core", idempotency_key="select:forged",
            )

    # ------------------------------------------------------------------ #
    # Answer binding: formal ClaimVersion only
    # ------------------------------------------------------------------ #

    def _in_progress_question(self):
        self.govern()
        self.start_decided_cycle(
            "agenda:cycle:1", "agenda-cycle:1", "decision:1",
            questions=[{"question": "Q?", "answer_criteria": "A"}],
        )
        self.record(question="Q?", answer_criteria="A", idempotency_key="record:1")
        question_ref = question_ref_for("mandate:coverage-quality", "wanhua", "Q?")
        self.backlog.select_question(question_ref=question_ref, decision_ref="decision:1", actor_ref="core", idempotency_key="select:1")
        self.plan_and_start_question(question_ref, suffix="answer-helper")
        return question_ref

    def test_answered_without_claims_rejected(self):
        question_ref = self._in_progress_question()
        with self.assertRaises(ResearchQuestionValidationError):
            self.backlog.answer_question(
                question_ref=question_ref, claim_version_refs=[], actor_ref="core",
            )

    def test_candidate_and_missing_claims_rejected(self):
        question_ref = self._in_progress_question()
        with self.assertRaises(ResearchQuestionConflict):
            self.backlog.answer_question(
                question_ref=question_ref,
                claim_version_refs=["candidate-claim:forged"],
                actor_ref="core",
            )
        with self.assertRaises(ResearchQuestionNotFound):
            self.backlog.answer_question(
                question_ref=question_ref,
                claim_version_refs=["claim-version:missing"],
                actor_ref="core",
            )
        # A candidate staged in the review authority is not a formal claim.
        with self.assertRaises(ResearchQuestionNotFound):
            self.backlog.answer_question(
                question_ref=question_ref,
                claim_version_refs=["claim-version:not-in-ledger"],
                actor_ref="core",
            )
        self.assertEqual(
            self.store.connection.execute(
                "SELECT COUNT(*) FROM backlog_answer_bindings"
            ).fetchone()[0], 0,
        )

    def test_answer_rolls_back_on_late_claim_failure(self):
        question_ref = self._in_progress_question()
        claim = self.formal_claim("claim:good")
        with self.assertRaises(ResearchQuestionNotFound):
            self.backlog.answer_question(
                question_ref=question_ref,
                claim_version_refs=[claim["claim_version_id"], "claim-version:missing"],
                actor_ref="core",
            )
        # The answered event and both bindings must not exist; state unchanged.
        self.assertEqual(
            self.store.connection.execute(
                "SELECT COUNT(*) FROM backlog_answer_bindings"
            ).fetchone()[0], 0,
        )
        question = self.backlog.question(question_ref)
        self.assertEqual(question["state"], "in_progress")
        self.assertEqual([e["state"] for e in question["events"]][-1], "in_progress")

    def test_claim_hash_tamper_detected_and_rolled_back(self):
        question_ref = self._in_progress_question()
        claim = self.formal_claim("claim:tamper")
        self.store.connection.execute("DROP TRIGGER claim_versions_no_update")
        self.store.connection.execute(
            "UPDATE claim_versions SET content_hash=? WHERE claim_version_id=?",
            ("0" * 64, claim["claim_version_id"]),
        )
        with self.assertRaises(ResearchQuestionConflict):
            self.backlog.answer_question(
                question_ref=question_ref,
                claim_version_refs=[claim["claim_version_id"]],
                actor_ref="core",
            )
        self.assertEqual(
            self.store.connection.execute(
                "SELECT COUNT(*) FROM backlog_answer_bindings"
            ).fetchone()[0], 0,
        )
        self.assertEqual(self.backlog.question(question_ref)["state"], "in_progress")

    # ------------------------------------------------------------------ #
    # Tamper detection
    # ------------------------------------------------------------------ #

    def test_tampered_question_rows_detected(self):
        self.govern()
        self.record(question="Q?", answer_criteria="A", idempotency_key="record:1")
        question_ref = question_ref_for("mandate:coverage-quality", "wanhua", "Q?")
        connection = self.store.connection

        connection.execute("DROP TRIGGER backlog_question_versions_no_update")
        connection.execute(
            "UPDATE backlog_question_versions SET question=? WHERE question_ref=?",
            ("Q? FORGED", question_ref),
        )
        with self.assertRaises(ResearchQuestionConflict):
            self.backlog.question(question_ref)
        with self.assertRaises(ResearchQuestionConflict):
            self.backlog.history(question_ref)

        connection.execute("DROP TRIGGER backlog_questions_no_update")
        connection.execute(
            "UPDATE backlog_questions SET identity_hash=? WHERE question_ref=?",
            ("0" * 64, question_ref),
        )
        with self.assertRaises(ResearchQuestionConflict):
            self.backlog.question(question_ref)

    def _answered_question(self, suffix):
        self.govern()
        self.start_decided_cycle(
            f"agenda:cycle:{suffix}", f"agenda-cycle:{suffix}", f"decision:{suffix}",
            questions=[{"question": f"Q {suffix}?", "answer_criteria": f"A {suffix}"}],
            snapshot_id=f"perception:{suffix}",
        )
        self.record(
            question=f"Q {suffix}?", answer_criteria=f"A {suffix}",
            idempotency_key=f"record:{suffix}",
        )
        question_ref = question_ref_for("mandate:coverage-quality", "wanhua", f"Q {suffix}?")
        self.backlog.select_question(question_ref=question_ref, decision_ref=f"decision:{suffix}", actor_ref="core", idempotency_key=f"select:{suffix}")
        self.plan_and_start_question(question_ref, suffix=suffix)
        claim = self.formal_claim(f"claim:{suffix}")
        self.backlog.answer_question(
            question_ref=question_ref, claim_version_refs=[claim["claim_version_id"]],
            actor_ref="core", idempotency_key=f"answer:{suffix}",
        )
        return question_ref

    def test_tampered_event_row_detected(self):
        self.govern()
        question_ref = self._answered_question("evt")
        connection = self.store.connection
        connection.execute("DROP TRIGGER backlog_question_events_no_update")
        connection.execute(
            "UPDATE backlog_question_events SET metadata_json=? WHERE question_ref=? AND state='answered'",
            ('{"forged":true}', question_ref),
        )
        with self.assertRaises(ResearchQuestionConflict):
            self.backlog.question(question_ref)
        with self.assertRaises(ResearchQuestionConflict):
            self.backlog.history(question_ref)

    def test_tampered_answer_binding_row_detected(self):
        self.govern()
        question_ref = self._answered_question("bnd")
        connection = self.store.connection
        # Simulate a cross-question mixup: the binding row is re-pointed at a
        # different, existing question.  The FK target must exist for the
        # write to go through; the row hash check must still reject it.
        self.record(
            question="Someone else?", answer_criteria="A",
            idempotency_key="record:mixup",
        )
        connection.execute("DROP TRIGGER backlog_answer_bindings_no_update")
        connection.execute(
            "UPDATE backlog_answer_bindings SET question_ref=? WHERE question_ref=?",
            (question_ref_for("mandate:coverage-quality", "wanhua", "Someone else?"), question_ref),
        )
        # The mixup is detected on the receiving question: its binding rows no
        # longer hash to their stored content hash.
        with self.assertRaises(ResearchQuestionConflict):
            self.backlog.question(
                question_ref_for("mandate:coverage-quality", "wanhua", "Someone else?")
            )

    def test_tampered_selection_link_detected(self):
        self.govern()
        question_ref = self._answered_question("lnk")
        connection = self.store.connection
        connection.execute("DROP TRIGGER backlog_selection_links_no_update")
        connection.execute(
            "UPDATE backlog_selection_links SET decision_ref=? WHERE question_ref=?",
            ("agenda-decision:forged", question_ref),
        )
        with self.assertRaises(ResearchQuestionConflict):
            self.backlog.question(question_ref)

    def test_referenced_claim_tamper_is_detected_after_answer(self):
        self.govern()
        question_ref = self._answered_question("claim-after")
        binding = self.store.connection.execute(
            "SELECT claim_version_ref FROM backlog_answer_bindings "
            "WHERE question_ref=?",
            (question_ref,),
        ).fetchone()
        self.store.connection.execute("DROP TRIGGER claim_versions_no_update")
        self.store.connection.execute(
            "UPDATE claim_versions SET content_hash=? WHERE claim_version_id=?",
            ("0" * 64, binding["claim_version_ref"]),
        )
        with self.assertRaises(ResearchQuestionConflict):
            self.backlog.question(question_ref)
        with self.assertRaises(ResearchQuestionConflict):
            self.backlog.mandate_progress("mandate:coverage-quality")

    def test_pointer_and_history_tamper_fail_closed(self):
        self.govern()
        first = self.record(
            question="Q1?", answer_criteria="A1", idempotency_key="record:pointer:1"
        )
        second = self.record(
            question="Q2?", answer_criteria="A2", idempotency_key="record:pointer:2"
        )
        self.store.connection.execute(
            "DROP TRIGGER backlog_question_pointer_authorized_update"
        )
        self.store.connection.execute(
            "UPDATE backlog_question_pointer SET version_id=? WHERE question_ref=?",
            (second["question_version_ref"], first["question_ref"]),
        )
        with self.assertRaises(ResearchQuestionConflict):
            self.backlog.question(first["question_ref"])

        self.store.connection.execute("DROP TRIGGER backlog_question_events_no_update")
        self.store.connection.execute(
            "UPDATE backlog_question_events SET state='selected' WHERE question_ref=?",
            (second["question_ref"],),
        )
        with self.assertRaises(ResearchQuestionConflict):
            self.backlog.question(second["question_ref"])

    def test_unauthorized_direct_write_rejected(self):
        self.govern()
        with self.assertRaises((sqlite3.OperationalError, sqlite3.IntegrityError)):
            self.store.connection.execute(
                "INSERT INTO backlog_questions(question_ref,identity_json,identity_hash,"
                "actor_ref,record_json,content_hash,created_at) VALUES(?,?,?,?,?,?,?)",
                ("research-question:forged", "{}", "0" * 64, "x", "{}", "0" * 64, NOW),
            )

    # ------------------------------------------------------------------ #
    # Mandate progress projection
    # ------------------------------------------------------------------ #

    def test_projection_determinism_rebuild_and_no_mutation(self):
        self.govern()
        self.start_decided_cycle(
            "agenda:cycle:1", "agenda-cycle:1", "decision:1",
            questions=[{"question": "Q?", "answer_criteria": "A"}],
        )
        self.record(question="Q?", answer_criteria="A", idempotency_key="record:1")
        question_ref = question_ref_for("mandate:coverage-quality", "wanhua", "Q?")
        self.backlog.select_question(question_ref=question_ref, decision_ref="decision:1", actor_ref="core", idempotency_key="select:1")
        self.plan_and_start_question(question_ref, suffix="progress")
        claim = self.formal_claim("claim:progress")
        self.backlog.answer_question(
            question_ref=question_ref, claim_version_refs=[claim["claim_version_id"]],
            actor_ref="core", idempotency_key="answer:1",
        )

        mandate_rows_before = self.store.connection.execute(
            "SELECT COUNT(*) FROM mandate_versions"
        ).fetchone()[0]
        before = self.table_counts()
        first = self.backlog.mandate_progress("mandate:coverage-quality")
        second = self.backlog.mandate_progress("mandate:coverage-quality")
        self.assertEqual(first, second)
        self.assertEqual(first["content_hash"], second["content_hash"])
        self.assertEqual(first["totals"]["answered"], 1)
        self.assertEqual(first["totals"]["open"], 0)
        self.assertEqual(len(first["questions"]), 1)
        self.assertEqual(first["questions"][0]["question_ref"], question_ref)
        self.assertEqual(first["answered_claim_refs"][0]["claim_version_ref"], claim["claim_version_id"])
        self.assertEqual(first["answered_claim_refs"][0]["claim_version_hash"], claim["content_hash"])
        self.assertEqual(first["mandate_version_ref"], "mandate-version:1")
        # The projection is pure: it changed no authority row at all.
        self.assert_only_backlog_tables_changed(before)
        self.assertEqual(
            self.store.connection.execute(
                "SELECT COUNT(*) FROM mandate_versions"
            ).fetchone()[0], mandate_rows_before,
        )

        # A second question changes the projection deterministically.
        self.record(question="Q2?", answer_criteria="A2", idempotency_key="record:2")
        third = self.backlog.mandate_progress("mandate:coverage-quality")
        self.assertEqual(third, self.backlog.mandate_progress("mandate:coverage-quality"))
        self.assertEqual(third["totals"]["open"], 1)
        self.assertNotEqual(third["content_hash"], first["content_hash"])

    def test_projection_is_per_mandate_and_fails_on_tamper(self):
        self.govern()
        self.record(question="Q?", answer_criteria="A", idempotency_key="record:1")
        self.agenda.create_mandate(
            "mandate:other", objective="Other", scope_refs=["wanhua"],
            constraints={}, success_criteria={}, effective_from=NOW,
            effective_until=LATER, actor_ref="human:owner",
            version_id="mandate-version:2", idempotency_key="mandate:other",
        )
        progress = self.backlog.mandate_progress("mandate:coverage-quality")
        self.assertEqual(progress["totals"]["open"], 1)
        other = self.backlog.mandate_progress("mandate:other")
        self.assertEqual(other["totals"]["open"], 0)
        self.assertEqual(other["questions"], [])

        # Tampered question authority makes the rebuild fail closed.
        question_ref = question_ref_for("mandate:coverage-quality", "wanhua", "Q?")
        self.store.connection.execute("DROP TRIGGER backlog_question_versions_no_update")
        self.store.connection.execute(
            "UPDATE backlog_question_versions SET question=? WHERE question_ref=?",
            ("Q? FORGED", question_ref),
        )
        with self.assertRaises(ResearchQuestionConflict):
            self.backlog.mandate_progress("mandate:coverage-quality")

    def test_projection_unknown_mandate_rejected(self):
        self.govern()
        with self.assertRaises(ResearchQuestionNotFound):
            self.backlog.mandate_progress("mandate:never-existed")

    # ------------------------------------------------------------------ #
    # No plan / auto-accept authority
    # ------------------------------------------------------------------ #

    def test_planned_lifecycle_does_not_commit_new_ledger_authority(self):
        self.govern()
        self.start_decided_cycle(
            "agenda:cycle:1", "agenda-cycle:1", "decision:1",
            questions=[{"question": "Q?", "answer_criteria": "A"}],
        )
        claim = self.formal_claim("claim:side")
        claim_rows_before = self.store.connection.execute(
            "SELECT COUNT(*) FROM claim_versions"
        ).fetchone()[0]
        self.record(question="Q?", answer_criteria="A", idempotency_key="record:1")
        question_ref = question_ref_for("mandate:coverage-quality", "wanhua", "Q?")
        self.backlog.select_question(question_ref=question_ref, decision_ref="decision:1", actor_ref="core", idempotency_key="select:1")
        self.plan_and_start_question(question_ref, suffix="side")
        self.backlog.answer_question(
            question_ref=question_ref, claim_version_refs=[claim["claim_version_id"]],
            actor_ref="core", idempotency_key="answer:1",
        )
        # Planner writes plan/workflow/scheduler authority, but never creates
        # a ClaimVersion or promotes a candidate by itself.
        self.assertEqual(
            self.store.connection.execute(
                "SELECT COUNT(*) FROM claim_versions"
            ).fetchone()[0],
            claim_rows_before,
        )
        self.assertEqual(
            self.store.connection.execute(
                "SELECT COUNT(*) FROM research_plan_versions"
            ).fetchone()[0], 1,
        )
        self.assertEqual(
            self.store.connection.execute(
                "SELECT COUNT(*) FROM scheduler_work_orders"
            ).fetchone()[0], 1,
        )

    def test_backlog_exposes_no_plan_or_execution_api(self):
        forbidden = {"create_plan", "start_work", "create_work_order", "authorize_execution",
                     "accept", "auto_accept", "promote", "commit_answer"}
        for name in forbidden:
            self.assertFalse(hasattr(self.backlog, name), name)
        self.assertFalse(hasattr(ResearchQuestionBacklog, "PLAN_SCHEMA_VERSION"))
        # Backlog still cannot create a plan; the separate plan authority can
        # bind one exact selected question and returns its immutable ref/hash.
        self.govern()
        self.start_decided_cycle(
            "agenda:cycle:1", "agenda-cycle:1", "decision:1",
            questions=[{"question": "Q?", "answer_criteria": "A"}],
        )
        self.record(question="Q?", answer_criteria="A", idempotency_key="record:1")
        question_ref = question_ref_for("mandate:coverage-quality", "wanhua", "Q?")
        self.backlog.select_question(question_ref=question_ref, decision_ref="decision:1", actor_ref="core", idempotency_key="select:1")
        planned = self.plan_selected_question(question_ref, key="plan:boundary")
        self.assertIn("plan_version_ref", planned)
        self.assertEqual(planned["question_state"], "planned")

    # ------------------------------------------------------------------ #
    # Exact readers
    # ------------------------------------------------------------------ #

    def test_readers_reject_missing_and_callers_cannot_supply_ids(self):
        self.govern()
        with self.assertRaises(ResearchQuestionNotFound):
            self.backlog.question("research-question:missing")
        with self.assertRaises(ResearchQuestionNotFound):
            self.backlog.question_version("research-question-version:missing")
        with self.assertRaises(TypeError):
            self.backlog.record_question(
                question_ref="research-question:forged",  # type: ignore[call-arg]
                mandate_version_ref="mandate-version:1", company_ref="wanhua",
                question="Q?", answer_criteria="A", source_refs=["s"],
                actor_ref="core",
            )
        with self.assertRaises(TypeError):
            self.backlog.record_question(
                mandate_version_ref="mandate-version:1", company_ref="wanhua",
                question="Q?", answer_criteria="A", source_refs=["s"],
                actor_ref="core", content_hash="0" * 64,  # type: ignore[call-arg]
            )

    def test_history_replays_exact_versions_and_events(self):
        self.govern()
        self.start_decided_cycle(
            "agenda:cycle:1", "agenda-cycle:1", "decision:1",
            questions=[{"question": "Q?", "answer_criteria": "A"}],
        )
        recorded = self.record(question="Q?", answer_criteria="A", idempotency_key="record:1")
        question_ref = recorded["question_ref"]
        self.backlog.select_question(question_ref=question_ref, decision_ref="decision:1", actor_ref="core", idempotency_key="select:1")
        history = self.backlog.history(question_ref)
        self.assertEqual([v["version"] for v in history["versions"]], [1])
        self.assertEqual([e["state"] for e in history["events"]], ["open", "selected"])
        self.assertEqual(history["state"], "selected")
        self.assertEqual(history["versions"][0]["question_ref"], question_ref)
        self.assertEqual(history["events"][0]["state"], "open")


if __name__ == "__main__":
    unittest.main()
