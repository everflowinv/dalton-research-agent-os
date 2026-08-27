"""Recorded end-to-end canary for ResearchPlan closure -> thesis impact."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from dalton_core.model_router import ModelRouter
from dalton_core.openclaw_model_adapter import (
    OpenClawModelAdapter,
    canonical_hash as broker_hash,
)
from dalton_core.research_plan_closure import ResearchPlanClosureCoordinator
from dalton_core.research_review import HumanReviewAuthority
from dalton_core.store import canonical_json
from dalton_core.thesis_impact import ThesisImpactAuthority
from dalton_core.thesis_impact_budget import ThesisImpactBudgetStore
from dalton_core.thesis_impact_control import (
    ASSESSMENT_BUDGET,
    MAX_CONTROL_PLANE_REDRIVE,
    VERIFIER_BUDGET,
    ResearchPlanThesisImpactConflict,
    ResearchPlanThesisImpactCoordinator,
    ResearchPlanThesisImpactPending,
)
from dalton_core.thesis_impact_model_worker import (
    ResearchPlanThesisImpactRuntime,
    ThesisImpactModelWorker,
    ThesisImpactModelWorkerConflict,
    ThesisImpactModelWorkerRejected,
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
                "confidence": "medium",
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
        self.assertEqual(
            verifier_work["metadata"]["verifier_binding_mode"],
            "wrapper-owned-v1",
        )
        self.assertEqual(
            verifier_work["metadata"]["verifier_decision_schema_version"],
            "0.1",
        )
        self.assertIn(
            "Do not return assessment_ref or assessment_hash",
            verifier_work["question"],
        )
        verifier = self._complete_model(
            work=verifier_work,
            model_family="impact-b",
            output={
                "schema_version": "0.1",
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
        verification = completed["verification"]["verification"]
        self.assertEqual(verification["assessment_ref"], assessment["id"])
        self.assertEqual(
            verification["assessment_hash"], assessment["content_hash"]
        )
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

    def test_terminal_control_plane_failure_redrives_with_bounded_budget(self) -> None:
        self._seed_thesis()
        started = self._close_and_start()
        work = started["impact"]["assessment_work_order"]
        original_id = work["id"]

        def fail_control_plane(work_order: dict) -> None:
            owner = "worker:control-plane"
            lease = self.harness.scheduler().claim(
                owner, work_order_id=work_order["id"]
            )
            identifier = f"control-{work_order['id'].removeprefix('work:')}"
            result = {
                "schema_version": "0.1",
                "id": f"result:{identifier}",
                "created_at": CREATED_AT,
                "work_order_ref": work_order["id"],
                "invocation_ref": f"invocation:not-started:{identifier}",
                "status": "failed",
                "outputs": {},
                "artifact_refs": [],
                "actual_side_effects": [],
                "usage_refs": [],
                "error": {"code": "MODEL_ADAPTER_REJECTED"},
                "metadata": {"control_plane_failure": True},
            }
            self.harness.scheduler().complete(
                work_order["id"],
                lease["attempt"]["attempt_number"],
                owner,
                lease["lease_token"],
                result,
                idempotency_key=f"complete:{identifier}",
            )

        fail_control_plane(work)
        # Before a re-drive the dead WorkOrder parks the phase (live incident
        # 2026-08-27: a missing broker auth key file failed every tick).
        with self.assertRaises(ResearchPlanThesisImpactPending):
            self.control.advance_assessment(
                plan_version_ref=self.harness.plan_wire["id"],
                thesis_ref=self.thesis_ref,
            )
        # After the control plane is repaired the same binding re-drives.
        redriven = self.control.start_from_closed_plan(
            plan_version_ref=self.harness.plan_wire["id"],
            thesis_ref=self.thesis_ref,
        )
        self.assertNotEqual(original_id, redriven["assessment_work_order"]["id"])
        seen = {original_id, redriven["assessment_work_order"]["id"]}
        fail_control_plane(redriven["assessment_work_order"])
        # The budget is bounded: base identity plus MAX_CONTROL_PLANE_REDRIVE
        # re-drives, then a persistent control-plane outage parks the target.
        for _ in range(MAX_CONTROL_PLANE_REDRIVE - 1):
            attempt = self.control.start_from_closed_plan(
                plan_version_ref=self.harness.plan_wire["id"],
                thesis_ref=self.thesis_ref,
            )
            self.assertNotIn(attempt["assessment_work_order"]["id"], seen)
            seen.add(attempt["assessment_work_order"]["id"])
            fail_control_plane(attempt["assessment_work_order"])
        with self.assertRaises(ResearchPlanThesisImpactConflict):
            self.control.start_from_closed_plan(
                plan_version_ref=self.harness.plan_wire["id"],
                thesis_ref=self.thesis_ref,
            )
        # Historical formal failures stay immutable.
        formal = self.harness.scheduler().formal_result(original_id)
        self.assertEqual("failed", formal["result_envelope"]["status"])
        self.assertTrue(
            formal["result_envelope"]["metadata"]["control_plane_failure"]
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
                    "max_output_tokens": 4_000,
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
                    "max_output_tokens": 4_000,
                    "max_total_tokens": 14_000,
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
                text = canonical_json({
                    "schema_version": "0.1",
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

    def _impact_profiles_and_policies(
        self,
        router: ModelRouter,
        *,
        pinned_profile_id: str | None,
    ) -> tuple[str, str, str | None]:
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
                "created_at": "2026-08-22T01:00:00+00:00",
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
                    "max_output_tokens": 4_000,
                },
                "availability": {
                    "state": "available",
                    "checked_at": "2026-08-22T01:00:00+00:00",
                    "valid_until": "2026-08-23T01:00:00+00:00",
                },
                "cost": {
                    "currency": "USD",
                    "input_per_million_usd": cost,
                    "output_per_million_usd": cost * 2,
                },
                "limits": {
                    "max_input_tokens": 10_000,
                    "max_output_tokens": 4_000,
                    "max_total_tokens": 14_000,
                    "max_cost_usd": 1.0,
                },
            }

        profiles = [
            profile(
                "impact-a",
                provider="openai",
                model="gpt-5-impact-a",
                family="impact-family-a",
                cost=0.5,
                capabilities=["research", "verify"],
            ),
            profile(
                "impact-b",
                provider="anthropic",
                model="claude-impact-b",
                family="impact-family-b",
                cost=2.0,
                capabilities=["verify"],
            ),
        ]
        for item in profiles:
            router.register_profile(item)
        shared_ref = "model-routing-policy-version:thesis-impact-pinned-shared:1"
        router.register_policy({
            "schema_version": "0.1",
            "policy_version_ref": shared_ref,
            "id": "model-routing-policy:thesis-impact-pinned-shared",
            "version": 1,
            "created_at": "2026-08-22T01:00:00+00:00",
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
        assessment_ref = "model-routing-policy-version:thesis-impact-assessment:1"
        router.register_policy({
            "schema_version": "0.1",
            "policy_version_ref": assessment_ref,
            "id": "model-routing-policy:thesis-impact-assessment",
            "version": 1,
            "created_at": "2026-08-22T01:00:00+00:00",
            "prior_version_ref": None,
            "filters": {
                "allowed_profile_ids": ["profile:impact-a"],
                "allowed_providers": [],
                "allowed_families": [],
                "allowed_adapter_refs": ["adapter:openclaw-model-broker:0.1"],
                "required_modalities": ["text"],
                "family_independence_capabilities": ["verify"],
            },
            "ordered_preferences": [
                {"field": "profile_version_ref", "direction": "asc"},
            ],
        })
        verifier_ref = None
        if pinned_profile_id is not None:
            verifier_ref = "model-routing-policy-version:thesis-impact-verifier:1"
            router.register_policy({
                "schema_version": "0.1",
                "policy_version_ref": verifier_ref,
                "id": "model-routing-policy:thesis-impact-verifier",
                "version": 1,
                "created_at": "2026-08-22T01:00:00+00:00",
                "prior_version_ref": None,
                "filters": {
                    "allowed_profile_ids": [pinned_profile_id],
                    "allowed_providers": [],
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
        return shared_ref, assessment_ref, verifier_ref

    @staticmethod
    def _recorded_response(
        request_count: Callable[[], int],
        assessment_output: dict[str, Any],
    ) -> Callable[[dict[str, Any]], dict[str, Any]]:
        def response(request: dict[str, Any]) -> dict[str, Any]:
            core = {key: value for key, value in request.items() if key != "auth"}
            if request_count() == 1:
                text = canonical_json(assessment_output)
            else:
                text = canonical_json({
                    "schema_version": "0.1",
                    "verdict": "pass",
                    "findings": [],
                })
            provider, model = core["model"].split("/", 1)
            wire = {
                "schemaVersion": "0.1",
                "brokerVersion": "0.1.0-recorded",
                "runtimeVersion": "2026.8.22",
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

        return response

    def test_phase_pinned_verifier_policy_binds_route_and_thinking_level(self) -> None:
        committed = self._seed_thesis()
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
        fixed_now = datetime(2026, 8, 22, 1, 30, tzinfo=timezone.utc)
        router = ModelRouter(
            Path(self.temp.name) / "pinned-router.sqlite",
            clock=lambda: fixed_now,
        )
        self.addCleanup(router.close)
        shared_ref, assessment_ref, verifier_ref = self._impact_profiles_and_policies(
            router, pinned_profile_id="profile:impact-b"
        )
        broker = FakeBroker(
            Path(self.temp.name),
            self._recorded_response(
                lambda: len(broker.requests), assessment_output
            ),
            connections=2,
        )
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
            routing_policy_ref=shared_ref,
            assessment_routing_policy_ref=assessment_ref,
            verifier_routing_policy_ref=verifier_ref,
            credential_slot_refs=(
                "credential-slot:openai:impact-a",
                "credential-slot:anthropic:impact-b",
            ),
            token_counter=lambda _text: 800,
            clock=lambda: fixed_now,
        )
        runtime = ResearchPlanThesisImpactRuntime(control=self.control, worker=worker)
        completed = runtime.run_once(
            plan_version_ref=self.harness.plan_wire["id"],
            thesis_ref=self.thesis_ref,
        )
        self.assertEqual(completed["status"], "eligible")
        decisions = router.list_decisions()
        self.assertEqual(len(decisions), 2)
        self.assertEqual(decisions[0]["policy_version_ref"], assessment_ref)
        self.assertEqual(
            decisions[0]["selected_endpoint"]["family"], "impact-family-a"
        )
        self.assertEqual(decisions[1]["policy_version_ref"], verifier_ref)
        self.assertEqual(
            decisions[1]["selected_endpoint"]["family"], "impact-family-b"
        )
        self.assertEqual(
            decisions[1]["constraints"]["producer_family"], "impact-family-a"
        )
        self.assertEqual(len(broker.requests), 2)
        self.assertNotIn("requiredControls", broker.requests[0])
        controls = broker.requests[1]["requiredControls"]
        self.assertEqual(controls["thinkingLevel"], "low")
        self.assertEqual(
            controls["structuredOutput"]["schemaName"],
            "thesis_impact_verifier_decision_provider_output_v0_1",
        )
        self.assertEqual(
            completed["assessment"]["verifier_work_order"]["metadata"][
                "verifier_thinking_level"
            ],
            "low",
        )

    def test_verifier_policy_fails_closed_on_same_family_and_unpinned_policy(self) -> None:
        committed = self._seed_thesis()
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
        fixed_now = datetime(2026, 8, 22, 1, 30, tzinfo=timezone.utc)
        router = ModelRouter(
            Path(self.temp.name) / "fail-closed-router.sqlite",
            clock=lambda: fixed_now,
        )
        self.addCleanup(router.close)
        shared_ref, assessment_ref, verifier_ref = self._impact_profiles_and_policies(
            router, pinned_profile_id="profile:impact-a"
        )
        broker = FakeBroker(
            Path(self.temp.name),
            self._recorded_response(
                lambda: len(broker.requests), assessment_output
            ),
            connections=1,
        )
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
            routing_policy_ref=shared_ref,
            assessment_routing_policy_ref=assessment_ref,
            verifier_routing_policy_ref=verifier_ref,
            credential_slot_refs=(
                "credential-slot:openai:impact-a",
                "credential-slot:anthropic:impact-b",
            ),
            token_counter=lambda _text: 800,
            clock=lambda: fixed_now,
        )
        runtime = ResearchPlanThesisImpactRuntime(control=self.control, worker=worker)
        failed = runtime.run_once(
            plan_version_ref=self.harness.plan_wire["id"],
            thesis_ref=self.thesis_ref,
        )
        self.assertEqual(failed["status"], "verification_failed")
        verifier_route = failed["verifier_run"]["route"]
        self.assertEqual(verifier_route["outcome"], "rejected")
        self.assertIn(
            "model_family_not_independent", verifier_route["rejection_reasons"]
        )
        self.assertEqual(len(broker.requests), 1)
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
            0,
        )

        unpinned = ThesisImpactModelWorker(
            scheduler=self.harness.scheduler(),
            router=router,
            adapter=adapter,
            impact=self.impact,
            observability=self.harness.observability,
            routing_policy_ref=shared_ref,
            assessment_routing_policy_ref=assessment_ref,
            verifier_routing_policy_ref=shared_ref,
            credential_slot_refs=(
                "credential-slot:openai:impact-a",
                "credential-slot:anthropic:impact-b",
            ),
            token_counter=lambda _text: 800,
            clock=lambda: fixed_now,
        )
        with self.assertRaisesRegex(
            ThesisImpactModelWorkerRejected, "not pinned to exactly one profile"
        ):
            unpinned._phase_policy_ref("verification")
        self.assertEqual(len(broker.requests), 1)

        assessment_unpinned = ThesisImpactModelWorker(
            scheduler=self.harness.scheduler(),
            router=router,
            adapter=adapter,
            impact=self.impact,
            observability=self.harness.observability,
            routing_policy_ref=shared_ref,
            assessment_routing_policy_ref=shared_ref,
            credential_slot_refs=(
                "credential-slot:openai:impact-a",
                "credential-slot:anthropic:impact-b",
            ),
            token_counter=lambda _text: 800,
            clock=lambda: fixed_now,
        )
        with self.assertRaisesRegex(
            ThesisImpactModelWorkerRejected,
            "assessment routing policy is not pinned to exactly one profile",
        ):
            assessment_unpinned._phase_policy_ref("assessment")

    def test_host_completion_failure_retries_within_bounded_attempts(self) -> None:
        committed = self._seed_thesis()
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
        fixed_now = datetime(2026, 8, 22, 1, 30, tzinfo=timezone.utc)
        router = ModelRouter(
            Path(self.temp.name) / "host-retry-router.sqlite",
            clock=lambda: fixed_now,
        )
        self.addCleanup(router.close)
        shared_ref, assessment_ref, verifier_ref = self._impact_profiles_and_policies(
            router, pinned_profile_id=None
        )

        def responder(request: dict[str, Any]) -> dict[str, Any]:
            core = {key: value for key, value in request.items() if key != "auth"}
            provider, model = core["model"].split("/", 1)
            wire: dict[str, Any] = {
                "schemaVersion": "0.1",
                "brokerVersion": "0.1.0-recorded",
                "runtimeVersion": "2026.8.22",
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
            if len(broker.requests) == 1:
                wire["text"] = canonical_json(assessment_output)
            elif len(broker.requests) == 2:
                # Live 2026-08-27 shape: the broker reported a transient host
                # completion failure for the verifier's first attempt.
                wire.update({
                    "ok": False,
                    "provider": None,
                    "model": None,
                    "canonicalModel": None,
                    "agentId": None,
                    "text": None,
                    "usage": {
                        "inputTokens": None,
                        "outputTokens": None,
                        "cacheReadTokens": None,
                        "cacheWriteTokens": None,
                        "totalTokens": None,
                    },
                    "cost": {"available": False, "usd": None},
                    "error": {
                        "code": "HOST_COMPLETION_FAILED",
                        "message": "host completion failed",
                    },
                })
            else:
                wire["text"] = canonical_json({
                    "schema_version": "0.1",
                    "verdict": "pass",
                    "findings": [],
                })
            wire["contentHash"] = broker_hash(wire)
            return wire

        broker = FakeBroker(Path(self.temp.name), responder, connections=3)
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
            routing_policy_ref=shared_ref,
            assessment_routing_policy_ref=assessment_ref,
            verifier_routing_policy_ref=verifier_ref,
            credential_slot_refs=(
                "credential-slot:openai:impact-a",
                "credential-slot:anthropic:impact-b",
            ),
            token_counter=lambda _text: 800,
            clock=lambda: fixed_now,
        )
        runtime = ResearchPlanThesisImpactRuntime(control=self.control, worker=worker)
        # First pass: the verifier's first attempt dies in the host; the
        # bounded retry keeps the phase runnable instead of parking it.
        first = runtime.run_once(
            plan_version_ref=self.harness.plan_wire["id"],
            thesis_ref=self.thesis_ref,
        )
        self.assertEqual("verification_retryable", first["status"])
        self.assertEqual(2, len(broker.requests))
        # Second pass: the retried verifier attempt completes and passes.
        second = runtime.run_once(
            plan_version_ref=self.harness.plan_wire["id"],
            thesis_ref=self.thesis_ref,
        )
        self.assertEqual("eligible", second["status"])
        self.assertEqual(3, len(broker.requests))
        self.assertEqual(
            1,
            self.harness.core.connection.execute(
                "SELECT COUNT(*) FROM thesis_impact_verifications"
            ).fetchone()[0],
        )

    def test_day_budget_gates_paid_calls_and_alerts(self) -> None:
        committed = self._seed_thesis()
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
        fixed_now = datetime(2026, 8, 22, 1, 30, tzinfo=timezone.utc)
        router = ModelRouter(
            Path(self.temp.name) / "budget-router.sqlite",
            clock=lambda: fixed_now,
        )
        self.addCleanup(router.close)
        shared_ref, assessment_ref, _ = self._impact_profiles_and_policies(
            router, pinned_profile_id=None
        )
        budget = ThesisImpactBudgetStore(
            Path(self.temp.name) / "budget.sqlite", clock=lambda: fixed_now
        )
        self.addCleanup(budget.close)
        budget.register_policy(
            policy_version_id="budget-policy:thesis-impact-day:1",
            day_cap_micros=250_500,
        )
        broker = FakeBroker(
            Path(self.temp.name),
            self._recorded_response(
                lambda: len(broker.requests), assessment_output
            ),
            connections=2,
        )
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
            routing_policy_ref=shared_ref,
            assessment_routing_policy_ref=assessment_ref,
            credential_slot_refs=(
                "credential-slot:openai:impact-a",
                "credential-slot:anthropic:impact-b",
            ),
            budget=budget,
            budget_policy_version_id="budget-policy:thesis-impact-day:1",
            token_counter=lambda _text: 800,
            clock=lambda: fixed_now,
        )
        runtime = ResearchPlanThesisImpactRuntime(control=self.control, worker=worker)
        failed = runtime.run_once(
            plan_version_ref=self.harness.plan_wire["id"],
            thesis_ref=self.thesis_ref,
        )
        self.assertEqual(failed["status"], "verification_failed")
        rejection = failed["verifier_run"]["budget_rejection"]
        self.assertEqual(rejection["day_cap_micros"], 250_500)
        self.assertEqual(rejection["day_committed_micros"], 1_000)
        self.assertEqual(rejection["reserved_micros"], 250_000)
        self.assertEqual(rejection["phase"], "verification")
        self.assertEqual(len(broker.requests), 1)
        self.assertEqual(
            budget.connection.execute(
                "SELECT COUNT(*) FROM thesis_impact_day_admissions"
            ).fetchone()[0],
            1,
        )
        self.assertEqual(
            budget.connection.execute(
                "SELECT COUNT(*) FROM thesis_impact_day_settlements"
            ).fetchone()[0],
            1,
        )
        self.assertEqual(
            budget.connection.execute(
                "SELECT COUNT(*) FROM thesis_impact_day_rejections"
            ).fetchone()[0],
            1,
        )
        summary = budget.day_summary(
            policy_version_id="budget-policy:thesis-impact-day:1",
            day="2026-08-22",
        )
        self.assertEqual(summary["committed_micros"], 1_000)
        self.assertEqual(summary["remaining_micros"], 249_500)
        alerts = budget.pending_alerts()
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]["kind"], "day_budget_exceeded")
        self.assertEqual(alerts[0]["severity"], "high")
        self.assertEqual(alerts[0]["phase"], "verification")
        verifier_ref = failed["verifier_run"]["work_order_ref"]
        formal = self.harness.scheduler().formal_result(verifier_ref)
        self.assertEqual(formal["terminal_state"], "failed")
        self.assertEqual(
            formal["result_envelope"]["error"]["code"], "DAY_BUDGET_EXCEEDED"
        )
        self.assertEqual(
            self.harness.core.connection.execute(
                "SELECT COUNT(*) FROM thesis_impact_verifications"
            ).fetchone()[0],
            0,
        )

        replay = runtime.run_once(
            plan_version_ref=self.harness.plan_wire["id"],
            thesis_ref=self.thesis_ref,
        )
        self.assertEqual(replay["status"], "verification_failed")
        self.assertEqual(len(broker.requests), 1)
        self.assertEqual(len(budget.pending_alerts()), 1)

    def test_worker_recovers_accounted_model_result_without_second_provider_call(self) -> None:
        committed = self._seed_thesis()
        started = self._close_and_start()
        work = started["impact"]["assessment_work_order"]
        recovery_miss_work_id = (
            "work:thesis-impact-recovery-miss-"
            + hashlib.sha256(work["id"].encode("utf-8")).hexdigest()[:32]
        )
        claim = self.harness.core.get_claim(
            started["impact"]["claim_version_ref"]
        )["claim"]
        thesis = self.harness.core.get_version(committed["version_id"])["content"]
        output = {
            "schema_version": "0.1",
            "claim_version_ref": claim["id"],
            "claim_version_hash": claim["content_hash"],
            "thesis_version_ref": thesis["id"],
            "thesis_version_hash": thesis["content_hash"],
            "driver_statement": thesis["mechanism"],
            "impact": "supports",
            "rationale": "The exact reported growth is consistent with the bound driver.",
            "follow_up_question": None,
        }
        router = ModelRouter(
            Path(self.temp.name) / "impact-recovery-router.sqlite",
            clock=self.harness.clock,
        )
        self.addCleanup(router.close)
        profile_ref = "model-profile-version:impact-recovery:1"
        router.register_profile({
            "schema_version": "0.1",
            "profile_version_ref": profile_ref,
            "id": "profile:impact-recovery",
            "version": 1,
            "created_at": "2026-08-15T07:00:00+00:00",
            "prior_version_ref": None,
            "provider": "openai",
            "model": "gpt-5-impact-recovery",
            "family": "impact-family-recovery",
            "adapter_ref": "adapter:openclaw-model-broker:0.1",
            "credential_slot_ref": "credential-slot:openai:impact-recovery",
            "capabilities": ["research"],
            "modalities": ["text"],
            "context": {
                "max_context_tokens": 100_000,
                "max_output_tokens": 4_000,
            },
            "availability": {
                "state": "available",
                "checked_at": "2026-08-15T07:30:00+00:00",
                "valid_until": "2026-08-16T08:00:00+00:00",
            },
            "cost": {
                "currency": "USD",
                "input_per_million_usd": 0.5,
                "output_per_million_usd": 1.0,
            },
            "limits": {
                "max_input_tokens": 10_000,
                "max_output_tokens": 4_000,
                "max_total_tokens": 14_000,
                "max_cost_usd": 1.0,
            },
        })
        policy_ref = "model-routing-policy-version:impact-recovery:1"
        router.register_policy({
            "schema_version": "0.1",
            "policy_version_ref": policy_ref,
            "id": "model-routing-policy:impact-recovery",
            "version": 1,
            "created_at": "2026-08-15T07:00:00+00:00",
            "prior_version_ref": None,
            "filters": {
                "allowed_profile_ids": ["profile:impact-recovery"],
                "allowed_providers": ["openai"],
                "allowed_families": [],
                "allowed_adapter_refs": ["adapter:openclaw-model-broker:0.1"],
                "required_modalities": ["text"],
                "family_independence_capabilities": [],
            },
            "ordered_preferences": [
                {"field": "estimated_cost_usd", "direction": "asc"},
                {"field": "profile_version_ref", "direction": "asc"},
            ],
        })

        provider_calls = 0

        def response(request: dict[str, Any]) -> dict[str, Any]:
            nonlocal provider_calls
            replay_only = request.get("replayOnly") is True
            if not replay_only:
                provider_calls += 1
            core = {
                key: value
                for key, value in request.items()
                if key not in {"auth", "replayOnly"}
            }
            text = canonical_json(output)
            recovery_miss = core["workOrderId"] == recovery_miss_work_id
            wire = {
                "schemaVersion": "0.1",
                "brokerVersion": "0.1.0-recorded",
                "runtimeVersion": "2026.8.21",
                "invocationId": core["invocationId"],
                "workOrderId": core["workOrderId"],
                "profileId": core["profileId"],
                "requestHash": broker_hash(core),
                "idempotencyStatus": (
                    "fresh" if recovery_miss else (
                        "duplicate" if replay_only else "fresh"
                    )
                ),
                "ok": not recovery_miss,
                "provider": None if recovery_miss else "openai",
                "model": None if recovery_miss else "gpt-5-impact-recovery",
                "canonicalModel": None if recovery_miss else core["model"],
                "agentId": None if recovery_miss else "dalton-model-broker",
                "text": None if recovery_miss else text,
                "usage": (
                    {
                        "inputTokens": None,
                        "outputTokens": None,
                        "cacheReadTokens": None,
                        "cacheWriteTokens": None,
                        "totalTokens": None,
                    }
                    if recovery_miss
                    else {
                        "inputTokens": 100,
                        "outputTokens": 50,
                        "cacheReadTokens": 0,
                        "cacheWriteTokens": None,
                        "totalTokens": 150,
                    }
                ),
                "cost": (
                    {"available": False, "usd": None}
                    if recovery_miss
                    else {"available": True, "usd": 0.001}
                ),
                "error": (
                    {
                        "code": "IDEMPOTENCY_MISS",
                        "message": (
                            "no durable completion exists; replay-only request "
                            "did not call the host"
                        ),
                    }
                    if recovery_miss
                    else None
                ),
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
            clock=self.harness.clock,
        )
        faulted = False

        def crash_after_accounting(seam: str) -> None:
            nonlocal faulted
            if seam == "after_model_accounting" and not faulted:
                faulted = True
                raise RuntimeError("injected crash after model accounting")

        worker = ThesisImpactModelWorker(
            scheduler=self.harness.scheduler(),
            router=router,
            adapter=adapter,
            impact=self.impact,
            observability=self.harness.observability,
            routing_policy_ref=policy_ref,
            credential_slot_refs=("credential-slot:openai:impact-recovery",),
            token_counter=lambda _text: 800,
            clock=self.harness.clock,
            fault_hook=crash_after_accounting,
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

        with self.assertRaisesRegex(RuntimeError, "after model accounting"):
            worker.run_once(work)
        self.assertEqual(provider_calls, 1)
        self.assertIsNone(self.harness.scheduler().formal_result(work["id"]))
        self.assertEqual(len(router.list_decisions(work_order_id=work["id"])), 1)

        self.harness.clock.value += timedelta(seconds=31)
        recovered = worker.run_once(work)
        self.assertEqual(recovered["status"], "succeeded")
        self.assertTrue(recovered["route_replayed"])
        self.assertEqual(
            recovered["result"]["metadata"]["broker_request_mode"],
            "replay_only",
        )
        self.assertEqual(provider_calls, 1)
        self.assertEqual(len(broker.requests), 2)
        self.assertNotIn("replayOnly", broker.requests[0])
        self.assertIs(broker.requests[1]["replayOnly"], True)
        self.assertEqual(len(router.list_decisions(work_order_id=work["id"])), 1)
        formal = self.harness.scheduler().formal_result(work["id"])
        self.assertEqual(formal["attempt_number"], 2)
        self.assertEqual(
            self.harness.core.connection.execute(
                "SELECT COUNT(*) FROM model_invocations"
            ).fetchone()[0],
            base_invocations + 1,
        )
        self.assertEqual(
            self.harness.core.connection.execute(
                "SELECT COUNT(*) FROM observability_usage_entries"
            ).fetchone()[0],
            base_usage + 1,
        )
        self.assertEqual(
            self.harness.core.connection.execute(
                "SELECT COUNT(*) FROM observability_cost_entries"
            ).fetchone()[0],
            base_cost + 1,
        )
        assessed = self.control.advance_assessment(
            plan_version_ref=self.harness.plan_wire["id"],
            thesis_ref=self.thesis_ref,
        )
        self.assertEqual(assessed["assessment"]["status"], "fresh")

        recovery_miss_work = {
            **work,
            "id": recovery_miss_work_id,
            "idempotency_key": f"work-key:{recovery_miss_work_id}",
        }
        self.harness.scheduler().enqueue(recovery_miss_work)
        self.harness.scheduler().claim(
            "worker:crash-before-broker",
            work_order_id=recovery_miss_work_id,
        )
        router.route(
            recovery_miss_work,
            attempt_number=1,
            capability="research",
            policy_version_ref=policy_ref,
            credential_slot_refs=("credential-slot:openai:impact-recovery",),
            required_modalities=("text",),
            required_context_tokens=800 + work["budget"]["max_output_tokens"],
            estimated_input_tokens=800,
            estimated_output_tokens=work["budget"]["max_output_tokens"],
            idempotency_key=f"route-key:{recovery_miss_work_id}:1",
        )
        self.harness.clock.value += timedelta(seconds=31)
        recovery_miss = worker.run_once(recovery_miss_work)
        self.assertEqual(recovery_miss["status"], "failed")
        self.assertIn("result", recovery_miss, recovery_miss)
        self.assertEqual(
            recovery_miss["result"]["error"]["code"], "MODEL_RECOVERY_MISS"
        )
        self.assertEqual(provider_calls, 1)
        self.assertEqual(len(broker.requests), 3)
        self.assertIs(broker.requests[2]["replayOnly"], True)
        self.assertEqual(
            len(router.list_decisions(work_order_id=recovery_miss_work_id)), 1
        )
        miss_formal = self.harness.scheduler().formal_result(recovery_miss_work_id)
        self.assertEqual(miss_formal["attempt_number"], 2)
        self.assertEqual(
            self.harness.core.connection.execute(
                "SELECT COUNT(*) FROM model_invocations"
            ).fetchone()[0],
            base_invocations + 1,
        )
        self.assertEqual(
            self.harness.core.connection.execute(
                "SELECT COUNT(*) FROM observability_usage_entries"
            ).fetchone()[0],
            base_usage + 1,
        )
        self.assertEqual(
            self.harness.core.connection.execute(
                "SELECT COUNT(*) FROM observability_cost_entries"
            ).fetchone()[0],
            base_cost + 1,
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
        self.assertEqual(
            router.connection.execute("PRAGMA integrity_check").fetchone()[0], "ok"
        )


class ThesisImpactModelWorkerUnitTests(unittest.TestCase):
    @staticmethod
    def finding(code, expected_impact=None):
        severities = {
            "binding_mismatch": "high",
            "driver_mismatch": "high",
            "impact_mismatch": "high",
            "follow_up_quality": "medium",
        }
        return {
            "code": code,
            "severity": severities[code],
            "detail": "Recorded calibration condition.",
            "expected_impact": expected_impact,
        }

    def test_authority_consistency_rejects_self_contradictory_findings(self) -> None:
        assessment = {
            "driver_statement": "Revenue growth sustains operating leverage.",
            "impact": "insufficient",
        }
        for finding, message in (
            (self.finding("binding_mismatch"), "binding mismatch"),
            (self.finding("driver_mismatch"), "driver mismatch"),
            (
                self.finding("impact_mismatch", expected_impact="insufficient"),
                "repeats the assessed impact",
            ),
        ):
            with self.subTest(code=finding["code"]):
                with self.assertRaisesRegex(ThesisImpactModelWorkerConflict, message):
                    ThesisImpactModelWorker._validate_verifier_authority_consistency(
                        {"schema_version": "0.2", "findings": [finding]},
                        assessment,
                        "Revenue growth sustains operating leverage.",
                    )

        with self.assertRaisesRegex(
            ThesisImpactModelWorkerConflict, "applies only to an insufficient"
        ):
            ThesisImpactModelWorker._validate_verifier_authority_consistency(
                {
                    "schema_version": "0.2",
                    "findings": [self.finding("follow_up_quality")],
                },
                {**assessment, "impact": "supports"},
                "Revenue growth sustains operating leverage.",
            )

        ThesisImpactModelWorker._validate_verifier_authority_consistency(
            {
                "schema_version": "0.2",
                "findings": [
                    self.finding("impact_mismatch", expected_impact="supports")
                ],
            },
            assessment,
            "Revenue growth sustains operating leverage.",
        )

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
