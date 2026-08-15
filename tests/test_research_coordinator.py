from __future__ import annotations

import copy
import inspect
import json
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from dalton_core.connector_runner import RunnerValidationError
from dalton_core.research_context import (
    ResearchContextConflict,
    ResearchContextError,
    build_claim_index,
    build_compiled_connector_plan,
    build_context_pack,
    build_fixture_runner_request,
    build_reference_fixture_plan,
    validate_claim_index,
    validate_compiled_connector_plan,
    validate_context_pack,
    validate_runner_request_plan_binding,
)
from dalton_core.research_coordinator import (
    FixtureResearchCoordinator,
    InjectedCoordinatorCrash,
    RecordedShadowFixturePort,
    ResearchCoordinatorConflict,
    ResearchCoordinatorError,
    ResearchCoordinatorStore,
    validate_connector_completion_receipt,
    validate_research_checkpoint,
    validate_research_run_state,
)
from dalton_core.store import content_hash
from tests.test_connector import CONTRACTS, assert_wire_schema, validate_json_schema


WHEN = datetime(2026, 8, 15, 6, 0, tzinfo=timezone.utc)
WIRE_WHEN = WHEN.isoformat(timespec="microseconds")


class FrozenClock:
    def __call__(self) -> datetime:
        return WHEN


class CrashOnce:
    def __init__(self, stage: str) -> None:
        self.stage = stage
        self.triggered = False

    def __call__(self, stage: str, payload: dict) -> None:
        if stage == self.stage and not self.triggered:
            self.triggered = True
            raise InjectedCoordinatorCrash(stage)


class ForgedReceiptPort:
    def __init__(self) -> None:
        self.inner = RecordedShadowFixturePort(clock=FrozenClock())

    def execute(self, step, runner_request, *, idempotency_key):
        receipt = dict(
            self.inner.execute(
                step, runner_request, idempotency_key=idempotency_key
            )
        )
        receipt["runner_request_ref"] = "connector-runner-request:forged"
        receipt.pop("content_hash")
        receipt["content_hash"] = content_hash(receipt)
        return receipt


class ResearchCoordinatorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.task_ref = "work-order:fixture-research:1"
        self.task_hash = content_hash({"work_order_ref": self.task_ref})
        self.plan = build_reference_fixture_plan(
            task_ref=self.task_ref,
            task_hash=self.task_hash,
            created_at=WIRE_WHEN,
        )
        self.claim_index = build_claim_index(
            [],
            ledger_snapshot_ref="ledger-snapshot:fixture:1",
            ledger_snapshot_hash=content_hash({"ledger_snapshot": "fixture:1"}),
            created_at=WIRE_WHEN,
        )
        self.context_pack = build_context_pack(
            [
                {
                    "kind": "mandate",
                    "ref": "research-mandate:fixture:1",
                    "hash": content_hash({"mandate": "official reference sources"}),
                    "priority": 100,
                    "content": "Read official CNINFO and SEC filings and the recorded AlphaEngine shadow.",
                }
            ],
            task_ref=self.task_ref,
            task_hash=self.task_hash,
            compiled_plan_ref=self.plan["id"],
            compiled_plan_hash=self.plan["content_hash"],
            claim_index_ref=self.claim_index["id"],
            claim_index_hash=self.claim_index["content_hash"],
            created_at=WIRE_WHEN,
            max_tokens=100,
            max_bytes=2_000,
        )
        self.runner_requests = {
            step["id"]: [
                build_fixture_runner_request(
                    self.plan,
                    step,
                    attempt_number=attempt,
                    created_at=WIRE_WHEN,
                )
                for attempt in range(1, step["max_attempts"] + 1)
            ]
            for step in self.plan["steps"]
        }
        self.run_ref = "research-run:fixture:1"
        self.attempt_ref = "research-attempt:fixture:1"
        self.attempt_hash = content_hash({"attempt_ref": self.attempt_ref})

    def coordinator(self, store, port, *, fault_hook=None):
        return FixtureResearchCoordinator(
            store=store,
            connector_port=port,
            clock=FrozenClock(),
            fault_hook=fault_hook,
        )

    def invoke(self, coordinator):
        return coordinator.run(
            plan=self.plan,
            context_pack=self.context_pack,
            claim_index=self.claim_index,
            runner_requests=self.runner_requests,
            run_ref=self.run_ref,
            attempt_ref=self.attempt_ref,
            attempt_hash=self.attempt_hash,
        )

    def test_contracts_are_closed_hashed_and_match_wire_schemas(self) -> None:
        store = ResearchCoordinatorStore(":memory:")
        self.addCleanup(store.close)
        port = RecordedShadowFixturePort(clock=FrozenClock())
        result = self.invoke(self.coordinator(store, port))
        checkpoints = store.list_checkpoints(self.run_ref)
        state = store.latest_run_state(self.run_ref)
        receipt = port.execute(
            self.plan["steps"][0],
            self.runner_requests[self.plan["steps"][0]["id"]][0],
            idempotency_key="schema-receipt",
        )

        assert_wire_schema(
            self, "compiled-connector-plan.schema.json", self.plan
        )
        assert_wire_schema(self, "claim-index.schema.json", self.claim_index)
        assert_wire_schema(self, "context-pack.schema.json", self.context_pack)
        assert_wire_schema(
            self, "connector-completion-receipt.schema.json", receipt
        )
        assert_wire_schema(
            self, "research-checkpoint.schema.json", checkpoints[0]
        )
        assert_wire_schema(self, "research-run-state.schema.json", state)
        runner_schema = json.loads(
            (CONTRACTS / "connector-runner-request.schema.json").read_text(
                encoding="utf-8"
            )
        )
        for requests in self.runner_requests.values():
            for request in requests:
                validate_json_schema(request, runner_schema, runner_schema)
        self.assertEqual(result["status"], "completed")

        unknown = dict(self.plan, unexpected=True)
        with self.assertRaises(ResearchContextError):
            validate_compiled_connector_plan(unknown)
        tampered = copy.deepcopy(self.context_pack)
        tampered["budget"]["max_tokens"] += 1
        with self.assertRaises(ResearchContextConflict):
            validate_context_pack(tampered)

    def test_reference_plan_is_emitted_once_and_binds_every_runner_request(self) -> None:
        rebuilt = build_reference_fixture_plan(
            task_ref=self.task_ref,
            task_hash=self.task_hash,
            created_at=WIRE_WHEN,
        )
        self.assertEqual(rebuilt, self.plan)
        self.assertEqual(
            [step["source_ref"] for step in self.plan["steps"]],
            ["source:cninfo", "source:sec-edgar", "source:alphaengine"],
        )
        for step in self.plan["steps"]:
            for request in self.runner_requests[step["id"]]:
                self.assertEqual(
                    validate_runner_request_plan_binding(
                        request, self.plan, step
                    )["compiled_step_hash"],
                    step["content_hash"],
                )
        forged = copy.deepcopy(self.runner_requests[self.plan["steps"][0]["id"]][0])
        forged["compiled_step_hash"] = "0" * 64
        forged.pop("content_hash")
        forged["content_hash"] = content_hash(forged)
        with self.assertRaises(ResearchContextConflict):
            validate_runner_request_plan_binding(
                forged, self.plan, self.plan["steps"][0]
            )
        partial = copy.deepcopy(
            self.runner_requests[self.plan["steps"][0]["id"]][0]
        )
        del partial["compiled_step_hash"]
        partial.pop("content_hash")
        partial["content_hash"] = content_hash(partial)
        with self.assertRaises(RunnerValidationError):
            validate_runner_request_plan_binding(
                partial, self.plan, self.plan["steps"][0]
            )
        runner_schema = json.loads(
            (CONTRACTS / "connector-runner-request.schema.json").read_text(
                encoding="utf-8"
            )
        )
        with self.assertRaises(AssertionError):
            validate_json_schema(partial, runner_schema, runner_schema)

    def test_context_pack_budget_and_duplicate_policy_are_deterministic(self) -> None:
        shared_hash = content_hash({"shared": "input"})
        pack = build_context_pack(
            [
                {
                    "kind": "claim",
                    "ref": "claim-version:shared",
                    "hash": shared_hash,
                    "priority": 20,
                    "content": "alpha beta",
                },
                {
                    "kind": "claim",
                    "ref": "claim-version:shared",
                    "hash": shared_hash,
                    "priority": 10,
                    "content": "alpha beta",
                },
                {
                    "kind": "source",
                    "ref": "source:too-large",
                    "hash": content_hash({"source": "too-large"}),
                    "priority": 1,
                    "content": "one two three four",
                },
            ],
            task_ref=self.task_ref,
            task_hash=self.task_hash,
            compiled_plan_ref=self.plan["id"],
            compiled_plan_hash=self.plan["content_hash"],
            claim_index_ref=self.claim_index["id"],
            claim_index_hash=self.claim_index["content_hash"],
            created_at=WIRE_WHEN,
            max_tokens=3,
            max_bytes=100,
        )
        self.assertEqual(
            [item["selection_reason"] for item in pack["inputs"]],
            ["included", "duplicate", "budget_tokens"],
        )
        self.assertEqual(pack["totals"]["selected_tokens"], 2)
        conflict_specs = [
            {
                "kind": "claim",
                "ref": "claim-version:shared",
                "hash": "1" * 64,
                "priority": 2,
                "content": "alpha",
            },
            {
                "kind": "claim",
                "ref": "claim-version:shared",
                "hash": "2" * 64,
                "priority": 1,
                "content": "alpha",
            },
        ]
        with self.assertRaises(ResearchContextConflict):
            build_context_pack(
                conflict_specs,
                task_ref=self.task_ref,
                task_hash=self.task_hash,
                compiled_plan_ref=self.plan["id"],
                compiled_plan_hash=self.plan["content_hash"],
                claim_index_ref=self.claim_index["id"],
                claim_index_hash=self.claim_index["content_hash"],
                created_at=WIRE_WHEN,
                max_tokens=100,
                max_bytes=100,
            )
        dishonest = copy.deepcopy(pack)
        first = dishonest["inputs"][0]
        first["selected"] = False
        first["selection_reason"] = "budget_tokens"
        first["included_tokens"] = 0
        first["included_bytes"] = 0
        first.pop("content_hash")
        first["content_hash"] = content_hash(first)
        dishonest["totals"] = {
            "selected_tokens": 0,
            "selected_bytes": 0,
            "omitted_count": len(dishonest["inputs"]),
        }
        dishonest.pop("content_hash")
        dishonest["content_hash"] = content_hash(dishonest)
        with self.assertRaises(ResearchContextConflict):
            validate_context_pack(dishonest)

        step = self.plan["steps"][0]
        secret_spec = {
            key: step[key]
            for key in (
                "source_ref", "source_hash", "connector_profile_ref",
                "connector_profile_hash", "operation", "input_schema_ref",
                "input_schema_hash", "output_schema_ref", "output_schema_hash",
                "completeness_required", "depends_on", "fallback_step_refs",
                "max_attempts",
            )
        }
        secret_spec["parameters"] = {"access_token": "must-not-enter"}
        with self.assertRaises(ResearchContextError):
            build_compiled_connector_plan(
                task_ref=self.task_ref,
                task_hash=self.task_hash,
                planner_ref="planner:fixture",
                planner_hash="1" * 64,
                routing_policy_ref="routing-policy:fixture",
                routing_policy_hash="2" * 64,
                step_specs=[secret_spec],
                created_at=WIRE_WHEN,
            )

    def test_claim_index_derives_search_projection_without_ledger_writes(self) -> None:
        claim = {
            "schema_version": "0.1",
            "id": "claim-version:revenue:1",
            "created_at": WIRE_WHEN,
            "claim_ref": "claim:revenue",
            "version": 1,
            "subject_ref": "company:fixture",
            "metric_or_aspect": "revenue",
            "period": "2026Q2",
            "basis": "reported",
            "normalized_statement": "Revenue was 42 USDm",
            "claim_kind": "quantitative",
            "value": 42,
            "unit": "USDm",
            "producer_invocation_refs": ["invocation:fixture"],
            "actor_ref": "actor:fixture",
            "prior_version_ref": None,
        }
        claim["content_hash"] = content_hash(claim)
        relation = {
            "schema_version": "0.1",
            "id": "evidence-relation:revenue:1",
            "created_at": WIRE_WHEN,
            "evidence_ref": "evidence:sec:fixture",
            "evidence_version_ref": "evidence-version:sec:fixture:1",
            "claim_ref": claim["claim_ref"],
            "claim_version_ref": claim["id"],
            "relation": "supports",
            "source_lineage": ["source:sec-edgar"],
            "independence_group": "sec:fixture",
            "actor_ref": "actor:fixture",
        }
        relation["content_hash"] = content_hash(relation)
        index = build_claim_index(
            [
                {
                    "claim": claim,
                    "evidence_relations": [relation],
                    "status": "corroborated",
                }
            ],
            ledger_snapshot_ref="ledger-snapshot:fixture:2",
            ledger_snapshot_hash=content_hash({"snapshot": 2}),
            created_at=WIRE_WHEN,
        )
        entry = validate_claim_index(index)["entries"][0]
        self.assertEqual(entry["source_types"], ["source:sec-edgar"])
        self.assertIn("revenue", entry["search_terms"])
        self.assertEqual(entry["claim_version_hash"], claim["content_hash"])

    def test_successful_three_source_run_is_resumable_and_idempotent(self) -> None:
        store = ResearchCoordinatorStore(":memory:")
        self.addCleanup(store.close)
        port = RecordedShadowFixturePort(clock=FrozenClock())
        coordinator = self.coordinator(store, port)
        first = self.invoke(coordinator)
        second = self.invoke(coordinator)
        self.assertEqual(first, second)
        self.assertEqual(first["status"], "completed")
        self.assertEqual(first["checkpoint_count"], 3)
        self.assertEqual(port.execute_calls, 3)
        self.assertEqual(port.transport_calls, 3)
        checkpoints = store.list_checkpoints(self.run_ref)
        self.assertTrue(all(item["outcome"] == "succeeded" for item in checkpoints))
        self.assertTrue(all(item["source_envelopes"] for item in checkpoints))
        self.assertTrue(all(item["artifacts"] for item in checkpoints))

    def test_crash_after_execute_reuses_port_idempotency_on_resume(self) -> None:
        store = ResearchCoordinatorStore(":memory:")
        self.addCleanup(store.close)
        port = RecordedShadowFixturePort(clock=FrozenClock())
        crash = CrashOnce("after_execute")
        with self.assertRaises(InjectedCoordinatorCrash):
            self.invoke(self.coordinator(store, port, fault_hook=crash))
        self.assertEqual(store.list_checkpoints(self.run_ref), [])
        result = self.invoke(self.coordinator(store, port))
        self.assertEqual(result["status"], "completed")
        self.assertEqual(port.execute_calls, 4)
        self.assertEqual(port.transport_calls, 3)

    def test_crash_after_checkpoint_does_not_repeat_connector_call(self) -> None:
        store = ResearchCoordinatorStore(":memory:")
        self.addCleanup(store.close)
        port = RecordedShadowFixturePort(clock=FrozenClock())
        crash = CrashOnce("after_checkpoint")
        with self.assertRaises(InjectedCoordinatorCrash):
            self.invoke(self.coordinator(store, port, fault_hook=crash))
        self.assertEqual(len(store.list_checkpoints(self.run_ref)), 1)
        result = self.invoke(self.coordinator(store, port))
        self.assertEqual(result["status"], "completed")
        self.assertEqual(port.execute_calls, 3)
        self.assertEqual(port.transport_calls, 3)

    def test_crash_after_state_does_not_repeat_connector_call(self) -> None:
        store = ResearchCoordinatorStore(":memory:")
        self.addCleanup(store.close)
        port = RecordedShadowFixturePort(clock=FrozenClock())
        crash = CrashOnce("after_state")
        with self.assertRaises(InjectedCoordinatorCrash):
            self.invoke(self.coordinator(store, port, fault_hook=crash))
        self.assertEqual(len(store.list_checkpoints(self.run_ref)), 1)
        result = self.invoke(self.coordinator(store, port))
        self.assertEqual(result["status"], "completed")
        self.assertEqual(port.execute_calls, 3)
        self.assertEqual(port.transport_calls, 3)

    def test_retry_is_bounded_and_never_busy_waits(self) -> None:
        first_step = self.plan["steps"][0]
        request_ids = [
            item["id"] for item in self.runner_requests[first_step["id"]]
        ]
        store = ResearchCoordinatorStore(":memory:")
        self.addCleanup(store.close)
        port = RecordedShadowFixturePort(
            scenario_by_request={request_ids[0]: "rate_limited"},
            clock=FrozenClock(),
        )
        coordinator = self.coordinator(store, port)
        first = self.invoke(coordinator)
        self.assertEqual(first["status"], "retryable")
        self.assertEqual(first["retry_after_ms"], 1000)
        self.assertEqual(port.transport_calls, 1)
        second = self.invoke(coordinator)
        self.assertEqual(second["status"], "completed")
        self.assertEqual(port.transport_calls, 4)
        self.assertEqual(
            [item["outcome"] for item in store.list_checkpoints(self.run_ref)],
            ["retryable", "succeeded", "succeeded", "succeeded"],
        )

        failed_store = ResearchCoordinatorStore(":memory:")
        self.addCleanup(failed_store.close)
        failed_port = RecordedShadowFixturePort(
            scenario_by_request={request_id: "rate_limited" for request_id in request_ids},
            clock=FrozenClock(),
        )
        failed_coordinator = self.coordinator(failed_store, failed_port)
        self.assertEqual(self.invoke(failed_coordinator)["status"], "retryable")
        exhausted = self.invoke(failed_coordinator)
        self.assertEqual(exhausted["status"], "failed")
        self.assertEqual(failed_port.transport_calls, 2)
        self.assertEqual(self.invoke(failed_coordinator), exhausted)
        self.assertEqual(failed_port.transport_calls, 2)

    def test_non_success_cannot_fabricate_sources_and_forged_receipt_is_rejected(self) -> None:
        first_step = self.plan["steps"][0]
        request = self.runner_requests[first_step["id"]][0]
        port = RecordedShadowFixturePort(
            scenario_by_request={request["id"]: "rate_limited"},
            clock=FrozenClock(),
        )
        receipt = port.execute(first_step, request, idempotency_key="failure")
        self.assertEqual(receipt["source_envelopes"], [])
        self.assertEqual(receipt["artifacts"], [])
        forged_output = dict(receipt)
        forged_output["source_envelopes"] = [{"ref": "fake", "hash": "f" * 64}]
        forged_output.pop("content_hash")
        forged_output["content_hash"] = content_hash(forged_output)
        with self.assertRaises(ResearchCoordinatorError):
            validate_connector_completion_receipt(forged_output)

        store = ResearchCoordinatorStore(":memory:")
        self.addCleanup(store.close)
        with self.assertRaises(ResearchCoordinatorConflict):
            self.invoke(self.coordinator(store, ForgedReceiptPort()))
        self.assertEqual(store.list_checkpoints(self.run_ref), [])

        failed_store = ResearchCoordinatorStore(":memory:")
        self.addCleanup(failed_store.close)
        failed_port = RecordedShadowFixturePort(
            scenario_by_request={request["id"]: "malformed"},
            clock=FrozenClock(),
        )
        failed = self.invoke(self.coordinator(failed_store, failed_port))
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(failed_port.transport_calls, 1)
        failed_checkpoint = failed_store.list_checkpoints(self.run_ref)[0]
        self.assertEqual(failed_checkpoint["source_envelopes"], [])
        self.assertEqual(failed_checkpoint["artifacts"], [])

    def test_scratch_store_is_private_immutable_and_separate_from_authorities(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "coordinator.sqlite"
            store = ResearchCoordinatorStore(path)
            self.addCleanup(store.close)
            self.invoke(
                self.coordinator(
                    store, RecordedShadowFixturePort(clock=FrozenClock())
                )
            )
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
            with self.assertRaises(sqlite3.DatabaseError):
                store.connection.execute(
                    "UPDATE compiled_connector_plans SET plan_json='{}'"
                )
            with self.assertRaises(sqlite3.DatabaseError):
                store.connection.execute("DELETE FROM research_checkpoints")
            integrity = store.connection.execute("PRAGMA integrity_check").fetchone()[0]
            self.assertEqual(integrity, "ok")

        source = inspect.getsource(FixtureResearchCoordinator)
        for forbidden in (
            "register_claim",
            "register_evidence",
            "relate_evidence",
            "stage_change",
            "commit(",
            "credential",
            "transport",
            "network",
        ):
            self.assertNotIn(forbidden, source)

    def test_run_state_and_checkpoint_reject_broken_chains(self) -> None:
        store = ResearchCoordinatorStore(":memory:")
        self.addCleanup(store.close)
        self.invoke(
            self.coordinator(
                store, RecordedShadowFixturePort(clock=FrozenClock())
            )
        )
        checkpoint = copy.deepcopy(store.list_checkpoints(self.run_ref)[1])
        checkpoint["prior_checkpoint_hash"] = "0" * 64
        checkpoint.pop("content_hash")
        checkpoint["content_hash"] = content_hash(checkpoint)
        self.assertEqual(
            validate_research_checkpoint(checkpoint)["prior_checkpoint_hash"],
            "0" * 64,
        )
        with self.assertRaises(ResearchCoordinatorConflict):
            store.append_checkpoint(checkpoint)

        checkpoints = store.list_checkpoints(self.run_ref)
        broken_chain = copy.deepcopy(checkpoints)
        broken_chain[1]["prior_checkpoint_hash"] = "0" * 64
        broken_chain[1].pop("content_hash")
        broken_chain[1]["content_hash"] = content_hash(broken_chain[1])
        coordinator = self.coordinator(
            store, RecordedShadowFixturePort(clock=FrozenClock())
        )
        with self.assertRaises(ResearchCoordinatorConflict):
            coordinator._derive_state(
                plan=self.plan,
                context_pack=self.context_pack,
                run_ref=self.run_ref,
                attempt_ref=self.attempt_ref,
                attempt_hash=self.attempt_hash,
                checkpoints=broken_chain,
            )

        state = copy.deepcopy(store.latest_run_state(self.run_ref))
        state["completed_step_refs"] = state["completed_step_refs"][:-1]
        state.pop("content_hash")
        state["content_hash"] = content_hash(state)
        with self.assertRaises(ResearchCoordinatorConflict):
            validate_research_run_state(state, plan=self.plan)

    def test_run_state_chain_cannot_rebind_plan_or_attempt_before_checkpoint(self) -> None:
        store = ResearchCoordinatorStore(":memory:")
        self.addCleanup(store.close)
        coordinator = self.coordinator(
            store, RecordedShadowFixturePort(clock=FrozenClock())
        )
        first = coordinator._reconcile_state(
            plan=self.plan,
            context_pack=self.context_pack,
            run_ref=self.run_ref,
            attempt_ref=self.attempt_ref,
            attempt_hash=self.attempt_hash,
        )
        self.assertEqual(first["status"], "planned")
        with self.assertRaises(ResearchCoordinatorConflict):
            coordinator._reconcile_state(
                plan=self.plan,
                context_pack=self.context_pack,
                run_ref=self.run_ref,
                attempt_ref="research-attempt:fixture:rebound",
                attempt_hash=content_hash({"attempt": "rebound"}),
            )

        rebound_plan = copy.deepcopy(self.plan)
        rebound_plan["created_at"] = datetime(
            2026, 8, 15, 6, 1, tzinfo=timezone.utc
        ).isoformat(timespec="microseconds")
        rebound_plan.pop("content_hash")
        rebound_plan["content_hash"] = content_hash(rebound_plan)
        rebound_context = build_context_pack(
            [
                {
                    "kind": "mandate",
                    "ref": "research-mandate:fixture:1",
                    "hash": content_hash({"mandate": "official reference sources"}),
                    "priority": 100,
                    "content": (
                        "Read official CNINFO and SEC filings and the recorded "
                        "AlphaEngine shadow."
                    ),
                }
            ],
            task_ref=self.task_ref,
            task_hash=self.task_hash,
            compiled_plan_ref=rebound_plan["id"],
            compiled_plan_hash=rebound_plan["content_hash"],
            claim_index_ref=self.claim_index["id"],
            claim_index_hash=self.claim_index["content_hash"],
            created_at=WIRE_WHEN,
            max_tokens=100,
            max_bytes=2_000,
        )
        with self.assertRaises(ResearchCoordinatorConflict):
            coordinator._reconcile_state(
                plan=rebound_plan,
                context_pack=rebound_context,
                run_ref=self.run_ref,
                attempt_ref=self.attempt_ref,
                attempt_hash=self.attempt_hash,
            )

    def test_fixture_port_idempotency_conflict_is_fail_closed(self) -> None:
        port = RecordedShadowFixturePort(clock=FrozenClock())
        first_step = self.plan["steps"][0]
        requests = self.runner_requests[first_step["id"]]
        port.execute(first_step, requests[0], idempotency_key="shared-key")
        with self.assertRaises(ResearchCoordinatorConflict):
            port.execute(first_step, requests[1], idempotency_key="shared-key")


if __name__ == "__main__":
    unittest.main()
