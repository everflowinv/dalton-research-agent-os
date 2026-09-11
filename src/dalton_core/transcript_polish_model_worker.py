"""Routed, accounted Scheduler worker for transcript-polish candidates."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from typing import Any, Callable

from .contracts import ModelInvocation, ResultEnvelope, WorkOrder
from .model_accounting import record_model_accounting
from .model_router import ModelRouter, RoutingPolicyNotFound
from .openclaw_model_adapter import (
    BrokerConnectionError,
    BrokerDefinitelyNotSent,
    OpenClawModelAdapter,
    OpenClawModelAdapterError,
)
from .research_context import count_dalton_search_tokens
from .scheduler import LeaseExpired, Scheduler
from .store import canonical_json, content_hash
from .transcript_polish import (
    TranscriptPolishError,
    TranscriptPolishWorker,
    parse_transcript_polish_candidate_text,
)
from .transcript_polish_model import (
    TRANSCRIPT_POLISH_MODEL_WORKER_REF,
    TranscriptPolishModelConflict,
    TranscriptPolishModelRejected,
    validate_transcript_polish_model_work_order,
)


SCHEMA_VERSION = "0.1"


class TranscriptPolishModelWorkerError(RuntimeError):
    pass


class TranscriptPolishModelWorkerRejected(TranscriptPolishModelWorkerError):
    pass


class TranscriptPolishModelWorkerConflict(TranscriptPolishModelWorkerError):
    pass


def _utc(clock: Callable[[], datetime]) -> str:
    value = clock()
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise TranscriptPolishModelWorkerError(
            "worker clock must include timezone"
        )
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")


class RoutedTranscriptPolishModelWorker:
    """Route, execute, account, and close one candidate-generation call."""

    # Subclasses may specialize the closed task and output contract while
    # retaining the same Scheduler lease, route, replay and accounting chain.
    worker_ref = TRANSCRIPT_POLISH_MODEL_WORKER_REF
    namespace = "transcript-polish-model"
    purpose: str | None = None

    @staticmethod
    def _validate_candidate_sink(sink: Any) -> None:
        if not isinstance(sink, TranscriptPolishWorker):
            raise TypeError("polish_worker must be TranscriptPolishWorker")

    @staticmethod
    def _validate_scheduler_store(scheduler, store):
        if scheduler.connection is not store.connection:
            raise TypeError("worker and Scheduler must share one Core connection")

    def _parse_candidate(self, text: str, work: WorkOrder) -> None:
        parse_transcript_polish_candidate_text(text)

    def __init__(
        self,
        *,
        scheduler: Scheduler,
        router: ModelRouter,
        adapter: OpenClawModelAdapter,
        store: Any,
        observability: Any,
        polish_worker: TranscriptPolishWorker,
        routing_policy_ref: str,
        credential_slot_refs: Sequence[str],
        token_counter: Callable[[str], int] = count_dalton_search_tokens,
        lease_seconds: float | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._validate_scheduler_store(scheduler, store)
        if observability.store is not store:
            raise TypeError("worker accounting must share one Core authority")
        self._validate_candidate_sink(polish_worker)
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
            raise TranscriptPolishModelWorkerRejected(
                "transcript routing policy is not registered"
            ) from exc
        allowed = policy.get("filters", {}).get("allowed_profile_ids")
        purposes = policy.get("purpose_overrides") or {}
        purpose_selection = (
            purposes.get(self.purpose) if self.purpose is not None else None
        )
        if (
            (not isinstance(allowed, list) or not allowed)
            and purpose_selection is None
        ):
            raise TranscriptPolishModelWorkerRejected(
                "transcript routing policy has no approved model candidates"
            )
        self.scheduler = scheduler
        self.router = router
        self.adapter = adapter
        self.store = store
        self.observability = observability
        self.polish_worker = polish_worker
        self.routing_policy_ref = routing_policy_ref
        self.credential_slot_refs = slots
        self.token_counter = token_counter
        self.lease_seconds = lease_seconds
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    @staticmethod
    def _work(value: WorkOrder | Mapping[str, Any]) -> WorkOrder:
        try:
            return validate_transcript_polish_model_work_order(value)
        except (TranscriptPolishModelConflict, TranscriptPolishModelRejected) as exc:
            raise TranscriptPolishModelWorkerRejected(str(exc)) from exc

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
            raise TranscriptPolishModelWorkerConflict(
                "Scheduler retry bounds are invalid"
            )
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
        digest = content_hash(identity)[:32]
        return ResultEnvelope(
            schema_version=SCHEMA_VERSION,
            id="result:transcript-polish-model-control-" + digest,
            created_at=created_at or _utc(self.clock),
            work_order_ref=work.id,
            invocation_ref=invocation_ref or "invocation:not-started:" + digest,
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

    def _admit_candidate(self, work: WorkOrder, candidate_text: str) -> None:
        probe_ref = work.metadata["probe_work_order_ref"]
        row = self.store.connection.execute(
            "SELECT work_order_json,work_order_hash FROM scheduler_work_orders "
            "WHERE work_order_id=?",
            (probe_ref,),
        ).fetchone()
        if row is None:
            raise TranscriptPolishModelWorkerConflict(
                "transcript probe WorkOrder authority is missing"
            )
        try:
            probe_wire = json.loads(row["work_order_json"])
        except (TypeError, json.JSONDecodeError) as exc:
            raise TranscriptPolishModelWorkerConflict(
                "transcript probe WorkOrder authority is invalid"
            ) from exc
        if (
            canonical_json(probe_wire) != row["work_order_json"]
            or content_hash(probe_wire) != row["work_order_hash"]
            or row["work_order_hash"]
            != work.metadata["probe_work_order_hash"]
        ):
            raise TranscriptPolishModelWorkerConflict(
                "transcript probe WorkOrder authority drifted"
            )
        self.polish_worker.execute(probe_wire, candidate_text)

    @staticmethod
    def _reuse_invocation(
        saved: Mapping[str, Any], replayed: ModelInvocation
    ) -> ModelInvocation:
        saved_wire = dict(saved)
        saved_alias = saved_wire.pop("invocation_id", None)
        if saved_alias is not None and saved_alias != saved_wire.get("id"):
            raise TranscriptPolishModelWorkerConflict(
                "saved invocation aliases disagree"
            )
        replayed_wire = replayed.to_dict()
        stable_fields = {
            "schema_version", "id", "work_order_ref", "profile_ref",
            "granularity", "capability", "provider", "model",
            "model_family", "input_refs", "output_refs", "side_effects",
            "runtime_ref", "actor_ref", "parent_ref", "environment_hash",
        }
        if any(
            saved_wire.get(field) != replayed_wire.get(field)
            for field in stable_fields
        ):
            raise TranscriptPolishModelWorkerConflict(
                "replayed result differs from committed ModelInvocation"
            )
        return ModelInvocation.from_dict(saved_wire)

    def _before_model_call(self, work, route, profile, replayed):
        """Specialized tasks reserve budget before any broker I/O."""

    def _after_accounting(self, work, route, accounting):
        """Specialized tasks settle only after durable usage/cost records."""

    def _after_capacity_deferred(self, work, route, adapter_result):
        """Specialized budget ledgers may release proved broker-local refusal."""

    def _execute_model(self, work, route, profile):
        """Admit immediately before the adapter's execution boundary."""
        self._before_model_call(work, route, profile, False)
        return self.adapter.execute(work, route, profile)

    def _complete_route_rejection(self, work, lease, route):
        attempt_number = lease["attempt"]["attempt_number"]
        result = self._control_result(
            work, attempt_number, code="MODEL_ROUTE_REJECTED",
            status="failed", route_ref=route["id"],
        )
        completion = self.scheduler.complete(
            work.id, attempt_number, self.worker_ref, lease["lease_token"], result,
            idempotency_key=f"{self.namespace}-complete:{work.id}:{attempt_number}",
        )
        return {"status": "failed", "route": route, "completion": completion}

    def _complete_adapter_failure(self, work, lease, route, exc):
        attempt_number = lease["attempt"]["attempt_number"]
        # A generic connection error may follow sendall and therefore may
        # describe paid work. Only the adapter's proved pre-send subtype can
        # safely receive another Scheduler attempt.
        retryable = isinstance(exc, BrokerDefinitelyNotSent)
        result = self._control_result(
            work,
            attempt_number,
            code=(
                "MODEL_ADAPTER_UNAVAILABLE"
                if isinstance(exc, BrokerConnectionError)
                else "MODEL_ADAPTER_REJECTED"
            ),
            status=(
                self._bounded_failure_status(lease) if retryable else "failed"
            ),
            route_ref=route["id"],
        )
        completion = self.scheduler.complete(
            work.id,
            attempt_number,
            self.worker_ref,
            lease["lease_token"],
            result,
            idempotency_key=(
                f"{self.namespace}-complete:{work.id}:{attempt_number}"
            ),
        )
        return {
            "status": (
                "retryable"
                if result.status == "retryable"
                and completion["work_state"] == "ready"
                else "failed"
            ),
            "route": route,
            "completion": completion,
            "error_type": type(exc).__name__,
        }

    def run_once(
        self, work_order: WorkOrder | Mapping[str, Any]
    ) -> dict[str, Any]:
        work = self._work(work_order)
        status = self.scheduler.status(work.id)
        if status["work_order_hash"] != content_hash(work.to_dict()):
            raise TranscriptPolishModelWorkerConflict(
                "Scheduler retains a different transcript model WorkOrder"
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
            self.worker_ref,
            work_order_id=work.id,
            lease_seconds=self.lease_seconds,
        )
        if lease is None:
            return {"status": "waiting", "work_order_ref": work.id}
        if (
            canonical_json(lease["work_order"])
            != canonical_json(work.to_dict())
            or lease["work_order_hash"] != content_hash(work.to_dict())
        ):
            raise TranscriptPolishModelWorkerConflict(
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
            profile = self.router.get_profile(
                route["selected_profile_version_ref"]
            )
            try:
                self._before_model_call(work, route, profile, True)
                invocation, adapter_result = self.adapter.replay(
                    work, route, profile
                )
            except OpenClawModelAdapterError as exc:
                return self._complete_adapter_failure(work, lease, route, exc)
        else:
            policy = self.router.get_policy(self.routing_policy_ref)
            has_chain = bool(policy.get("fallback_chains")) or (
                self.purpose is not None
                and self.purpose in (policy.get("purpose_overrides") or {})
            )
            estimated_input = max(1, self.token_counter(work.question))
            estimated_output = int(work.budget["max_output_tokens"])
            if not has_chain:
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
                    purpose=self.purpose,
                    idempotency_key=(
                        f"{self.namespace}-route:{work.id}:{attempt_number}"
                    ),
                )
                route = routed["decision"]
                if route["outcome"] != "selected":
                    return self._complete_route_rejection(work, lease, route)
                profile = self.router.get_profile(
                    route["selected_profile_version_ref"]
                )
                try:
                    invocation, adapter_result = self._execute_model(
                        work, route, profile
                    )
                except OpenClawModelAdapterError as exc:
                    return self._complete_adapter_failure(work, lease, route, exc)
            else:
                # Imported here because the chain registry imports CockpitModel,
                # whose extraction validator subclasses this worker.
                from .model_fallback_chain import (
                    classify_model_failure,
                    execute_chain,
                )
                from .openclaw_model_adapter import BrokerDefinitelyNotSent


                def call(route, profile):
                    try:
                        value = self._execute_model(work, route, profile)
                    except BrokerDefinitelyNotSent as exc:
                        return {
                            "outcome": "failed",
                            "failure_class": classify_model_failure(exc),
                        }
                    except OpenClawModelAdapterError:
                        return {
                            "outcome": "failed",
                            "failure_class": "unclassified_failure",
                        }
                    invocation, envelope = value
                    code = str((envelope.error or {}).get("code", "")).upper()
                    if envelope.status == "failed" and code in {
                        "BUSY", "CONCURRENCY_LIMIT", "BROKER_CONCURRENCY_LIMIT",
                        "QUEUE_TIMEOUT", "BROKER_CLOSED",
                    }:
                        # This typed broker-local result is authoritative proof
                        # that no provider request was made. Release the durable
                        # reservation before execute_chain returns its deferred
                        # outcome; the post-chain served path is unreachable.
                        self._after_capacity_deferred(work, route, envelope)
                        return {
                            "outcome": "failed",
                            "failure_class": "capacity_busy",
                            "error_code": code,
                            "reason": (envelope.error or {}).get(
                                "message", "broker capacity unavailable"),
                        }
                    # A returned envelope may represent a paid provider response.
                    # Preserve it for the normal accounting path; only failures
                    # before an invocation exists are safe to switch past here.
                    return {"outcome": "served", "value": value}

                chained = execute_chain(
                    self.router,
                    work,
                    purpose=self.purpose or "document_extraction",
                    capability="research",
                    attempt_number=attempt_number,
                    policy_version_ref=self.routing_policy_ref,
                    credential_slot_refs=self.credential_slot_refs,
                    required_modalities=("text",),
                    required_context_tokens=estimated_input + estimated_output,
                    estimated_input_tokens=estimated_input,
                    estimated_output_tokens=estimated_output,
                    idempotency_prefix=(
                        f"{self.namespace}-route:{work.id}:{attempt_number}"
                    ),
                    call=call,
                )
                if chained["status"] != "served":
                    route_ref = chained.get("route_decision_ref")
                    if route_ref is None and chained.get("links"):
                        route_ref = chained["links"][-1]["decision_id"]
                    result = self._control_result(
                        work,
                        attempt_number,
                        code="MODEL_CHAIN_" + chained["status"].upper(),
                        status=(
                            "retryable" if chained.get("reason") == "capacity_busy"
                            else (self._bounded_failure_status(lease)
                                  if chained["status"] == "exhausted"
                                  else "failed")
                        ),
                        route_ref=route_ref,
                    )
                    completion = self.scheduler.complete(
                        work.id,
                        attempt_number,
                        self.worker_ref,
                        lease["lease_token"],
                        result,
                        idempotency_key=(
                            f"{self.namespace}-complete:{work.id}:{attempt_number}"
                        ),
                    )
                    return {
                        "status": (
                            "retryable"
                            if result.status == "retryable"
                            and completion["work_state"] == "ready"
                            else "failed"
                        ),
                        "chain": chained,
                        "completion": completion,
                    }
                route = chained["decision"]
                profile = chained["profile"]
                invocation, adapter_result = chained["value"]
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
                self.worker_ref,
                lease["lease_token"],
                result,
                idempotency_key=(
                    f"{self.namespace}-complete:{work.id}:{attempt_number}"
                ),
            )
            return {"status": "failed", "route": route, "completion": completion}
        capacity_code = str((adapter_result.error or {}).get("code", "")).upper()
        if adapter_result.status == "failed" and capacity_code in {
            "BUSY", "CONCURRENCY_LIMIT", "BROKER_CONCURRENCY_LIMIT",
            "QUEUE_TIMEOUT", "BROKER_CLOSED",
        }:
            self._after_capacity_deferred(work, route, adapter_result)
            result = self._control_result(
                work, attempt_number, code=capacity_code, status="retryable",
                route_ref=route["id"], created_at=adapter_result.created_at,
            )
            completion = self.scheduler.complete(
                work.id, attempt_number, self.worker_ref, lease["lease_token"],
                result, idempotency_key=(
                    f"{self.namespace}-complete:{work.id}:{attempt_number}"),
            )
            return {"status": ("retryable" if completion["work_state"] == "ready"
                               else "failed"),
                    "route": route, "completion": completion}
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
            actor_ref=self.worker_ref,
            namespace=self.namespace,
        )
        accounting_failure = self._after_accounting(work, route, accounting)
        try:
            self.scheduler.validate_lease_for_use(
                work.id,
                attempt_number,
                self.worker_ref,
                lease["lease_token"],
                lease_revision_ref=lease["lease"]["id"],
                lease_hash=lease["lease"]["content_hash"],
                work_order_hash=lease["work_order_hash"],
            )
        except LeaseExpired:
            scheduler_status = self.scheduler.status(work.id)
            return {
                "status": (
                    "retryable"
                    if scheduler_status["state"] == "ready"
                    else "failed"
                ),
                "work_order_ref": work.id,
                "route": route,
                "profile": profile,
                "invocation": invocation.to_dict(),
                "result": adapter_result.to_dict(),
                "accounting": accounting,
                "completion": None,
                "output_error": None,
                "route_replayed": route_replayed,
                "late_completion_rejected": True,
                "replayed": False,
            }
        result = adapter_result
        if accounting_failure is not None:
            result = self._control_result(work, attempt_number, code=accounting_failure, status="failed",
                invocation_ref=invocation.id, route_ref=route["id"], usage_refs=adapter_result.usage_refs,
                created_at=adapter_result.created_at)
        output_error = None
        if result.status == "succeeded":
            try:
                if set(result.outputs) != {"text", "content_hash"}:
                    raise ValueError("result output shape is invalid")
                text = result.outputs["text"]
                if not isinstance(text, str):
                    raise ValueError("result text is invalid")
                self._parse_candidate(text, work)
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
            else:
                try:
                    self._admit_candidate(work, text)
                except TranscriptPolishError as exc:
                    output_error = type(exc).__name__
                    result = self._control_result(
                        work,
                        attempt_number,
                        code="MODEL_CANDIDATE_CONSERVATION_REJECTED",
                        status=self._bounded_failure_status(lease),
                        invocation_ref=invocation.id,
                        route_ref=route["id"],
                        usage_refs=adapter_result.usage_refs,
                        created_at=adapter_result.created_at,
                    )
        try:
            completion = self.scheduler.complete(
                work.id,
                attempt_number,
                self.worker_ref,
                lease["lease_token"],
                result,
                idempotency_key=(
                    f"{self.namespace}-complete:{work.id}:{attempt_number}"
                ),
            )
        except LeaseExpired:
            # The adapter may finish after its Scheduler lease.  Core correctly
            # rejects that late completion, but the broker result and model
            # accounting are already durable.  Return a bounded retry state so
            # the next attempt can use adapter.replay() without another model
            # execution.
            scheduler_status = self.scheduler.status(work.id)
            return {
                "status": (
                    "retryable"
                    if scheduler_status["state"] == "ready"
                    else "failed"
                ),
                "work_order_ref": work.id,
                "route": route,
                "profile": profile,
                "invocation": invocation.to_dict(),
                "result": result.to_dict(),
                "accounting": accounting,
                "completion": None,
                "output_error": output_error,
                "route_replayed": route_replayed,
                "late_completion_rejected": True,
                "replayed": False,
            }
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
    "RoutedTranscriptPolishModelWorker",
    "TranscriptPolishModelWorkerError",
    "TranscriptPolishModelWorkerRejected",
    "TranscriptPolishModelWorkerConflict",
]
