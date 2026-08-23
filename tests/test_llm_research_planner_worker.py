from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from dalton_core.contracts import (
    InvocationGranularity,
    ModelInvocation,
    ResultEnvelope,
    WorkOrder,
)
from dalton_core.llm_research_planner import build_planner_work_order
from dalton_core.llm_research_planner_worker import LLMResearchPlannerModelWorker
from dalton_core.model_router import ModelRouter
from dalton_core.observability import ObservabilityStore
from dalton_core.scheduler import Scheduler
from dalton_core.store import DaltonStore


NOW = datetime(2026, 8, 23, 18, 0, tzinfo=timezone.utc)


def profile() -> dict:
    return {
        "schema_version": "0.1",
        "profile_version_ref": "model-profile-version:test-planner:1",
        "id": "profile:test-planner",
        "version": 1,
        "created_at": NOW.isoformat(),
        "prior_version_ref": None,
        "provider": "test",
        "model": "planner",
        "family": "test-planner",
        "adapter_ref": "adapter:openclaw-model-broker:0.1",
        "credential_slot_ref": "credential-slot:openclaw:test",
        "capabilities": ["research"],
        "modalities": ["text"],
        "context": {"max_context_tokens": 100_000, "max_output_tokens": 8_000},
        "availability": {
            "state": "available",
            "checked_at": NOW.isoformat(),
            "valid_until": "2026-08-24T18:00:00+00:00",
        },
        "cost": {
            "currency": "USD",
            "input_per_million_usd": 1.0,
            "output_per_million_usd": 2.0,
        },
        "limits": {
            "max_input_tokens": 90_000,
            "max_output_tokens": 8_000,
            "max_total_tokens": 98_000,
            "max_cost_usd": 20.0,
        },
    }


def policy() -> dict:
    return {
        "schema_version": "0.1",
        "policy_version_ref": "model-routing-policy-version:test-planner:1",
        "id": "model-routing-policy:test-planner",
        "version": 1,
        "created_at": NOW.isoformat(),
        "prior_version_ref": None,
        "filters": {
            "allowed_profile_ids": ["profile:test-planner"],
            "allowed_providers": [],
            "allowed_families": [],
            "allowed_adapter_refs": ["adapter:openclaw-model-broker:0.1"],
            "required_modalities": ["text"],
            "family_independence_capabilities": [],
        },
        "ordered_preferences": [
            {"field": "profile_version_ref", "direction": "asc"}
        ],
    }


def context() -> dict:
    return {
        "id": "planner-context-pack-version:" + "1" * 32,
        "content_hash": "a" * 64,
        "created_at": NOW.isoformat(),
        "loop_version_ref": "bounded-planner-loop-version:test:1",
        "loop_version_hash": "b" * 64,
        "round_ordinal": 1,
        "question_input": {"ref": "question:test", "hash": "c" * 64, "quoted_data": {}},
        "doctrine_input": {"ref": "doctrine:test", "hash": "d" * 64, "quoted_data": {}},
        "selected_lens_ref": "lens:test",
        "selected_lens": {"priority_topics": ["commitments"]},
        "override_input": None,
        "driver_pack_input": None,
        "thesis_inputs": [],
        "outcome_inputs": [],
        "directive_inputs": [],
        "remaining_budget": {
            "rounds_remaining": 1,
            "cost_units_remaining": 1,
            "seconds_remaining": 10,
        },
        "catalog_inputs": [{"coverage_item_ref": "commitments"}],
    }


class FakeAdapter:
    def __init__(self, candidate: dict) -> None:
        self.candidate = candidate

    def replay(self, work: WorkOrder, route: dict, selected: dict):
        raise AssertionError("fresh worker should not replay")

    def execute(self, work: WorkOrder, route: dict, selected: dict):
        text = json.dumps(self.candidate, separators=(",", ":"))
        invocation = ModelInvocation(
            schema_version="0.1",
            id="invocation:test-planner-worker",
            created_at=NOW.isoformat(),
            work_order_ref=work.id,
            profile_ref=selected["profile_version_ref"],
            granularity=InvocationGranularity.TASK,
            capability="research",
            provider=selected["provider"],
            model=selected["model"],
            model_family=selected["family"],
            input_refs=work.input_refs,
            output_refs=(),
            started_at=NOW.isoformat(),
            completed_at=NOW.isoformat(),
            usage={
                "input_tokens": 100,
                "output_tokens": 20,
                "total_tokens": 120,
                "cache_read_tokens": None,
                "cache_write_tokens": None,
                "raw_provider_telemetry": {
                    "cost": {"available": True, "usd": 0.001}
                },
            },
            side_effects=(),
            runtime_ref=selected["adapter_ref"],
            actor_ref="broker:test",
            parent_ref=route["id"],
            environment_hash="environment:test",
        )
        result = ResultEnvelope(
            schema_version="0.1",
            id="result:test-planner-worker",
            created_at=NOW.isoformat(),
            work_order_ref=work.id,
            invocation_ref=invocation.id,
            status="succeeded",
            outputs={
                "text": text,
                "content_hash": hashlib.sha256(text.encode()).hexdigest(),
            },
            actual_side_effects=(),
            usage_refs=(f"usage:{invocation.id}",),
            artifact_refs=(),
            error=None,
            metadata={
                "route_decision_ref": route["id"],
                "profile_version_ref": selected["profile_version_ref"],
            },
        )
        return invocation, result


class LLMResearchPlannerWorkerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = DaltonStore(Path(self.temp.name) / "core.sqlite")
        self.addCleanup(self.store.close)
        self.scheduler = Scheduler(connection=self.store.connection, clock=lambda: NOW)
        self.observability = ObservabilityStore(self.store)
        self.router = ModelRouter(clock=lambda: NOW)
        self.addCleanup(self.router.close)
        self.assertEqual(self.router.register_profile(profile())["status"], "fresh")
        self.assertEqual(self.router.register_policy(policy())["status"], "fresh")

    def _work(self) -> WorkOrder:
        work = build_planner_work_order(context())
        self.assertEqual(self.scheduler.enqueue(work)["status"], "fresh")
        return work

    def _worker(self, candidate: dict) -> LLMResearchPlannerModelWorker:
        return LLMResearchPlannerModelWorker(
            scheduler=self.scheduler,
            router=self.router,
            adapter=FakeAdapter(candidate),
            store=self.store,
            observability=self.observability,
            routing_policy_ref="model-routing-policy-version:test-planner:1",
            credential_slot_refs=("credential-slot:openclaw:test",),
            clock=lambda: NOW,
        )

    def test_valid_candidate_is_accounted_and_formally_completed(self) -> None:
        work = self._work()
        result = self._worker({
            "schema_version": "0.1",
            "action": {"kind": "probe", "coverage_item_ref": "commitments"},
            "rationale": "Inspect the commitments disclosure next.",
        }).run_once(work)
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(result["accounting"]["cost"]["cost_status"], "actual")
        self.assertEqual(self.scheduler.formal_result(work.id)["terminal_state"], "succeeded")
        self.assertIsNotNone(self.store.connection.execute(
            "SELECT 1 FROM model_invocations WHERE invocation_id=?",
            ("invocation:test-planner-worker",),
        ).fetchone())

    def test_invalid_candidate_is_retried_within_scheduler_bound(self) -> None:
        work = self._work()
        result = self._worker({
            "schema_version": "0.1",
            "action": {
                "kind": "probe",
                "coverage_item_ref": "commitments",
                "parameters": {"forbidden": True},
            },
            "rationale": "Attempt to emit executable parameters.",
        }).run_once(work)
        self.assertEqual(result["status"], "retryable")
        self.assertEqual(result["result"]["error"]["code"], "MODEL_OUTPUT_CONTRACT_REJECTED")
        self.assertEqual(self.scheduler.status(work.id)["state"], "ready")


if __name__ == "__main__":
    unittest.main()
