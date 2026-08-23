"""Scheduler worker for one governed LLM research-planning WorkOrder."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from typing import Any, Callable

from .contracts import ModelInvocation, ResultEnvelope, WorkOrder
from .llm_research_planner import (
    LLM_RESEARCH_PLANNER_HASH,
    LLM_RESEARCH_PLANNER_REF,
    PLANNER_CANDIDATE_CONTRACT_HASH,
    WORKER_REF,
    parse_planner_candidate_text,
)
from .model_accounting import record_model_accounting
from .model_router import ModelRouter, RoutingPolicyNotFound
from .openclaw_model_adapter import (
    BrokerConnectionError,
    OpenClawModelAdapter,
    OpenClawModelAdapterError,
)
from .research_context import count_dalton_search_tokens
from .scheduler import Scheduler
from .store import canonical_json, content_hash


SCHEMA_VERSION = "0.1"


class LLMResearchPlannerWorkerError(RuntimeError):
    pass


class LLMResearchPlannerWorkerRejected(LLMResearchPlannerWorkerError):
    pass


class LLMResearchPlannerWorkerConflict(LLMResearchPlannerWorkerError):
    pass


def _utc(clock: Callable[[], datetime]) -> str:
    value = clock()
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise LLMResearchPlannerWorkerError("worker clock must include timezone")
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")


class LLMResearchPlannerModelWorker:
    """Route, execute, account, and close one exact planner model call."""

    def __init__(
        self,
        *,
        scheduler: Scheduler,
        router: ModelRouter,
        adapter: OpenClawModelAdapter,
        store: Any,
        observability: Any,
        routing_policy_ref: str,
        credential_slot_refs: Sequence[str],
        token_counter: Callable[[str], int] = count_dalton_search_tokens,
        lease_seconds: float | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if scheduler.connection is not store.connection:
            raise TypeError("worker and Scheduler must share one Core connection")
        if observability.store is not store:
            raise TypeError("worker accounting must share one Core authority")
        if not isinstance(routing_policy_ref, str) or not routing_policy_ref:
            raise ValueError("routing_policy_ref must be non-empty")
        slots = tuple(credential_slot_refs)
        if not slots or any(not isinstance(item, str) or not item for item in slots):
            raise ValueError("credential_slot_refs must be non-empty")
        if len(set(slots)) != len(slots):
            raise ValueError("credential_slot_refs must be unique")
        try:
            policy = router.get_policy(routing_policy_ref)
        except RoutingPolicyNotFound as exc:
            raise LLMResearchPlannerWorkerRejected(
                "planner routing policy is not registered"
            ) from exc
        allowed = policy.get("filters", {}).get("allowed_profile_ids")
        if not isinstance(allowed, list) or len(allowed) != 1:
            raise LLMResearchPlannerWorkerRejected(
                "planner routing policy must pin exactly one model profile"
            )
        self.scheduler = scheduler
        self.router = router
        self.adapter = adapter
        self.store = store
        self.observability = observability
        self.routing_policy_ref = routing_policy_ref
        self.credential_slot_refs = slots
        self.token_counter = token_counter
        self.lease_seconds = lease_seconds
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    @staticmethod
    def _work(value: WorkOrder | Mapping[str, Any]) -> WorkOrder:
        try:
            return value if isinstance(value, WorkOrder) else WorkOrder.from_dict(value)
        except Exception as exc:
            raise LLMResearchPlannerWorkerConflict(
                "planner WorkOrder is invalid"
            ) from exc

    @staticmethod
    def _validate_work(work: WorkOrder) -> None:
        metadata = work.metadata
        if (
            work.requested_capabilities != ("research",)
            or len(work.input_refs) != 1
            or work.declared_side_effects
            or metadata.get("control_plane") != "bounded-llm-research-planner"
            or metadata.get("phase") != "planning"
            or metadata.get("planner_ref") != LLM_RESEARCH_PLANNER_REF
            or metadata.get("planner_hash") != LLM_RESEARCH_PLANNER_HASH
            or metadata.get("planner_context_pack_ref") != work.input_refs[0]
            or metadata.get("candidate_contract_hash")
            != PLANNER_CANDIDATE_CONTRACT_HASH
        ):
            raise LLMResearchPlannerWorkerRejected(
                "WorkOrder is not an admitted LLM planner call"
            )

    @staticmethod
    def _bounded_failure_status(lease: Mapping[str, Any]) -> str:
        attempt = lease.get("attempt", {}).get("attempt_number")
        maximum = lease.get("max_attempts")
        if (
            isinstance(attempt, bool)
            or not isinstance(attempt, int)
            or isinstance(maximum, bool)
            or not isinstance(maximum, int)
            or attempt < 1
            or maximum < attempt
        ):
            raise LLMResearchPlannerWorkerConflict("Scheduler retry bounds are invalid")
        return "failed" if attempt >= maximum else "retryable"

    def _control_result(
        self,
        work: WorkOrder,
        attempt_number: int,
        *,
        code: str,
        status: str,
        invocation_ref: str | None = None,
        route_ref: str | None = None,
        usage_refs: Sequence[str] = (),
        created_at: str | None = None,
    ) -> ResultEnvelope:
        identity = {
            "work_order_ref": work.id,
            "attempt_number": attempt_number,
            "code": code,
            "invocation_ref": invocation_ref,
            "route_ref": route_ref,
        }
        return ResultEnvelope(
            schema_version=SCHEMA_VERSION,
            id="result:llm-planner-control-" + content_hash(identity)[:32],
            created_at=created_at or _utc(self.clock),
            work_order_ref=work.id,
            invocation_ref=(
                invocation_ref
                or "invocation:not-started:" + content_hash(identity)[:32]
            ),
            status=status,
            outputs={},
            actual_side_effects=(),
            usage_refs=tuple(usage_refs),
            artifact_refs=(),
            error={"code": code},
            metadata={
                "control_plane_failure": True,
                "route_decision_ref": route_ref,
            },
        )

    def _accepted_attempts(self, work_order_id: str) -> set[int]:
        return {
            event["attempt_number"]
            for event in self.scheduler.attempt_history(work_order_id)
            if event.get("result_envelope_id") is not None
        }

    def _saved_invocation(self, invocation_id: str) -> dict[str, Any] | None:
        row = self.store.connection.execute(
            "SELECT invocation_json FROM model_invocations WHERE invocation_id=?",
            (invocation_id,),
        ).fetchone()
        return None if row is None else json.loads(row["invocation_json"])

    @staticmethod
    def _reuse_invocation(
        saved: Mapping[str, Any], replayed: ModelInvocation
    ) -> ModelInvocation:
        replayed_wire = replayed.to_dict()
        stable_fields = {
            "schema_version", "id", "work_order_ref", "profile_ref", "granularity",
            "capability", "provider", "model", "model_family", "input_refs",
            "output_refs", "side_effects", "runtime_ref", "actor_ref", "parent_ref",
            "environment_hash",
        }
        if any(saved.get(field) != replayed_wire.get(field) for field in stable_fields):
            raise LLMResearchPlannerWorkerConflict(
                "replayed result differs from committed ModelInvocation"
            )
        return ModelInvocation.from_dict(saved)

    def run_once(self, work_order: WorkOrder | Mapping[str, Any]) -> dict[str, Any]:
        work = self._work(work_order)
        self._validate_work(work)
        status = self.scheduler.status(work.id)
        if status["work_order_hash"] != content_hash(work.to_dict()):
            raise LLMResearchPlannerWorkerConflict(
                "Scheduler retains a different WorkOrder"
            )
        formal = self.scheduler.formal_result(work.id)
        if formal is not None:
            return {
                "status": formal["terminal_state"],
                "work_order_ref": work.id,
                "formal_result": formal,
                "replayed": True,
            }
        lease = self.scheduler.claim(
            WORKER_REF, work_order_id=work.id, lease_seconds=self.lease_seconds
        )
        if lease is None:
            return {"status": "waiting", "work_order_ref": work.id}
        if (
            canonical_json(lease["work_order"]) != canonical_json(work.to_dict())
            or lease["work_order_hash"] != content_hash(work.to_dict())
        ):
            raise LLMResearchPlannerWorkerConflict(
                "Scheduler lease does not retain the exact WorkOrder"
            )
        attempt_number = lease["attempt"]["attempt_number"]
        prior = self.router.list_decisions(work_order_id=work.id)
        accepted_attempts = self._accepted_attempts(work.id)
        recovery_route = (
            prior[-1]
            if prior and prior[-1]["attempt_number"] not in accepted_attempts
            else None
        )
        route_replayed = recovery_route is not None
        if recovery_route is not None:
            route = recovery_route
        else:
            estimated_input = max(1, self.token_counter(work.question))
            estimated_output = int(work.budget["max_output_tokens"])
            routed = self.router.route(
                work,
                attempt_number=attempt_number,
                capability="research",
                policy_version_ref=self.routing_policy_ref,
                credential_slot_refs=self.credential_slot_refs,
                required_modalities=("text",),
                required_context_tokens=estimated_input + estimated_output,
                estimated_input_tokens=estimated_input,
                estimated_output_tokens=estimated_output,
                decision_kind="initial" if not prior else "retry",
                previous_decision_ref=None if not prior else prior[-1]["id"],
                producer_family=None,
                idempotency_key=f"llm-planner-route:{work.id}:{attempt_number}",
            )
            route = routed["decision"]
        if route["outcome"] != "selected":
            result = self._control_result(
                work,
                attempt_number,
                code="MODEL_ROUTE_REJECTED",
                status="failed",
                route_ref=route["id"],
            )
            completion = self.scheduler.complete(
                work.id,
                attempt_number,
                WORKER_REF,
                lease["lease_token"],
                result,
                idempotency_key=f"llm-planner-complete:{work.id}:{attempt_number}",
            )
            return {"status": "failed", "route": route, "completion": completion}
        profile = self.router.get_profile(route["selected_profile_version_ref"])
        try:
            if route_replayed:
                invocation, adapter_result = self.adapter.replay(work, route, profile)
            else:
                invocation, adapter_result = self.adapter.execute(work, route, profile)
        except OpenClawModelAdapterError as exc:
            retryable = isinstance(exc, BrokerConnectionError)
            result = self._control_result(
                work,
                attempt_number,
                code=(
                    "MODEL_ADAPTER_UNAVAILABLE"
                    if retryable
                    else "MODEL_ADAPTER_REJECTED"
                ),
                status=(self._bounded_failure_status(lease) if retryable else "failed"),
                route_ref=route["id"],
            )
            completion = self.scheduler.complete(
                work.id,
                attempt_number,
                WORKER_REF,
                lease["lease_token"],
                result,
                idempotency_key=f"llm-planner-complete:{work.id}:{attempt_number}",
            )
            return {
                "status": (
                    "retryable"
                    if result.status == "retryable" and completion["work_state"] == "ready"
                    else "failed"
                ),
                "route": route,
                "completion": completion,
                "error_type": type(exc).__name__,
            }
        if (
            route_replayed
            and adapter_result.status == "failed"
            and adapter_result.error is not None
            and adapter_result.error.get("code") == "IDEMPOTENCY_MISS"
        ):
            result = self._control_result(
                work,
                attempt_number,
                code="MODEL_RECOVERY_MISS",
                status="failed",
                route_ref=route["id"],
                created_at=adapter_result.created_at,
            )
            completion = self.scheduler.complete(
                work.id,
                attempt_number,
                WORKER_REF,
                lease["lease_token"],
                result,
                idempotency_key=f"llm-planner-complete:{work.id}:{attempt_number}",
            )
            return {"status": "failed", "route": route, "completion": completion}

        saved = self._saved_invocation(invocation.id)
        if saved is None:
            self.store.register_invocation(invocation.to_dict())
        else:
            invocation = self._reuse_invocation(saved, invocation)
        accounting = record_model_accounting(
            self.observability,
            invocation,
            route,
            profile,
            actor_ref=WORKER_REF,
            namespace="llm-research-planner",
        )
        result = adapter_result
        output_error = None
        if result.status == "succeeded":
            try:
                if set(result.outputs) != {"text", "content_hash"}:
                    raise ValueError("result output shape is invalid")
                text = result.outputs["text"]
                if not isinstance(text, str):
                    raise ValueError("result text is invalid")
                parse_planner_candidate_text(text)
            except Exception as exc:
                output_error = type(exc).__name__
                result = self._control_result(
                    work,
                    attempt_number,
                    code="MODEL_OUTPUT_CONTRACT_REJECTED",
                    status=self._bounded_failure_status(lease),
                    invocation_ref=invocation.id,
                    route_ref=route["id"],
                    usage_refs=adapter_result.usage_refs,
                    created_at=adapter_result.created_at,
                )
        completion = self.scheduler.complete(
            work.id,
            attempt_number,
            WORKER_REF,
            lease["lease_token"],
            result,
            idempotency_key=f"llm-planner-complete:{work.id}:{attempt_number}",
        )
        if result.status == "succeeded":
            normalized = "succeeded"
        elif result.status == "retryable" and completion["work_state"] == "ready":
            normalized = "retryable"
        else:
            normalized = "failed"
        return {
            "status": normalized,
            "work_order_ref": work.id,
            "route": route,
            "profile": profile,
            "invocation": invocation.to_dict(),
            "result": result.to_dict(),
            "accounting": accounting,
            "completion": completion,
            "output_error": output_error,
            "route_replayed": route_replayed,
            "replayed": False,
        }


__all__ = [
    "LLMResearchPlannerModelWorker",
    "LLMResearchPlannerWorkerConflict",
    "LLMResearchPlannerWorkerError",
    "LLMResearchPlannerWorkerRejected",
]
