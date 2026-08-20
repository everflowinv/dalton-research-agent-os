"""Adversarial tests for accepted ResearchPlan-to-Backlog closure."""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from dalton_core.errors import GateRejected
from dalton_core.research_plan_closure import (
    ResearchPlanClosureConflict,
    ResearchPlanClosureCoordinator,
    ResearchPlanClosurePending,
)
from dalton_core.research_auto_commit import ResearchAutoCommitRejected, RULE_REF
from dalton_core.research_review import (
    HumanReviewAuthority,
    ResearchReviewConflict,
)
from tests.test_research_plan_executor import PlanExecutorHarness
from tests.test_connector_runner import assert_wire_schema


REVIEWER = "human:tailscale-0123456789abcdef0123456789abcdef"
REVIEWED_AT = "2026-08-20T09:30:00.000000+00:00"


class InjectedClosureCrash(RuntimeError):
    pass


class ResearchPlanClosureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.harness = PlanExecutorHarness(suffix="closure")
        self.addCleanup(self.harness.close)
        self.harness.run_to_complete()
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.review_path = Path(self.temp.name) / "candidate-review.sqlite"
        target = sqlite3.connect(self.review_path)
        try:
            self.harness.staging.connection.backup(target)
        finally:
            target.close()
        self.review = HumanReviewAuthority(self.review_path)
        self.addCleanup(self.review.close)

    def _accept(
        self,
        *,
        promote: bool = True,
        candidate_claim_ref: str | None = None,
    ) -> dict:
        candidates = self.review.list_candidates(limit=10)
        candidate = (
            candidates[0]
            if candidate_claim_ref is None
            else next(
                item for item in candidates
                if item["claim"]["id"] == candidate_claim_ref
            )
        )
        claim = candidate["claim"]
        result = self.review.decide(
            candidate_claim_ref=claim["id"],
            candidate_claim_hash=claim["content_hash"],
            verdict="accept",
            reviewed_semantics={
                field: claim[field]
                for field in (
                    "subject_ref", "metric_or_aspect", "period", "basis",
                    "normalized_statement",
                )
            },
            rationale="Checked the exact bounded filing list and count.",
            findings=["candidate answers the plan's exact question"],
            reviewer_ref=REVIEWER,
            source_event_ref=f"research-review:plan-closure-test:v{claim['version']}",
            idempotency_key=f"review:plan-closure-test:v{claim['version']}",
            created_at=REVIEWED_AT,
            proposed_revisions=None,
        )
        if promote:
            bundle = self.review.pending_commits(limit=10)[0]
            ledger = self.harness.core.commit_reviewed_candidate(
                **bundle,
                idempotency_key=f"reviewed-ledger:{result['decision_ref']}",
            )
            self.review.record_commit_result(
                result["decision_ref"],
                created_at=REVIEWED_AT,
                ledger_result=ledger,
            )
        return result

    def _revise(self) -> tuple[dict, dict]:
        candidate = self.review.list_candidates(limit=10)[0]
        claim = candidate["claim"]
        decision = self.review.decide(
            candidate_claim_ref=claim["id"],
            candidate_claim_hash=claim["content_hash"],
            verdict="revise",
            reviewed_semantics={
                field: claim[field]
                for field in (
                    "subject_ref", "metric_or_aspect", "period", "basis",
                    "normalized_statement",
                )
            },
            rationale="The statement should identify that the count is bounded by the exact plan window.",
            findings=["the numeric result is sound but the statement needs narrower wording"],
            reviewer_ref=REVIEWER,
            source_event_ref="research-review:plan-closure-revise-v1",
            idempotency_key="review:plan-closure-revise-v1",
            created_at=REVIEWED_AT,
            proposed_revisions={
                "normalized_statement": (
                    "The exact ResearchPlan window contains the verified SEC filing count."
                )
            },
        )
        consumed = self.review.consume_revision(decision["decision_ref"])
        revised = next(
            item for item in self.review.list_candidates(limit=10)
            if item["claim"]["id"] == consumed["candidate_claim_ref"]
        )
        return decision, revised

    def _closure(self, *, fault_at: str | None = None) -> ResearchPlanClosureCoordinator:
        def fault_hook(seam: str) -> None:
            if seam == fault_at:
                raise InjectedClosureCrash(seam)

        return ResearchPlanClosureCoordinator(
            plan=self.harness.planner.plans,
            backlog=self.harness.planner.backlog,
            coordinator=self.harness.coordinator,
            review=self.review,
            fault_injector=fault_hook if fault_at else None,
        )

    def _activate_auto_commit_policy(self) -> dict:
        active = self.harness.core.active_policy()
        policy = dict(active["policy"])
        policy["research_candidate_auto_commit"] = {
            "enabled": True,
            "rules": [RULE_REF],
            "max_records": 20,
        }
        return self.harness.core.create_policy(
            policy,
            policy_version_id="policy:research-auto-commit:test:v2",
            version_number=2,
            prior_version_ref=active["policy_version_id"],
            actor_ref="human:test-owner",
            change_reason="authorize isolated low-risk SEC filing-count results",
            activate=True,
        )

    def test_accepted_formal_claim_closes_exact_question_and_replays(self) -> None:
        decision = self._accept()
        first = self._closure().close(
            plan_version_ref=self.harness.plan_wire["id"],
            review_decision_ref=decision["decision_ref"],
        )
        self.assertEqual(first["status"], "fresh")
        self.assertEqual(
            self.harness.planner.backlog.question(
                self.harness.plan_wire["question_ref"]
            )["state"],
            "answered",
        )
        claim = json.loads(self.harness.core.connection.execute(
            "SELECT claim_json FROM claim_versions WHERE claim_version_id=?",
            (first["claim_version_ref"],),
        ).fetchone()[0])
        self.assertEqual(claim["candidate_origin_ref"], first["candidate_claim_ref"])
        self.assertEqual(
            self.harness.core.connection.execute(
                "SELECT COUNT(*) FROM evidence_versions"
            ).fetchone()[0],
            1,
        )
        self.assertEqual(
            self.harness.core.connection.execute(
                "SELECT COUNT(*) FROM claim_versions"
            ).fetchone()[0],
            1,
        )
        self.assertEqual(
            self.harness.core.connection.execute(
                "SELECT COUNT(*) FROM evidence_relations"
            ).fetchone()[0],
            1,
        )
        replay = self._closure().close(
            plan_version_ref=self.harness.plan_wire["id"],
            review_decision_ref=decision["decision_ref"],
        )
        self.assertEqual(replay["status"], "duplicate")
        self.assertEqual(replay["answer_binding_ref"], first["answer_binding_ref"])
        self.assertEqual(
            self.harness.core.connection.execute(
                "SELECT COUNT(*) FROM backlog_answer_bindings"
            ).fetchone()[0],
            1,
        )

    def test_low_risk_policy_candidate_closes_without_human_review(self) -> None:
        self._activate_auto_commit_policy()
        candidate = self.review.list_candidates(limit=10)[0]
        promoted = self.harness.core.commit_policy_candidate(
            evidence=candidate["evidence"],
            claim=candidate["claim"],
            idempotency_key="policy-ledger:plan-closure-test",
        )
        self.assertEqual(promoted["status"], "fresh")
        self.assertEqual(
            promoted["authorization"]["authorization"],
            "versioned_governance_policy",
        )
        assert_wire_schema(
            self, "policy-commit-decision.schema.json", promoted["authorization"]
        )
        self.assertEqual(
            self.review.connection.execute(
                "SELECT COUNT(*) FROM human_review_decisions"
            ).fetchone()[0],
            0,
        )
        first = self._closure().close_policy_authorized(
            plan_version_ref=self.harness.plan_wire["id"],
            authorization=promoted["authorization"],
        )
        replay = self._closure().close_policy_authorized(
            plan_version_ref=self.harness.plan_wire["id"],
            authorization=promoted["authorization"],
        )
        self.assertEqual(first["status"], "fresh")
        self.assertEqual(replay["status"], "duplicate")
        self.assertEqual(first["answer_binding_ref"], replay["answer_binding_ref"])
        self.assertEqual(
            self.harness.core.connection.execute(
                "SELECT COUNT(*) FROM claim_versions"
            ).fetchone()[0],
            1,
        )
        self.assertEqual(
            self.harness.core.connection.execute(
                "SELECT COUNT(*) FROM backlog_answer_bindings"
            ).fetchone()[0],
            1,
        )

    def test_full_low_risk_loop_needs_no_per_plan_or_per_claim_human_review(self) -> None:
        autonomous = PlanExecutorHarness(suffix="autonomous-loop", auto_start=True)
        try:
            autonomous.run_to_complete()
            with tempfile.TemporaryDirectory() as directory:
                review_path = Path(directory) / "candidate-review.sqlite"
                target = sqlite3.connect(review_path)
                try:
                    autonomous.staging.connection.backup(target)
                finally:
                    target.close()
                review = HumanReviewAuthority(review_path)
                try:
                    candidate = review.list_candidates(limit=10)[0]
                    promoted = autonomous.core.commit_policy_candidate(
                        evidence=candidate["evidence"],
                        claim=candidate["claim"],
                        idempotency_key="policy-ledger:autonomous-loop",
                    )
                    closure = ResearchPlanClosureCoordinator(
                        plan=autonomous.planner.plans,
                        backlog=autonomous.planner.backlog,
                        coordinator=autonomous.coordinator,
                        review=review,
                    )
                    closed = closure.close_policy_authorized(
                        plan_version_ref=autonomous.plan_wire["id"],
                        authorization=promoted["authorization"],
                    )
                    self.assertEqual(closed["status"], "fresh")
                    self.assertEqual(
                        autonomous.core.connection.execute(
                            "SELECT COUNT(*) FROM research_plan_approvals"
                        ).fetchone()[0],
                        0,
                    )
                    self.assertEqual(
                        review.connection.execute(
                            "SELECT COUNT(*) FROM human_review_decisions"
                        ).fetchone()[0],
                        0,
                    )
                    self.assertEqual(
                        autonomous.core.connection.execute(
                            "SELECT COUNT(*) FROM backlog_answer_bindings"
                        ).fetchone()[0],
                        1,
                    )
                finally:
                    review.close()
        finally:
            autonomous.close()

    def test_auto_commit_fails_closed_without_policy_or_exact_semantics(self) -> None:
        candidate = self.review.list_candidates(limit=10)[0]
        with self.assertRaises(ResearchAutoCommitRejected):
            self.harness.core.commit_policy_candidate(
                evidence=candidate["evidence"],
                claim=candidate["claim"],
                idempotency_key="policy-ledger:no-policy",
            )
        self._activate_auto_commit_policy()
        drifted = dict(candidate["claim"])
        drifted["normalized_statement"] = "A model-written interpretation."
        drifted.pop("content_hash")
        from dalton_core.store import content_hash
        drifted["content_hash"] = content_hash(drifted)
        with self.assertRaises(ResearchAutoCommitRejected):
            self.harness.core.commit_policy_candidate(
                evidence=candidate["evidence"],
                claim=drifted,
                idempotency_key="policy-ledger:semantic-drift",
            )
        self.assertEqual(
            self.harness.core.connection.execute(
                "SELECT COUNT(*) FROM claim_versions"
            ).fetchone()[0],
            0,
        )

    def test_revised_v2_accept_closes_original_plan_once(self) -> None:
        revise_decision, revised = self._revise()
        with self.assertRaises(GateRejected):
            self.harness.core.commit_reviewed_candidate(
                **self.review.decision_bundle(revise_decision["decision_ref"]),
                idempotency_key="reviewed-ledger:revised-v1",
            )
        self.assertEqual(
            self.harness.core.connection.execute(
                "SELECT COUNT(*) FROM claim_versions"
            ).fetchone()[0],
            0,
        )
        accepted = self._accept(candidate_claim_ref=revised["claim"]["id"])
        first = self._closure().close(
            plan_version_ref=self.harness.plan_wire["id"],
            review_decision_ref=accepted["decision_ref"],
        )
        replay = self._closure().close(
            plan_version_ref=self.harness.plan_wire["id"],
            review_decision_ref=accepted["decision_ref"],
        )
        self.assertEqual(first["status"], "fresh")
        self.assertEqual(replay["status"], "duplicate")
        self.assertEqual(first["answer_binding_ref"], replay["answer_binding_ref"])
        formal = json.loads(self.harness.core.connection.execute(
            "SELECT claim_json FROM claim_versions WHERE claim_version_id=?",
            (first["claim_version_ref"],),
        ).fetchone()[0])
        self.assertEqual(formal["candidate_origin_ref"], revised["claim"]["id"])
        self.assertEqual(
            formal["normalized_statement"],
            "The exact ResearchPlan window contains the verified SEC filing count.",
        )
        self.assertEqual(
            self.harness.core.connection.execute(
                "SELECT COUNT(*) FROM backlog_answer_bindings"
            ).fetchone()[0],
            1,
        )
        self.assertEqual(
            self.review.connection.execute(
                "SELECT COUNT(*) FROM candidate_claim_versions"
            ).fetchone()[0],
            2,
        )
        self.assertEqual(
            self.review.connection.execute("PRAGMA integrity_check").fetchone()[0],
            "ok",
        )

    def test_accept_without_formal_commit_cannot_answer_backlog(self) -> None:
        decision = self._accept(promote=False)
        with self.assertRaises(ResearchPlanClosurePending):
            self._closure().close(
                plan_version_ref=self.harness.plan_wire["id"],
                review_decision_ref=decision["decision_ref"],
            )
        self.assertEqual(
            self.harness.planner.backlog.question(
                self.harness.plan_wire["question_ref"]
            )["state"],
            "in_progress",
        )
        self.assertEqual(
            self.harness.core.connection.execute(
                "SELECT COUNT(*) FROM backlog_answer_bindings"
            ).fetchone()[0],
            0,
        )

    def test_crash_after_answer_replays_without_second_binding(self) -> None:
        decision = self._accept()
        with self.assertRaises(InjectedClosureCrash):
            self._closure(fault_at="after_backlog_answer").close(
                plan_version_ref=self.harness.plan_wire["id"],
                review_decision_ref=decision["decision_ref"],
            )
        self.assertEqual(
            self.harness.core.connection.execute(
                "SELECT COUNT(*) FROM backlog_answer_bindings"
            ).fetchone()[0],
            1,
        )
        replay = self._closure().close(
            plan_version_ref=self.harness.plan_wire["id"],
            review_decision_ref=decision["decision_ref"],
        )
        self.assertEqual(replay["status"], "duplicate")
        self.assertEqual(
            self.harness.core.connection.execute(
                "SELECT COUNT(*) FROM backlog_answer_bindings"
            ).fetchone()[0],
            1,
        )

    def test_core_promotion_receipt_tamper_fails_before_answer(self) -> None:
        decision = self._accept()
        connection = self.harness.core.connection
        connection.execute(
            "DROP TRIGGER reviewed_candidate_commits_no_update"
        )
        connection.execute(
            "UPDATE reviewed_candidate_commits SET request_hash=?",
            ("0" * 64,),
        )
        with self.assertRaises(ResearchPlanClosureConflict):
            self._closure().close(
                plan_version_ref=self.harness.plan_wire["id"],
                review_decision_ref=decision["decision_ref"],
            )
        self.assertEqual(
            connection.execute(
                "SELECT COUNT(*) FROM backlog_answer_bindings"
            ).fetchone()[0],
            0,
        )

    def test_review_commit_chain_tamper_fails_before_answer(self) -> None:
        decision = self._accept()
        self.review.connection.execute(
            "DROP TRIGGER human_review_commit_events_no_update"
        )
        self.review.connection.execute(
            "UPDATE human_review_commit_events SET content_hash=? "
            "WHERE state='committed'",
            ("0" * 64,),
        )
        with self.assertRaises(ResearchReviewConflict):
            self._closure().close(
                plan_version_ref=self.harness.plan_wire["id"],
                review_decision_ref=decision["decision_ref"],
            )
        self.assertEqual(
            self.harness.core.connection.execute(
                "SELECT COUNT(*) FROM backlog_answer_bindings"
            ).fetchone()[0],
            0,
        )

    def test_promotion_domain_event_tamper_fails_before_answer(self) -> None:
        decision = self._accept()
        connection = self.harness.core.connection
        connection.execute("DROP TRIGGER domain_events_no_update")
        connection.execute(
            "UPDATE domain_events SET content_hash=? "
            "WHERE event_type='claim_versioned'",
            ("0" * 64,),
        )
        with self.assertRaises(ResearchPlanClosureConflict):
            self._closure().close(
                plan_version_ref=self.harness.plan_wire["id"],
                review_decision_ref=decision["decision_ref"],
            )
        self.assertEqual(
            connection.execute(
                "SELECT COUNT(*) FROM backlog_answer_bindings"
            ).fetchone()[0],
            0,
        )


if __name__ == "__main__":
    unittest.main()
