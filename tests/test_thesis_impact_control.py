"""Recorded end-to-end canary for ResearchPlan closure -> thesis impact."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dalton_core.model_router import ModelRouter
from dalton_core.openclaw_model_adapter import (
    OpenClawModelAdapter,
    canonical_hash as broker_hash,
)
from dalton_core.research_plan_closure import ResearchPlanClosureCoordinator
from dalton_core.research_review import HumanReviewAuthority
from dalton_core.store import canonical_json
from dalton_core.thesis_impact import ThesisImpactAuthority
from dalton_core.thesis_impact_control import (
    ASSESSMENT_BUDGET,
    VERIFIER_BUDGET,
    ResearchPlanThesisImpactCoordinator,
)
from dalton_core.thesis_impact_model_worker import (
    ResearchPlanThesisImpactRuntime,
    ThesisImpactModelWorker,
)
from tests.test_openclaw_model_adapter import FakeBroker
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

    def test_routed_worker_retries_contract_then_verifies_independently(self) -> None:
        committed = self._seed_thesis()
        before_pointer = dict(self.harness.core.current_pointer(self.thesis_ref))
        started = self._close_and_start()
        claim = self.harness.core.get_claim(
            started["impact"]["claim_version_ref"]
        )["claim"]
        thesis = self.harness.core.get_version(committed["version_id"])["content"]
        assessment_output = {
            "schema_version": "0.1",
            "claim_version_ref": claim["id"],
            "claim_version_hash": claim["content_hash"],
            "thesis_version_ref": thesis["id"],
            "thesis_version_hash": thesis["content_hash"],
            "driver_statement": thesis["mechanism"],
            "impact": "supports",
            "rationale": (
                "The exact reported growth is directionally consistent with the "
                "bound operating-leverage driver."
            ),
            "follow_up_question": None,
        }
        fixed_now = datetime(2026, 8, 21, 1, 30, tzinfo=timezone.utc)
        router = ModelRouter(
            Path(self.temp.name) / "impact-router.sqlite",
            clock=lambda: fixed_now,
        )
        self.addCleanup(router.close)

        def profile(
            name: str,
            *,
            provider: str,
            model: str,
            family: str,
            cost: float,
            capabilities: list[str],
        ) -> dict[str, Any]:
            return {
                "schema_version": "0.1",
                "profile_version_ref": f"model-profile-version:{name}:1",
                "id": f"profile:{name}",
                "version": 1,
                "created_at": "2026-08-21T01:00:00+00:00",
                "prior_version_ref": None,
                "provider": provider,
                "model": model,
                "family": family,
                "adapter_ref": "adapter:openclaw-model-broker:0.1",
                "credential_slot_ref": f"credential-slot:{provider}:{name}",
                "capabilities": capabilities,
                "modalities": ["text"],
                "context": {
                    "max_context_tokens": 100_000,
                    "max_output_tokens": 2_000,
                },
                "availability": {
                    "state": "available",
                    "checked_at": "2026-08-21T01:00:00+00:00",
                    "valid_until": "2026-08-22T01:00:00+00:00",
                },
                "cost": {
                    "currency": "USD",
                    "input_per_million_usd": cost,
                    "output_per_million_usd": cost * 2,
                },
                "limits": {
                    "max_input_tokens": 10_000,
                    "max_output_tokens": 2_000,
                    "max_total_tokens": 12_000,
                    "max_cost_usd": 1.0,
                },
            }

        first_profile = profile(
            "impact-a",
            provider="openai",
            model="gpt-5-impact-a",
            family="impact-family-a",
            cost=0.5,
            capabilities=["research", "verify"],
        )
        second_profile = profile(
            "impact-b",
            provider="anthropic",
            model="claude-impact-b",
            family="impact-family-b",
            cost=2.0,
            capabilities=["verify"],
        )
        router.register_profile(first_profile)
        router.register_profile(second_profile)
        policy_ref = "model-routing-policy-version:thesis-impact:1"
        router.register_policy({
            "schema_version": "0.1",
            "policy_version_ref": policy_ref,
            "id": "model-routing-policy:thesis-impact",
            "version": 1,
            "created_at": "2026-08-21T01:00:00+00:00",
            "prior_version_ref": None,
            "filters": {
                "allowed_profile_ids": ["profile:impact-a", "profile:impact-b"],
                "allowed_providers": ["openai", "anthropic"],
                "allowed_families": [],
                "allowed_adapter_refs": ["adapter:openclaw-model-broker:0.1"],
                "required_modalities": ["text"],
                "family_independence_capabilities": ["verify"],
            },
            "ordered_preferences": [
                {"field": "estimated_cost_usd", "direction": "asc"},
                {"field": "profile_version_ref", "direction": "asc"},
            ],
        })

        def response(request: dict[str, Any]) -> dict[str, Any]:
            core = {key: value for key, value in request.items() if key != "auth"}
            index = len(broker.requests)
            if index == 1:
                text = canonical_json({"unexpected": True})
            elif index == 2:
                text = canonical_json(assessment_output)
            else:
                marker = "\nASSESSMENT_CANONICAL_JSON:\n"
                assessment = json.loads(request["prompt"].split(marker, 1)[1])
                text = canonical_json({
                    "schema_version": "0.1",
                    "assessment_ref": assessment["id"],
                    "assessment_hash": assessment["content_hash"],
                    "verdict": "pass",
                    "findings": [],
                })
            provider, model = core["model"].split("/", 1)
            wire = {
                "schemaVersion": "0.1",
                "brokerVersion": "0.1.0-recorded",
                "runtimeVersion": "2026.8.21",
                "invocationId": core["invocationId"],
                "workOrderId": core["workOrderId"],
                "profileId": core["profileId"],
                "requestHash": broker_hash(core),
                "idempotencyStatus": "fresh",
                "ok": True,
                "provider": provider,
                "model": model,
                "canonicalModel": core["model"],
                "agentId": "dalton-model-broker",
                "text": text,
                "usage": {
                    "inputTokens": 100,
                    "outputTokens": 50,
                    "cacheReadTokens": 0,
                    "cacheWriteTokens": None,
                    "totalTokens": 150,
                },
                "cost": {"available": True, "usd": 0.001},
                "error": None,
            }
            wire["contentHash"] = broker_hash(wire)
            return wire

        broker = FakeBroker(Path(self.temp.name), response, connections=3)
        self.addCleanup(broker.close)
        adapter = OpenClawModelAdapter(
            broker.path,
            route_resolver=router.get_decision,
            auth_client_id="client:thesis-impact-runtime",
            auth_key_provider=lambda: b"a" * 64,
            timeout_seconds=1.0,
            clock=lambda: fixed_now,
        )
        worker = ThesisImpactModelWorker(
            scheduler=self.harness.scheduler(),
            router=router,
            adapter=adapter,
            impact=self.impact,
            observability=self.harness.observability,
            routing_policy_ref=policy_ref,
            credential_slot_refs=(
                "credential-slot:openai:impact-a",
                "credential-slot:anthropic:impact-b",
            ),
            token_counter=lambda _text: 800,
            clock=lambda: fixed_now,
        )
        runtime = ResearchPlanThesisImpactRuntime(
            control=self.control, worker=worker
        )
        base_invocations = self.harness.core.connection.execute(
            "SELECT COUNT(*) FROM model_invocations"
        ).fetchone()[0]
        base_usage = self.harness.core.connection.execute(
            "SELECT COUNT(*) FROM observability_usage_entries"
        ).fetchone()[0]
        base_cost = self.harness.core.connection.execute(
            "SELECT COUNT(*) FROM observability_cost_entries"
        ).fetchone()[0]

        rejected = runtime.run_once(
            plan_version_ref=self.harness.plan_wire["id"],
            thesis_ref=self.thesis_ref,
        )
        self.assertEqual(rejected["status"], "assessment_retryable")
        self.assertEqual(
            rejected["assessment_run"]["output_error"],
            "ThesisImpactValidationError",
        )
        self.assertEqual(
            self.harness.core.connection.execute(
                "SELECT COUNT(*) FROM thesis_impact_assessments"
            ).fetchone()[0],
            0,
        )
        self.assertIsNone(
            self.harness.scheduler().formal_result(
                started["impact"]["assessment_work_order"]["id"]
            )
        )

        completed = runtime.run_once(
            plan_version_ref=self.harness.plan_wire["id"],
            thesis_ref=self.thesis_ref,
        )
        self.assertEqual(completed["status"], "eligible")
        self.assertEqual(
            completed["final"]["eligible"]["assessment"]["impact"], "supports"
        )
        self.assertEqual(
            self.harness.core.current_pointer(self.thesis_ref), before_pointer
        )
        decisions = router.list_decisions()
        self.assertEqual(len(decisions), 3)
        self.assertEqual(
            [item["selected_endpoint"]["family"] for item in decisions],
            ["impact-family-a", "impact-family-a", "impact-family-b"],
        )
        self.assertEqual(decisions[1]["decision_kind"], "retry")
        self.assertEqual(decisions[1]["previous_decision_ref"], decisions[0]["id"])
        self.assertEqual(
            decisions[2]["constraints"]["producer_family"], "impact-family-a"
        )
        self.assertEqual(
            self.harness.core.connection.execute(
                "SELECT COUNT(*) FROM model_invocations"
            ).fetchone()[0],
            base_invocations + 3,
        )
        self.assertEqual(
            self.harness.core.connection.execute(
                "SELECT COUNT(*) FROM observability_usage_entries"
            ).fetchone()[0],
            base_usage + 3,
        )
        self.assertEqual(
            self.harness.core.connection.execute(
                "SELECT COUNT(*) FROM observability_cost_entries"
            ).fetchone()[0],
            base_cost + 3,
        )
        self.assertEqual(
            self.review.connection.execute(
                "SELECT COUNT(*) FROM human_review_decisions"
            ).fetchone()[0],
            0,
        )
        self.assertEqual(len(broker.requests), 3)

        replay = runtime.run_once(
            plan_version_ref=self.harness.plan_wire["id"],
            thesis_ref=self.thesis_ref,
        )
        self.assertEqual(replay["status"], "eligible")
        self.assertTrue(replay["assessment_run"]["replayed"])
        self.assertTrue(replay["verifier_run"]["replayed"])
        self.assertEqual(len(broker.requests), 3)
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
            self.harness.core.connection.execute("PRAGMA integrity_check").fetchone()[0],
            "ok",
        )
        self.assertEqual(
            self.harness.scheduler().connection.execute(
                "PRAGMA integrity_check"
            ).fetchone()[0],
            "ok",
        )
        self.assertEqual(router.connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")


class ThesisImpactModelWorkerUnitTests(unittest.TestCase):
    def test_retry_status_becomes_formal_failure_at_attempt_limit(self) -> None:
        self.assertEqual(
            ThesisImpactModelWorker._bounded_failure_status({
                "attempt": {"attempt_number": 2}, "max_attempts": 3
            }),
            "retryable",
        )
        self.assertEqual(
            ThesisImpactModelWorker._bounded_failure_status({
                "attempt": {"attempt_number": 3}, "max_attempts": 3
            }),
            "failed",
        )


if __name__ == "__main__":
    unittest.main()
