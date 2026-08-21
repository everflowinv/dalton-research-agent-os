"""Recorded end-to-end canary for ResearchPlan closure -> thesis impact."""

from __future__ import annotations

import hashlib
import sqlite3
import tempfile
import unittest
from pathlib import Path

from dalton_core.research_plan_closure import ResearchPlanClosureCoordinator
from dalton_core.research_review import HumanReviewAuthority
from dalton_core.store import canonical_json
from dalton_core.thesis_impact import ThesisImpactAuthority
from dalton_core.thesis_impact_control import (
    ASSESSMENT_BUDGET,
    VERIFIER_BUDGET,
    ResearchPlanThesisImpactCoordinator,
)
from tests.test_research_plan_executor import PlanExecutorHarness


CREATED_AT = "2026-08-20T23:00:00.000000+00:00"


def invocation(
    identifier: str,
    work_order_ref: str,
    input_refs: list[str],
    model_family: str,
    capability: str,
) -> dict:
    return {
        "schema_version": "0.1",
        "id": identifier,
        "created_at": CREATED_AT,
        "work_order_ref": work_order_ref,
        "profile_ref": "runtime-profile:dalton:0.1",
        "granularity": "task",
        "capability": capability,
        "provider": f"recorded-{model_family}",
        "model": f"recorded-model-{model_family}",
        "model_family": model_family,
        "input_refs": input_refs,
        "output_refs": [],
        "started_at": CREATED_AT,
        "completed_at": "2026-08-20T23:00:01.000000+00:00",
        "usage": {"input_tokens": 10, "output_tokens": 10, "cost_usd": 0},
        "side_effects": [],
        "runtime_ref": "runtime:recorded-canary",
        "actor_ref": "system:recorded-canary",
        "parent_ref": None,
        "environment_hash": "a" * 64,
    }


class ResearchPlanThesisImpactControlTests(unittest.TestCase):
    def setUp(self) -> None:
        self.harness = PlanExecutorHarness(
            suffix=self._testMethodName,
            company_facts=True,
            auto_start=True,
        )
        self.addCleanup(self.harness.close)
        self.harness.run_to_complete()
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        review_path = Path(self.temp.name) / "review.sqlite"
        target = sqlite3.connect(review_path)
        try:
            self.harness.staging.connection.backup(target)
        finally:
            target.close()
        self.review = HumanReviewAuthority(review_path)
        self.addCleanup(self.review.close)
        candidate = self.review.list_candidates(limit=10)[0]
        bundle = self.review.candidate_authority_bundle(candidate["claim"]["id"])
        promoted = self.harness.core.commit_policy_candidate(
            **bundle,
            idempotency_key=f"policy-ledger:{self._testMethodName}",
        )
        self.authorization = promoted["authorization"]
        closure = ResearchPlanClosureCoordinator(
            plan=self.harness.planner.plans,
            backlog=self.harness.planner.backlog,
            coordinator=self.harness.coordinator,
            review=self.review,
        )
        self.impact = ThesisImpactAuthority(
            self.harness.core, self.harness.scheduler()
        )
        self.control = ResearchPlanThesisImpactCoordinator(
            closure=closure, impact=self.impact
        )
        self.thesis_ref = "thesis:wanhua:operating-leverage"

    def _seed_thesis(self) -> dict:
        producer = invocation(
            f"invocation:seed-producer:{self._testMethodName}",
            f"work:seed-producer:{self._testMethodName}",
            [],
            "seed-a",
            "research",
        )
        verifier = invocation(
            f"invocation:seed-verifier:{self._testMethodName}",
            f"work:seed-verifier:{self._testMethodName}",
            [],
            "seed-b",
            "verify",
        )
        change_ref = f"change:seed-thesis:{self._testMethodName}"
        verification_ref = f"verification:seed-thesis:{self._testMethodName}"
        self.harness.core.stage_change(
            change_ref,
            thesis_id=self.thesis_ref,
            content={
                "statement": "Quarterly revenue growth should support earnings growth.",
                "mechanism": "Quarterly revenue growth sustains operating leverage.",
                "confidence": 0.6,
                "implied_expectation": "Reported quarterly revenue grows year over year.",
                "claim_refs": [],
                "catalyst_refs": [],
                "falsifier_refs": [],
                "change_reason": "recorded closure-to-impact canary seed",
            },
            producer_invocation=producer,
        )
        self.harness.core.verify_change(
            change_ref,
            verification_id=verification_ref,
            verifier_invocation=verifier,
            verdict="pass",
            findings=[],
        )
        return self.harness.core.commit(
            change_ref,
            verification_ref,
            f"commit:seed-thesis:{self._testMethodName}",
        )

    def _complete_model(
        self,
        *,
        work: dict,
        output: dict,
        model_family: str,
    ) -> dict:
        identifier = work["id"].removeprefix("work:")
        invocation_ref = f"invocation:{identifier}"
        inv = invocation(
            invocation_ref,
            work["id"],
            work["input_refs"],
            model_family,
            work["requested_capabilities"][0],
        )
        owner = f"worker:{model_family}"
        lease = self.harness.scheduler().claim(owner, work_order_id=work["id"])
        text = canonical_json(output)
        result = {
            "schema_version": "0.1",
            "id": f"result:{identifier}",
            "created_at": CREATED_AT,
            "work_order_ref": work["id"],
            "invocation_ref": invocation_ref,
            "status": "succeeded",
            "outputs": {
                "text": text,
                "content_hash": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            },
            "artifact_refs": [],
            "actual_side_effects": [],
            "usage_refs": [],
            "error": None,
            "metadata": {"mode": "recorded-canary"},
        }
        self.harness.scheduler().complete(
            work["id"],
            lease["attempt"]["attempt_number"],
            owner,
            lease["lease_token"],
            result,
            idempotency_key=f"complete:{identifier}",
        )
        return inv

    def _close_and_start(self) -> dict:
        return self.control.close_policy_and_start(
            plan_version_ref=self.harness.plan_wire["id"],
            authorization=self.authorization,
            thesis_ref=self.thesis_ref,
        )

    def test_recorded_supports_path_replays_without_thesis_mutation(self) -> None:
        committed = self._seed_thesis()
        before_pointer = dict(
            self.harness.core.current_pointer(self.thesis_ref)
        )
        started = self._close_and_start()
        assessment_work = started["impact"]["assessment_work_order"]
        claim = self.harness.core.get_claim(
            started["impact"]["claim_version_ref"]
        )["claim"]
        thesis = self.harness.core.get_version(committed["version_id"])["content"]
        self.assertEqual(assessment_work["budget"], ASSESSMENT_BUDGET)
        self.assertEqual(assessment_work["declared_side_effects"], [])
        self.assertIn(canonical_json(claim), assessment_work["question"])
        self.assertIn(canonical_json(thesis), assessment_work["question"])
        producer = self._complete_model(
            work=assessment_work,
            model_family="impact-a",
            output={
                "schema_version": "0.1",
                "claim_version_ref": claim["id"],
                "claim_version_hash": claim["content_hash"],
                "thesis_version_ref": thesis["id"],
                "thesis_version_hash": thesis["content_hash"],
                "driver_statement": thesis["mechanism"],
                "impact": "supports",
                "rationale": "The exact reported growth is directionally consistent with the driver.",
                "follow_up_question": None,
            },
        )
        assessed = self.control.advance_assessment(
            plan_version_ref=self.harness.plan_wire["id"],
            thesis_ref=self.thesis_ref,
            producer_invocation=producer,
        )
        assessment = assessed["assessment"]["assessment"]
        verifier_work = assessed["verifier_work_order"]
        self.assertEqual(verifier_work["budget"], VERIFIER_BUDGET)
        self.assertEqual(
            verifier_work["input_refs"],
            [assessment["id"], claim["id"], thesis["id"]],
        )
        self.assertIn(canonical_json(claim), verifier_work["question"])
        self.assertIn(canonical_json(thesis), verifier_work["question"])
        self.assertIn(canonical_json(assessment), verifier_work["question"])
        verifier = self._complete_model(
            work=verifier_work,
            model_family="impact-b",
            output={
                "schema_version": "0.1",
                "assessment_ref": assessment["id"],
                "assessment_hash": assessment["content_hash"],
                "verdict": "pass",
                "findings": [],
            },
        )
        completed = self.control.advance_verification(
            plan_version_ref=self.harness.plan_wire["id"],
            thesis_ref=self.thesis_ref,
            assessment_ref=assessment["id"],
            verifier_invocation=verifier,
        )
        self.assertEqual(completed["status"], "eligible")
        self.assertEqual(completed["eligible"]["assessment"]["impact"], "supports")
        self.assertIsNone(completed["follow_up"])
        self.assertEqual(
            self.harness.core.current_pointer(self.thesis_ref), before_pointer
        )
        self.assertEqual(
            self.review.connection.execute(
                "SELECT COUNT(*) FROM human_review_decisions"
            ).fetchone()[0],
            0,
        )

        replay_start = self.control.start_from_closed_plan(
            plan_version_ref=self.harness.plan_wire["id"], thesis_ref=self.thesis_ref
        )
        replay_assessment = self.control.advance_assessment(
            plan_version_ref=self.harness.plan_wire["id"],
            thesis_ref=self.thesis_ref,
            producer_invocation=producer,
        )
        replay_verification = self.control.advance_verification(
            plan_version_ref=self.harness.plan_wire["id"],
            thesis_ref=self.thesis_ref,
            assessment_ref=assessment["id"],
            verifier_invocation=verifier,
        )
        self.assertEqual(replay_start["enqueue"]["status"], "duplicate")
        self.assertEqual(
            replay_assessment["assessment"]["status"], "duplicate"
        )
        self.assertEqual(
            replay_verification["verification"]["status"], "duplicate"
        )
        self.assertEqual(
            self.harness.core.connection.execute(
                "SELECT COUNT(*) FROM thesis_impact_assessments"
            ).fetchone()[0],
            1,
        )
        self.assertEqual(
            self.harness.core.connection.execute(
                "SELECT COUNT(*) FROM thesis_impact_verifications"
            ).fetchone()[0],
            1,
        )
        self.assertEqual(
            self.harness.scheduler().connection.execute(
                "SELECT COUNT(*) FROM scheduler_work_orders"
            ).fetchone()[0],
            6,
        )
        self.assertEqual(
            self.harness.core.connection.execute("PRAGMA integrity_check").fetchone()[0],
            "ok",
        )
        self.assertEqual(
            self.harness.scheduler().connection.execute(
                "PRAGMA integrity_check"
            ).fetchone()[0],
            "ok",
        )

    def test_missing_thesis_records_follow_up_without_model_work(self) -> None:
        started = self._close_and_start()
        self.assertEqual(started["impact"]["status"], "follow_up_recorded")
        self.assertIsNone(started["impact"]["assessment_work_order"])
        self.assertEqual(
            self.harness.scheduler().connection.execute(
                "SELECT COUNT(*) FROM scheduler_work_orders"
            ).fetchone()[0],
            4,
        )
        replay = self.control.start_from_closed_plan(
            plan_version_ref=self.harness.plan_wire["id"], thesis_ref=self.thesis_ref
        )
        self.assertEqual(replay["route"]["backlog"]["status"], "duplicate")

    def test_insufficient_pass_records_exact_follow_up_once(self) -> None:
        committed = self._seed_thesis()
        started = self._close_and_start()
        work = started["impact"]["assessment_work_order"]
        claim = self.harness.core.get_claim(
            started["impact"]["claim_version_ref"]
        )["claim"]
        thesis = self.harness.core.get_version(committed["version_id"])["content"]
        follow_up_question = (
            "What was the exact same-period gross margin change and source basis?"
        )
        producer = self._complete_model(
            work=work,
            model_family="impact-a",
            output={
                "schema_version": "0.1",
                "claim_version_ref": claim["id"],
                "claim_version_hash": claim["content_hash"],
                "thesis_version_ref": thesis["id"],
                "thesis_version_hash": thesis["content_hash"],
                "driver_statement": thesis["mechanism"],
                "impact": "insufficient",
                "rationale": "Revenue growth alone does not establish operating leverage.",
                "follow_up_question": follow_up_question,
            },
        )
        assessed = self.control.advance_assessment(
            plan_version_ref=self.harness.plan_wire["id"],
            thesis_ref=self.thesis_ref,
            producer_invocation=producer,
        )
        assessment = assessed["assessment"]["assessment"]
        verifier = self._complete_model(
            work=assessed["verifier_work_order"],
            model_family="impact-b",
            output={
                "schema_version": "0.1",
                "assessment_ref": assessment["id"],
                "assessment_hash": assessment["content_hash"],
                "verdict": "pass",
                "findings": [],
            },
        )
        first = self.control.advance_verification(
            plan_version_ref=self.harness.plan_wire["id"],
            thesis_ref=self.thesis_ref,
            assessment_ref=assessment["id"],
            verifier_invocation=verifier,
        )
        replay = self.control.advance_verification(
            plan_version_ref=self.harness.plan_wire["id"],
            thesis_ref=self.thesis_ref,
            assessment_ref=assessment["id"],
            verifier_invocation=verifier,
        )
        self.assertEqual(first["follow_up"]["status"], "fresh")
        self.assertEqual(replay["follow_up"]["status"], "duplicate")
        recorded = self.harness.planner.backlog.question(
            first["follow_up"]["question_ref"]
        )
        self.assertEqual(recorded["head"]["question"], follow_up_question)
        self.assertEqual(
            recorded["head"]["source_refs"], [claim["id"], assessment["id"]]
        )
        self.assertEqual(
            self.harness.core.connection.execute(
                "SELECT COUNT(*) FROM thesis_versions"
            ).fetchone()[0],
            1,
        )


if __name__ == "__main__":
    unittest.main()
