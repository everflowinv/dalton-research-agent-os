"""Adversarial tests for accepted ResearchPlan-to-Backlog closure."""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from dalton_core.research_plan_closure import (
    ResearchPlanClosureConflict,
    ResearchPlanClosureCoordinator,
    ResearchPlanClosurePending,
)
from dalton_core.research_review import (
    HumanReviewAuthority,
    ResearchReviewConflict,
)
from tests.test_research_plan_executor import PlanExecutorHarness


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

    def _accept(self, *, promote: bool = True) -> dict:
        candidate = self.review.list_candidates(limit=10)[0]
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
            source_event_ref="research-review:plan-closure-test",
            idempotency_key="review:plan-closure-test",
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
