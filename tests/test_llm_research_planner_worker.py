from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from dalton_core.contracts import (
    InvocationGranularity,
    ModelInvocation,
    ResultEnvelope,
    WorkOrder,
)
from dalton_core.llm_research_planner import build_planner_work_order
from dalton_core.budget_pools import (
    POOL_EXHAUSTED_STATUS,
    mission_pool_scope,
)
from dalton_core.llm_research_planner_worker import (
    LLMResearchPlannerModelWorker,
    LLMResearchPlannerWorkerRejected,
    planner_provider_attempt_bound,
)
from dalton_core.model_router import ModelRouter
from dalton_core.observability import ObservabilityStore
from dalton_core.scheduler import Scheduler
from dalton_core.store import DaltonStore, content_hash
from dalton_core.thesis_impact_budget import ThesisImpactBudgetStore


NOW = datetime(2026, 8, 23, 18, 0, tzinfo=timezone.utc)
DAY = NOW.date().isoformat()
BUDGET_POLICY = "thesis-impact-day-budget-policy:c2b:1"


def budget_mission(pools: dict | None = None) -> dict:
    """A mission version the day ledger will admit a planner call against."""

    budget = {"max_daily_paid_calls": 100, "max_daily_cost_usd": 10.0,
              "max_alphaengine_calls_24h": 5}
    if pools is not None:
        budget["pools"] = pools
    return {
        "mission_ref": "coverage-mission:c2b",
        "id": "coverage-mission-version:c2b:1",
        "content_hash": content_hash({"mission": "c2b"}),
        "budget": budget,
    }


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


def alternate_profile() -> dict:
    return {
        **profile(),
        "profile_version_ref": "model-profile-version:test-planner-z:1",
        "id": "profile:test-planner-z",
        "provider": "test-z",
        "model": "planner-z",
        "family": "test-planner-z",
        "credential_slot_ref": "credential-slot:openclaw:test-z",
    }


def retry_policy() -> dict:
    return {
        **policy(),
        "policy_version_ref": "model-routing-policy-version:test-planner-retry:1",
        "id": "model-routing-policy:test-planner-retry",
        "filters": {
            **policy()["filters"],
            "allowed_profile_ids": [
                "profile:test-planner", "profile:test-planner-z",
            ],
        },
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
        self.served: list[str] = []

    def replay(self, work: WorkOrder, route: dict, selected: dict):
        raise AssertionError("fresh worker should not replay")

    def execute(self, work: WorkOrder, route: dict, selected: dict):
        self.served.append(work.id)
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


class ReturnedProviderPlannerAdapter(FakeAdapter):
    """Return formally proved paid failures, then one valid candidate."""

    def __init__(self, candidate: dict, failures: list[str]) -> None:
        super().__init__(candidate)
        self.failures = list(failures)
        self.profiles: list[str] = []

    def execute(self, work: WorkOrder, route: dict, selected: dict):
        invocation, result = super().execute(work, route, selected)
        self.profiles.append(selected["id"])
        suffix = content_hash({"route": route["id"], "n": len(self.profiles)})[:32]
        invocation = replace(
            invocation,
            id="invocation:test-planner-retry-" + suffix,
            parent_ref=route["id"],
            usage=(
                {} if self.failures else invocation.usage
            ),
        )
        if not self.failures:
            return invocation, replace(
                result,
                id="result:test-planner-retry-" + suffix,
                invocation_ref=invocation.id,
            )
        code = self.failures.pop(0)
        return invocation, replace(
            result,
            id="result:test-planner-retry-" + suffix,
            invocation_ref=invocation.id,
            status="failed",
            outputs={},
            error={
                "code": code,
                "message": "provider asked the caller to retry",
                "source": "openclaw-model-broker",
            },
            metadata={
                "route_decision_ref": route["id"],
                "broker_response_hash": content_hash(
                    {"route": route["id"], "code": code}
                ),
                "broker_request_mode": "execute",
                "dispatch_proof": {
                    "authority": "openclaw-model-adapter",
                    "state": "provider_completed_failure",
                    "version": "0.1",
                },
            },
        )


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

    def test_provider_retry_policy_is_part_of_the_work_identity(self) -> None:
        first_policy = {
            "max_same_profile_retries": 1, "retry_backoff_seconds": 2,
        }
        second_policy = {
            "max_same_profile_retries": 2, "retry_backoff_seconds": 2,
        }
        first = build_planner_work_order(context(), provider_retry=first_policy)
        second = build_planner_work_order(context(), provider_retry=second_policy)
        legacy = build_planner_work_order(context())
        self.assertNotEqual(first.id, second.id)
        self.assertNotEqual(first.id, legacy.id)
        self.assertEqual(first.metadata["provider_retry"], first_policy)

    def test_worker_refuses_a_retry_policy_that_differs_from_the_work(self) -> None:
        work = build_planner_work_order(
            context(), provider_retry={
                "max_same_profile_retries": 1, "retry_backoff_seconds": 0,
            },
        )
        self.assertEqual(self.scheduler.enqueue(work)["status"], "fresh")
        worker = LLMResearchPlannerModelWorker(
            scheduler=self.scheduler,
            router=self.router,
            adapter=FakeAdapter({
                "schema_version": "0.1",
                "action": {"kind": "terminal", "reason": "coverage_complete"},
                "rationale": "Done.",
            }),
            store=self.store,
            observability=self.observability,
            routing_policy_ref="model-routing-policy-version:test-planner:1",
            credential_slot_refs=("credential-slot:openclaw:test",),
            provider_retry={
                "max_same_profile_retries": 2, "retry_backoff_seconds": 0,
            },
            clock=lambda: NOW,
        )
        with self.assertRaisesRegex(
            LLMResearchPlannerWorkerRejected, "retry policy differs"
        ):
            worker.run_once(work)

    def test_scheduler_attempt_bound_covers_each_profile_and_its_retries(self) -> None:
        self.assertEqual(
            self.router.register_profile(alternate_profile())["status"], "fresh"
        )
        self.assertEqual(
            self.router.register_policy(retry_policy())["status"], "fresh"
        )
        attempts, policy_hash = planner_provider_attempt_bound(
            self.router,
            "model-routing-policy-version:test-planner-retry:1",
            {"max_same_profile_retries": 4, "retry_backoff_seconds": 2},
        )
        self.assertEqual(attempts, 10)
        self.assertEqual(
            policy_hash,
            self.router.get_policy(
                "model-routing-policy-version:test-planner-retry:1"
            )["content_hash"],
        )



class PlannerWorkerDayLedgerTests(LLMResearchPlannerWorkerTests):
    """C2b: the Tier-1 planner's model calls are admitted like every other.

    Before C2b these calls were the one paid thing the mission day ledger
    never saw, which made the owner's 25% ad-hoc pool unenforceable for
    exactly the work it was sized for.  These tests are about the ledger, so
    they inherit the routing fixture and change only who is holding it.
    """

    ACTION = {
        "schema_version": "0.1",
        "action": {"kind": "probe", "coverage_item_ref": "commitments"},
        "rationale": "Inspect the commitments disclosure next.",
    }

    def setUp(self) -> None:
        super().setUp()
        self.budget_db = Path(self.temp.name) / "thesis-impact-budget.sqlite"
        self.ledger = ThesisImpactBudgetStore(self.budget_db, clock=lambda: NOW)
        self.addCleanup(self.ledger.close)
        self.ledger.register_policy(
            policy_version_id=BUDGET_POLICY, day_cap_micros=100_000_000)

    def _work(self) -> WorkOrder:
        # The driver's own ceiling, not the planner module's $5 default: a
        # round reserves ``planner_max_cost_usd`` and the ad-hoc pool is 25%
        # of the day, so a $5 reservation could never fit in it.
        work = build_planner_work_order(context(), max_cost_usd=0.5)
        self.assertEqual(self.scheduler.enqueue(work)["status"], "fresh")
        return work

    def _binding(self, mission: dict) -> dict:
        return {
            "mission_ref": mission["mission_ref"],
            "mission_version_ref": mission["id"],
            "mission_version_hash": mission["content_hash"],
            "max_daily_paid_calls": mission["budget"]["max_daily_paid_calls"],
            "max_daily_cost_micros": int(
                mission["budget"]["max_daily_cost_usd"] * 1_000_000),
            **mission_pool_scope(
                mission, pool="adhoc", lane="llm_planner_execute"),
        }

    def _budgeted(self, adapter: FakeAdapter, mission: dict, *,
                  provider_retry=None, transport_retry=None,
                  ) -> LLMResearchPlannerModelWorker:
        return LLMResearchPlannerModelWorker(
            scheduler=self.scheduler,
            router=self.router,
            adapter=adapter,
            store=self.store,
            observability=self.observability,
            routing_policy_ref="model-routing-policy-version:test-planner:1",
            credential_slot_refs=("credential-slot:openclaw:test",),
            budget=self.ledger,
            budget_policy_ref=BUDGET_POLICY,
            mission_binding=self._binding(mission),
            provider_retry=provider_retry,
            transport_retry=transport_retry,
            clock=lambda: NOW,
        )

    def _rows(self, table: str) -> list:
        return self.ledger.connection.execute(
            f"SELECT * FROM {table}").fetchall()

    def test_a_planner_call_reserves_and_settles_in_its_own_pool(self) -> None:
        work = self._work()
        adapter = FakeAdapter(self.ACTION)
        result = self._budgeted(adapter, budget_mission()).run_once(work)

        self.assertEqual(result["status"], "succeeded")
        admissions = self._rows("thesis_impact_day_admissions")
        settlements = self._rows("thesis_impact_day_settlements")
        self.assertEqual(len(admissions), 1)
        self.assertEqual(len(settlements), 1)
        # The loop's pool, and the lane that names it, travel into the ledger:
        # this is what makes the ad-hoc share readable after the fact.
        self.assertEqual(admissions[0]["pool"], "adhoc")
        self.assertEqual(admissions[0]["pool_lane"], "llm_planner_execute")
        self.assertEqual(settlements[0]["pool"], "adhoc")
        # Settled with what the link that served actually cost -- the broker
        # reported $0.001 -- not with the WorkOrder's $5 reservation.
        self.assertEqual(settlements[0]["actual_micros"], 1_000)
        self.assertEqual(admissions[0]["reserved_micros"], 500_000)
        self.assertEqual(result["budget"]["status"], "settled")
        self.assertEqual(result["budget"]["pool"], "adhoc")
        self.assertEqual(result["budget"]["settled_micros"], 1_000)
        self.assertEqual(result["budget"]["cost_status"], "actual")

    def test_paid_retry_uses_new_attempts_then_fallback_and_keeps_unknown_cost(self) -> None:
        retry = {"max_same_profile_retries": 1, "retry_backoff_seconds": 0}
        self.assertEqual(
            self.router.register_profile(alternate_profile())["status"], "fresh"
        )
        self.assertEqual(
            self.router.register_policy(retry_policy())["status"], "fresh"
        )
        work = build_planner_work_order(
            context(), max_cost_usd=0.5, provider_retry=retry
        )
        self.assertEqual(self.scheduler.enqueue(work)["status"], "fresh")
        adapter = ReturnedProviderPlannerAdapter(
            self.ACTION, ["RATE_LIMITED", "PROVIDER_INTERNAL_ERROR"]
        )
        worker = LLMResearchPlannerModelWorker(
            scheduler=self.scheduler,
            router=self.router,
            adapter=adapter,
            store=self.store,
            observability=self.observability,
            routing_policy_ref=(
                "model-routing-policy-version:test-planner-retry:1"
            ),
            credential_slot_refs=(
                "credential-slot:openclaw:test",
                "credential-slot:openclaw:test-z",
            ),
            budget=self.ledger,
            budget_policy_ref=BUDGET_POLICY,
            mission_binding=self._binding(budget_mission()),
            provider_retry=retry,
            clock=lambda: NOW,
        )
        runs = [worker.run_once(work), worker.run_once(work), worker.run_once(work)]
        self.assertEqual([item["status"] for item in runs], [
            "retryable", "retryable", "succeeded",
        ])
        self.assertEqual(adapter.profiles[0], adapter.profiles[1])
        self.assertNotEqual(adapter.profiles[1], adapter.profiles[2])
        self.assertEqual(work.metadata["provider_retry"], retry)
        decisions = self.router.list_decisions(work_order_id=work.id)
        self.assertEqual(
            [item["attempt_number"] for item in decisions], [1, 2, 3]
        )
        self.assertEqual(
            len({item["invocation"]["id"] for item in runs}), 3
        )
        rows = self.ledger.connection.execute(
            "SELECT a.attempt_number,a.reserved_micros,s.actual_micros "
            "FROM thesis_impact_day_admissions a "
            "JOIN thesis_impact_day_settlements s "
            "ON s.admission_id=a.admission_id "
            "WHERE a.work_order_ref=? ORDER BY a.attempt_number",
            (work.id,),
        ).fetchall()
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[0][2], rows[0][1])
        self.assertEqual(rows[1][2], rows[1][1])
        self.assertEqual(rows[2][2], 1_000)

    def test_nonretryable_provider_failure_with_actual_cost_is_charged(self) -> None:
        class NonretryableFailure(FakeAdapter):
            def execute(inner, work, route, selected):
                invocation, result = super().execute(work, route, selected)
                return invocation, replace(
                    result,
                    status="failed",
                    outputs={},
                    error={
                        "code": "INVALID_HOST_RESULT",
                        "message": "host rejected the result",
                        "source": "openclaw-model-broker",
                    },
                )

        work = self._work()
        result = self._budgeted(
            NonretryableFailure(self.ACTION), budget_mission()
        ).run_once(work)
        self.assertEqual(result["status"], "failed")
        settlement = self._rows("thesis_impact_day_settlements")[0]
        self.assertEqual(settlement["actual_micros"], 1_000)

    def test_unproved_busy_without_metering_retains_full_reservation(self) -> None:
        class UnprovedBusy(FakeAdapter):
            def execute(inner, work, route, selected):
                invocation, result = super().execute(work, route, selected)
                return replace(invocation, usage={}), replace(
                    result,
                    status="failed",
                    outputs={},
                    error={"code": "BUSY", "message": "provider said busy"},
                )

        work = self._work()
        result = self._budgeted(
            UnprovedBusy(self.ACTION), budget_mission()
        ).run_once(work)
        self.assertEqual(result["status"], "failed")
        admission = self._rows("thesis_impact_day_admissions")[0]
        settlement = self._rows("thesis_impact_day_settlements")[0]
        self.assertEqual(
            settlement["actual_micros"], admission["reserved_micros"]
        )

    def test_proved_not_sent_transport_retry_stays_in_one_attempt(self) -> None:
        from dalton_core.openclaw_model_adapter import BrokerDefinitelyNotSent

        class NotSentOnce(FakeAdapter):
            def __init__(inner, candidate):
                super().__init__(candidate)
                inner.calls = 0

            def execute(inner, work, route, selected):
                inner.calls += 1
                if inner.calls == 1:
                    raise BrokerDefinitelyNotSent("connect failed before send")
                return super().execute(work, route, selected)

        transport = {
            "max_definitely_not_sent_retries": 1,
            "queue_wait_seconds": 0,
            "retry_backoff_seconds": 0,
        }
        work = build_planner_work_order(
            context(), max_cost_usd=0.5, transport_retry=transport
        )
        self.assertEqual(self.scheduler.enqueue(work)["status"], "fresh")
        adapter = NotSentOnce(self.ACTION)
        result = self._budgeted(
            adapter, budget_mission(), transport_retry=transport
        ).run_once(work)
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(adapter.calls, 2)
        self.assertEqual(
            {item["attempt_number"] for item in self.router.list_decisions(
                work_order_id=work.id
            )},
            {1},
        )
        self.assertEqual(len(self._rows("thesis_impact_day_admissions")), 1)

    def test_post_send_unknown_is_persisted_charged_and_not_retried(self) -> None:
        from dalton_core.openclaw_model_adapter import BrokerConnectionError

        class PostSendUnknown(FakeAdapter):
            def execute(inner, work, route, selected):
                invocation, succeeded = super().execute(work, route, selected)
                invocation = replace(invocation, usage={})
                failed = replace(
                    succeeded,
                    status="failed",
                    outputs={},
                    error={
                        "code": "POST_SEND_RESULT_UNKNOWN",
                        "message": "broker result unavailable after dispatch",
                        "source": "openclaw-model-adapter",
                    },
                    metadata={
                        "route_decision_ref": route["id"],
                        "profile_version_ref": selected["profile_version_ref"],
                        "broker_request_mode": "execute",
                        "dispatch_proof": {
                            "authority": "openclaw-model-adapter",
                            "state": "post_send_result_unknown",
                            "version": "0.1",
                        },
                    },
                )
                error = BrokerConnectionError("connection dropped after send")
                error.post_send_unknown_evidence = SimpleNamespace(
                    invocation=invocation, result=failed
                )
                raise error

        work = self._work()
        result = self._budgeted(
            PostSendUnknown(self.ACTION), budget_mission()
        ).run_once(work)
        self.assertEqual(result["status"], "failed")
        self.assertTrue(result["post_send_result_unknown"])
        self.assertEqual(result["result"]["error"]["code"],
                         "POST_SEND_RESULT_UNKNOWN")
        self.assertIsNotNone(self.store.connection.execute(
            "SELECT 1 FROM model_invocations WHERE invocation_id=?",
            (result["invocation"]["id"],),
        ).fetchone())
        admission = self._rows("thesis_impact_day_admissions")[0]
        settlement = self._rows("thesis_impact_day_settlements")[0]
        self.assertEqual(
            settlement["actual_micros"], admission["reserved_micros"]
        )
        self.assertEqual(self.scheduler.status(work.id)["state"], "failed")

    def test_a_spent_pool_is_returned_before_the_work_order_is_leased(self) -> None:
        work = self._work()
        adapter = FakeAdapter(self.ACTION)
        spent = budget_mission(
            {"coverage": 10.0, "event_response": 0, "adhoc": 0, "maintenance": 0})
        result = self._budgeted(adapter, spent).run_once(work)

        self.assertEqual(result["status"], "rejected")
        self.assertEqual(result["reason"], "pool_exhausted")
        self.assertEqual(result["lane_status"], POOL_EXHAUSTED_STATUS)
        self.assertEqual(result["pool"], "adhoc")
        self.assertEqual(result["cap"], 0)
        self.assertEqual(result["gate"], "pre_lease")
        # Nothing was called and nothing was charged.
        self.assertEqual(adapter.served, [])
        self.assertEqual(self._rows("thesis_impact_day_settlements"), [])
        # And -- the point of gating before the claim -- the loop keeps the
        # attempt.  A rejection taken after the lease would burn one of the
        # WorkOrder's bounded attempts every tick for the rest of the day and
        # leave the loop permanently exhausted by a budget decision.
        self.assertEqual(self.scheduler.status(work.id)["state"], "ready")
        self.assertIsNone(self.scheduler.formal_result(work.id))
        history = self.scheduler.attempt_history(work.id)
        self.assertEqual(len(history), 1)
        self.assertEqual(history[-1]["reason"], "enqueued")
        self.assertEqual(history[-1]["attempt_number"], 1)

    def test_the_pool_refills_and_the_same_loop_runs_tomorrow(self) -> None:
        # The rejection is a decision about today, so it must not be durable:
        # the same worker, the same WorkOrder, a mission whose pool has room.
        work = self._work()
        spent = budget_mission(
            {"coverage": 10.0, "event_response": 0, "adhoc": 0, "maintenance": 0})
        self.assertEqual(
            self._budgeted(FakeAdapter(self.ACTION), spent).run_once(work)["status"],
            "rejected")
        adapter = FakeAdapter(self.ACTION)
        again = self._budgeted(adapter, budget_mission()).run_once(work)
        self.assertEqual(again["status"], "succeeded")
        self.assertEqual(adapter.served, [work.id])

    def test_a_broker_that_never_served_hands_the_reservation_back(self) -> None:
        from dalton_core.openclaw_model_adapter import BrokerDefinitelyNotSent

        class DeadBroker(FakeAdapter):
            def execute(self, work, route, selected):
                raise BrokerDefinitelyNotSent("the broker socket is not there")

        work = self._work()
        result = self._budgeted(
            DeadBroker(self.ACTION), budget_mission()).run_once(work)
        self.assertEqual(result["status"], "retryable")
        settlements = self._rows("thesis_impact_day_settlements")
        self.assertEqual(len(settlements), 1)
        self.assertEqual(settlements[0]["actual_micros"], 0)

    def test_unproved_adapter_failure_retains_budget_and_is_terminal(self) -> None:
        # The fix for the failure that mattered most: reporting the budget
        # only on success made a budgeted install with a dead broker report
        # the one word -- "unbudgeted" -- that means the call never reached
        # the day ledger. That is exactly the moment somebody asks.
        from dalton_core.openclaw_model_adapter import BrokerConnectionError

        class DeadBroker(FakeAdapter):
            def execute(self, work, route, selected):
                raise BrokerConnectionError("the broker socket is not there")

        work = self._work()
        result = self._budgeted(
            DeadBroker(self.ACTION), budget_mission()).run_once(work)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["budget"]["status"], "settled")
        self.assertEqual(result["budget"]["pool"], "adhoc")
        self.assertEqual(result["budget"]["settled_micros"], 500_000)
        self.assertEqual(result["budget"]["cost_status"], "reserved")
        self.assertNotEqual(result["budget"]["status"], "unbudgeted")

    def test_a_failure_this_worker_does_not_model_still_frees_the_pool(self) -> None:
        # An open reservation counts against the pool at its full reserved
        # amount until something settles it. Nothing else would.
        class Exploding(FakeAdapter):
            def execute(self, work, route, selected):
                raise ZeroDivisionError("something nobody modelled")

        work = self._work()
        with self.assertRaises(ZeroDivisionError):
            self._budgeted(
                Exploding(self.ACTION), budget_mission()).run_once(work)
        settlements = self._rows("thesis_impact_day_settlements")
        self.assertEqual(len(settlements), 1)
        self.assertEqual(settlements[0]["actual_micros"], 0)

    def test_a_pre_lease_refusal_is_visible_to_the_cockpit(self) -> None:
        # admit() records its own rejections, but this gate refuses *before*
        # admitting, so without recording it here the most common refusal in
        # the system would be the one nobody could see.
        from dalton_core.budget_pools import pool_status

        work = self._work()
        spent = budget_mission(
            {"coverage": 10.0, "event_response": 0, "adhoc": 0, "maintenance": 0})
        self._budgeted(FakeAdapter(self.ACTION), spent).run_once(work)

        status = pool_status(
            self.ledger.connection, day=DAY,
            mission_ref="coverage-mission:c2b", now=NOW)
        self.assertTrue(status["pools"]["adhoc"]["exhausted"])
        self.assertEqual(
            [item["lane"] for item in status["exhausted_lanes"]],
            ["llm_planner_execute"])

    def test_the_same_refusal_twice_is_one_row(self) -> None:
        work = self._work()
        spent = budget_mission(
            {"coverage": 10.0, "event_response": 0, "adhoc": 0, "maintenance": 0})
        for _ in range(3):
            self._budgeted(FakeAdapter(self.ACTION), spent).run_once(work)
        self.assertEqual(
            len(self._rows("model_budget_pool_rejections")), 1)

    def test_an_unbudgeted_worker_says_so_rather_than_saying_nothing(self) -> None:
        # The pre-C2b behaviour, kept: a planner configuration without a
        # budget_db still runs.  It reports "unbudgeted" so that "is the
        # planner on the ledger yet?" is answerable from one op result rather
        # than from the installer's history.
        work = self._work()
        result = self._worker(self.ACTION).run_once(work)
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(result["budget"], {"status": "unbudgeted"})
        self.assertEqual(self._rows("thesis_impact_day_admissions"), [])

    def test_half_a_ledger_is_refused_at_construction(self) -> None:
        # A worker holding the ledger but no binding would look budgeted and
        # admit nothing, which is the hole C2b closes wearing a disguise.
        with self.assertRaisesRegex(ValueError, "together"):
            LLMResearchPlannerModelWorker(
                scheduler=self.scheduler, router=self.router,
                adapter=FakeAdapter(self.ACTION), store=self.store,
                observability=self.observability,
                routing_policy_ref="model-routing-policy-version:test-planner:1",
                credential_slot_refs=("credential-slot:openclaw:test",),
                budget=self.ledger, clock=lambda: NOW,
            )

if __name__ == "__main__":
    unittest.main()
