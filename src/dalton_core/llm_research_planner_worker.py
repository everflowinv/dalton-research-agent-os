"""Scheduler worker for one governed LLM research-planning WorkOrder."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Callable

from .budget_pools import (
    POOL_EXHAUSTED_REASON,
    POOL_EXHAUSTED_STATUS,
    pool_decision,
    record_pool_rejection,
)
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
    BrokerDefinitelyNotSent,
    OpenClawModelAdapter,
    OpenClawModelAdapterError,
)
from .research_context import count_dalton_search_tokens
from .scheduler import Scheduler
from .store import canonical_json, content_hash


SCHEMA_VERSION = "0.1"
# The day ledger admits by phase, and a planning call is an assessment: it is
# the work deciding what to look at, not a second opinion on something already
# produced.  Named once so the pre-lease gate and the admission cannot name
# two different phases and quietly reserve twice.
BUDGET_PHASE = "assessment"


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


def planner_provider_attempt_bound(
    router: ModelRouter,
    routing_policy_ref: str,
    provider_retry: Mapping[str, Any],
) -> tuple[int, str]:
    """Return attempts needed to exhaust each admitted profile exactly once."""

    from .provider_retry import ProviderRetryError, validate_provider_retry

    try:
        retry = validate_provider_retry(provider_retry)
    except ProviderRetryError as exc:
        raise LLMResearchPlannerWorkerRejected(
            f"invalid planner provider retry policy: {exc}"
        ) from exc
    if "unknown_recovery" in retry:
        raise LLMResearchPlannerWorkerRejected(
            "planner provider retry does not support unknown-result recovery"
        )
    try:
        policy = router.get_policy(routing_policy_ref)
    except RoutingPolicyNotFound as exc:
        raise LLMResearchPlannerWorkerRejected(
            "planner routing policy is not registered"
        ) from exc
    allowed = policy.get("filters", {}).get("allowed_profile_ids")
    if not isinstance(allowed, list) or not allowed:
        raise LLMResearchPlannerWorkerRejected(
            "planner routing policy must admit at least one model profile"
        )
    attempts = len(allowed) * (int(retry["max_same_profile_retries"]) + 1)
    return attempts, str(policy["content_hash"])


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
        budget: Any | None = None,
        budget_policy_ref: str | None = None,
        mission_binding: Mapping[str, Any] | None = None,
        provider_retry: Mapping[str, Any] | None = None,
        transport_retry: Mapping[str, Any] | None = None,
        token_counter: Callable[[str], int] = count_dalton_search_tokens,
        lease_seconds: float | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not callable(getattr(scheduler, "work_order_authority", None)):
            raise TypeError("planner worker requires Scheduler WorkOrder authority")
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
        if not isinstance(allowed, list) or not allowed:
            raise LLMResearchPlannerWorkerRejected(
                "planner routing policy must admit at least one model profile"
            )
        self.scheduler = scheduler
        self.router = router
        self.adapter = adapter
        self.store = store
        self.observability = observability
        # C2b: the day ledger, the budget policy version this call accounts
        # against, and the mission binding that carries the capacity pool.
        # All three or none: a worker holding two of them would look budgeted
        # and admit nothing, which is exactly the hole this closes.
        wired = (budget is not None, budget_policy_ref is not None,
                 mission_binding is not None)
        if any(wired) and not all(wired):
            raise ValueError(
                "a budgeted planner worker needs the ledger, the policy "
                "version and the mission binding together"
            )
        self.routing_policy_ref = routing_policy_ref
        self.credential_slot_refs = slots
        self.budget = budget
        self.budget_policy_ref = budget_policy_ref
        self.mission_binding = None if mission_binding is None else dict(mission_binding)
        if provider_retry is None:
            self.provider_retry = None
        else:
            from .provider_retry import ProviderRetryError, validate_provider_retry

            try:
                self.provider_retry = validate_provider_retry(provider_retry)
            except ProviderRetryError as exc:
                raise ValueError(f"invalid planner provider retry policy: {exc}") from exc
            if "unknown_recovery" in self.provider_retry:
                raise ValueError(
                    "planner provider retry does not support unknown-result recovery"
                )
        if transport_retry is None:
            self.transport_retry = None
        else:
            from .document_extraction import validate_transport_retry

            try:
                self.transport_retry = validate_transport_retry(transport_retry)
            except Exception as exc:
                raise ValueError(
                    f"invalid planner transport retry policy: {exc}"
                ) from exc
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

    def _validate_retry_binding(self, work: WorkOrder) -> None:
        if (
            work.metadata.get("provider_retry") != self.provider_retry
            or work.metadata.get("transport_retry") != self.transport_retry
        ):
            raise LLMResearchPlannerWorkerRejected(
                "planner WorkOrder retry policy differs from the worker"
            )

    def _execute_with_safe_retry(
        self,
        work: WorkOrder,
        route: Mapping[str, Any],
        profile: Mapping[str, Any],
    ) -> tuple[ModelInvocation, ResultEnvelope]:
        """Repeat only when the adapter proves no request was dispatched."""

        policy = self.transport_retry or {}
        maximum = int(policy.get("max_definitely_not_sent_retries", 0))
        for retry_number in range(maximum + 1):
            try:
                return self.adapter.execute(work, route, profile)
            except BrokerDefinitelyNotSent:
                if retry_number >= maximum:
                    raise
                backoff = int(policy.get("retry_backoff_seconds", 0))
                if backoff:
                    import time

                    time.sleep(backoff)
        raise AssertionError("planner safe transport retry loop did not return")

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

    def _provider_retry_state(self, work: WorkOrder) -> dict[str, Any] | None:
        """Rebuild the last accepted paid retry from Scheduler/Router authority."""

        if self.provider_retry is None:
            return None
        rows = self.scheduler.connection.execute(
            "SELECT result_envelope_json,result_envelope_hash,attempt_number "
            "FROM scheduler_result_envelopes "
            "WHERE work_order_id=? AND outcome='retryable' "
            "ORDER BY attempt_number DESC",
            (work.id,),
        ).fetchall()
        selected = None
        for row in rows:
            try:
                wire = json.loads(row["result_envelope_json"])
            except (TypeError, json.JSONDecodeError) as exc:
                raise LLMResearchPlannerWorkerConflict(
                    "persisted planner provider retry result is invalid"
                ) from exc
            if (
                canonical_json(wire) != row["result_envelope_json"]
                or content_hash(wire) != row["result_envelope_hash"]
            ):
                raise LLMResearchPlannerWorkerConflict(
                    "persisted planner provider retry result drifted"
                )
            metadata = wire.get("metadata") if isinstance(wire, Mapping) else None
            if not isinstance(metadata, Mapping) or "provider_retry_proof" not in metadata:
                continue
            if "provider_retry_state" not in metadata:
                raise LLMResearchPlannerWorkerConflict(
                    "persisted planner provider retry proof has no route state"
                )
            selected = (row, wire, metadata["provider_retry_state"])
            break
        if selected is None:
            return {
                "excluded_profile_ids": [],
                "retry_profile_version_ref": None,
                "same_profile_retries": 0,
            }
        row, wire, state = selected
        if (
            not isinstance(state, Mapping)
            or set(state) != {
                "excluded_profile_ids", "retry_profile_version_ref",
                "same_profile_retries",
            }
            or not isinstance(state.get("excluded_profile_ids"), list)
            or not all(
                isinstance(item, str) and item
                for item in state["excluded_profile_ids"]
            )
            or len(set(state["excluded_profile_ids"]))
               != len(state["excluded_profile_ids"])
            or state.get("retry_profile_version_ref") is not None
               and not isinstance(state["retry_profile_version_ref"], str)
            or isinstance(state.get("same_profile_retries"), bool)
            or not isinstance(state.get("same_profile_retries"), int)
            or state["same_profile_retries"] < 0
        ):
            raise LLMResearchPlannerWorkerConflict(
                "persisted planner provider retry route state is invalid"
            )
        decisions = self.router.list_decisions(work_order_id=work.id)
        matching = [
            item for item in decisions
            if item.get("attempt_number") == row["attempt_number"]
        ]
        proved_route = matching[-1] if matching else None
        selected_version = (
            None if proved_route is None
            else proved_route.get("selected_profile_version_ref")
        )
        selected_id = (
            None if selected_version is None
            else self.router.get_profile(selected_version)["id"]
        )
        if (
            wire.get("work_order_ref") != work.id
            or wire.get("status") != "retryable"
            or proved_route is None
            or wire.get("metadata", {}).get("route_decision_ref")
               != proved_route.get("id")
            or state["retry_profile_version_ref"] is not None
               and state["retry_profile_version_ref"] != selected_version
            or state["retry_profile_version_ref"] is None
               and selected_id not in state["excluded_profile_ids"]
            or state["same_profile_retries"]
               > self.provider_retry["max_same_profile_retries"]
        ):
            raise LLMResearchPlannerWorkerConflict(
                "persisted planner provider retry state does not match route history"
            )
        return dict(state)

    def _paid_retry_result(
        self,
        *,
        work: WorkOrder,
        lease: Mapping[str, Any],
        route: Mapping[str, Any],
        profile: Mapping[str, Any],
        invocation: ModelInvocation,
        result: ResultEnvelope,
        state: Mapping[str, Any] | None,
    ) -> ResultEnvelope | None:
        if self.provider_retry is None or state is None:
            return None
        from .provider_retry import returned_provider_failure_proof

        proof = returned_provider_failure_proof(invocation, result)
        if proof is None:
            return None
        used = int(state.get("same_profile_retries", 0))
        excluded = list(state.get("excluded_profile_ids", []))
        if used < self.provider_retry["max_same_profile_retries"]:
            retry_profile = profile["profile_version_ref"]
            used += 1
        else:
            if profile["id"] not in excluded:
                excluded.append(profile["id"])
            retry_profile = None
            used = 0
        status = (
            "failed"
            if int(lease["attempt"]["attempt_number"]) >= int(lease["max_attempts"])
            else "retryable"
        )
        return ResultEnvelope(
            schema_version=result.schema_version,
            id=result.id,
            created_at=result.created_at,
            work_order_ref=result.work_order_ref,
            invocation_ref=result.invocation_ref,
            status=status,
            outputs={},
            actual_side_effects=result.actual_side_effects,
            usage_refs=result.usage_refs,
            artifact_refs=result.artifact_refs,
            error=dict(result.error or {}),
            metadata=dict(result.metadata) | {
                "provider_retry_proof": proof,
                "provider_retry_state": {
                    "excluded_profile_ids": excluded,
                    "retry_profile_version_ref": retry_profile,
                    "same_profile_retries": used,
                },
            },
        )

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

    # -- the day ledger (C2b) -----------------------------------------------

    @property
    def budgeted(self) -> bool:
        """Whether this worker's calls reach the mission's day ledger at all.

        False is the behaviour every deployment had before C2b: the call is
        routed, made and accounted, but the mission's daily caps and its four
        capacity pools never see it.  The op result says ``unbudgeted`` in that
        case rather than saying nothing, because a budget that silently does
        not apply is worse than one that is visibly absent.
        """

        return self.budget is not None

    def _day(self) -> str:
        return self.clock().astimezone(timezone.utc).date().isoformat()

    @staticmethod
    def _reserved_micros(work: WorkOrder) -> int:
        """What the call reserves: the WorkOrder's own cost ceiling.

        The same number P14e's ad-hoc pool reserves per round, because it is
        the same number: ``planner_max_cost_usd`` travels from the driver's
        config into this WorkOrder's budget.
        """

        return int(
            (Decimal(str(work.budget["max_cost_usd"])) * 1_000_000).quantize(
                Decimal("1"), rounding=ROUND_HALF_UP)
        )

    def _next_attempt_number(self, work_order_id: str) -> int:
        """The attempt number ``claim`` would hand out, read without claiming.

        The Scheduler numbers forward: ``enqueue`` writes the first event
        already carrying attempt 1, and a requeue writes the next one carrying
        the number after the attempt that just finished.  So the latest event's
        own number *is* the next attempt, and adding one to it would make the
        pre-lease gate reserve under an attempt number the admission never
        uses.
        """

        history = self.scheduler.attempt_history(work_order_id)
        if not history:
            return 1
        return int(history[-1]["attempt_number"])

    def _pool_gate(self, work: WorkOrder) -> dict[str, Any] | None:
        """Refuse a spent pool *before* the WorkOrder is leased, or return None.

        The gate has to come before the claim, not after.  A rejection found
        after claiming holds a lease and has spent one of the WorkOrder's
        bounded attempts; a pool stays spent for the rest of the day, so the
        next few ticks would burn the remaining attempts and leave the loop's
        planning WorkOrder permanently exhausted -- a budget decision turned
        into a dead loop.  Refusing before the lease costs the loop nothing and
        it resumes when the pool refills at midnight.

        ``pool_decision`` is a pure read on the ledger's own connection, and
        the admission inside :meth:`ThesisImpactBudgetStore.admit` re-runs it
        inside the write transaction, so this gate is an early answer rather
        than a second authority.
        """

        if not self.budgeted:
            return None
        decision = pool_decision(
            self.budget.connection,
            mission_binding=self.mission_binding,
            day=self._day(),
            reserved_micros=self._reserved_micros(work),
            work_order_ref=work.id,
            attempt_number=self._next_attempt_number(work.id),
            phase=BUDGET_PHASE,
            now=self.clock(),
        )
        if decision["status"] != "rejected":
            return None
        self._record_refusal(decision)
        return self._pool_refusal(work, decision, gate="pre_lease")

    def _record_refusal(self, rejection: Mapping[str, Any]) -> None:
        """Leave the refusal where every other pool refusal already lives.

        ``ThesisImpactBudgetStore.admit`` records its own rejections inside the
        write transaction that made them, but this gate deliberately refuses
        *before* admitting, so without this the whole pre-lease path would be
        invisible: ``pool_status()["adhoc"]["exhausted"]`` would read false on
        a day the ad-hoc pool refused every planner call there was, and the
        lane that ran out would not appear in ``exhausted_lanes``.  The most
        common refusal in the system would have been the one nobody could see.

        ``INSERT OR IGNORE`` on a content-addressed id, so the same loop
        refused on the same day for the same attempt is one row however many
        ticks ask.  A failure to record is swallowed: observability turning a
        budget decision into a fault is the exact inversion this branch exists
        to prevent.
        """

        try:
            with self.budget.connection as connection:
                record_pool_rejection(connection.cursor(), rejection)
        except sqlite3.Error:
            pass

    @staticmethod
    def _pool_refusal(
        work: WorkOrder, rejection: Mapping[str, Any], *, gate: str,
        completion: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """The returned -- never raised -- shape of a spent pool."""

        wire = {
            "status": "rejected",
            "reason": POOL_EXHAUSTED_REASON,
            "lane_status": POOL_EXHAUSTED_STATUS,
            "pool": rejection.get("pool"),
            "day": rejection.get("day"),
            "spent": rejection.get("spent"),
            "cap": rejection.get("cap"),
            "work_order_ref": work.id,
            "gate": gate,
            "rejection": dict(rejection),
        }
        if completion is not None:
            wire["completion"] = dict(completion)
        return wire

    def _budget_report(
        self, admission: Mapping[str, Any] | None, micros: int, status: str,
    ) -> dict[str, Any]:
        """What the day ledger did with this call, including doing nothing.

        ``unbudgeted`` is the honest word for a planner configuration with no
        ``budget_db``: the call was made and accounted, and the mission's day
        caps and pools did not see it.  It is reported rather than omitted so
        that "is the planner on the ledger yet?" is answerable from one tick
        summary instead of from the installer's history.
        """

        if not self.budgeted:
            return {"status": "unbudgeted"}
        if admission is None:
            return {"status": "not_admitted", "pool": self._pool_name()}
        return {
            "status": "settled",
            "pool": admission.get("pool", self._pool_name()),
            "admission_id": admission["admission_id"],
            "reserved_micros": admission.get("reserved_micros"),
            "settled_micros": micros,
            "cost_status": status,
        }

    def _pool_name(self) -> str | None:
        return None if self.mission_binding is None else self.mission_binding.get("pool")

    def _settle(self, admission: Mapping[str, Any] | None, micros: int) -> None:
        if admission is None:
            return
        from .cockpit_model import settle_day_ledger

        settle_day_ledger(self.budget, admission, actual_micros=micros)

    def run_once(self, work_order: WorkOrder | Mapping[str, Any]) -> dict[str, Any]:
        work = self._work(work_order)
        self._validate_work(work)
        self._validate_retry_binding(work)
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
        # C2b: before the lease, because a spent pool must cost the loop
        # neither an attempt nor a held lease.
        refused = self._pool_gate(work)
        if refused is not None:
            return refused
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
        provider_retry_state = self._provider_retry_state(work)
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
                purpose="plan",
                excluded_profile_ids=(
                    () if provider_retry_state is None
                    else provider_retry_state["excluded_profile_ids"]
                ),
                required_profile_version_ref=(
                    None if provider_retry_state is None
                    else provider_retry_state["retry_profile_version_ref"]
                ),
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
            # C2b: a failing call still says what the ledger did, which here is
            # nothing -- the reservation happens after the route is chosen.
            # Reporting the budget only on success would have made a budgeted
            # install with a dead broker indistinguishable from an unbudgeted
            # one, and the dead-broker case is exactly when somebody asks.
            return {"status": "failed", "route": route, "completion": completion,
                    "budget": self._budget_report(None, 0, "not_admitted")}
        profile = self.router.get_profile(route["selected_profile_version_ref"])
        # C2b: the reservation, against the same day ledger and the same four
        # pools as every cockpit-shaped call.  It happens here -- after the
        # route decision the ledger records, before the broker is spoken to --
        # so nothing is ever paid for that the day did not admit.
        admission: dict[str, Any] | None = None
        reserved = self._reserved_micros(work)
        if self.budgeted:
            from .cockpit_model import admit_day_ledger

            decision = admit_day_ledger(
                self.budget,
                policy_version_id=self.budget_policy_ref,
                day=self._day(),
                work_order_ref=work.id,
                attempt_number=attempt_number,
                phase=BUDGET_PHASE,
                route_decision_ref=route["id"],
                reserved_micros=reserved,
                mission_binding=self.mission_binding,
            )
            if decision["status"] != "admitted":
                # The gate above already answered the common case; reaching
                # here means the pool went empty between the read and the
                # write, or the day cap itself refused.  Either way the lease
                # is live and has to be handed back as a completed attempt.
                exhausted = decision["status"] == "pool_exhausted"
                result = self._control_result(
                    work,
                    attempt_number,
                    code=("MODEL_POOL_EXHAUSTED" if exhausted else "BUDGET_REFUSED"),
                    status=(
                        self._bounded_failure_status(lease) if exhausted
                        else "failed"
                    ),
                    route_ref=route["id"],
                )
                completion = self.scheduler.complete(
                    work.id,
                    attempt_number,
                    WORKER_REF,
                    lease["lease_token"],
                    result,
                    idempotency_key=(
                        f"llm-planner-complete:{work.id}:{attempt_number}"
                    ),
                )
                if exhausted:
                    return self._pool_refusal(
                        work, decision["rejection"], gate="admission",
                        completion=completion,
                    )
                return {
                    "status": "failed",
                    "reason": "budget_refused",
                    "work_order_ref": work.id,
                    "failure": decision["failure"],
                    "route": route,
                    "completion": completion,
                }
            admission = decision["admission"]
        try:
            if route_replayed:
                invocation, adapter_result = self.adapter.replay(work, route, profile)
            else:
                invocation, adapter_result = self._execute_with_safe_retry(
                    work, route, profile
                )
        except OpenClawModelAdapterError as exc:
            evidence = getattr(exc, "post_send_unknown_evidence", None)
            if evidence is not None:
                # Dispatch happened and metering is unknown. Preserve the full
                # reservation before validating or persisting the evidence;
                # retry eligibility is deliberately absent for this Work.
                self._settle(admission, reserved)
                invocation = getattr(evidence, "invocation", None)
                result = getattr(evidence, "result", None)
                if (
                    not isinstance(invocation, ModelInvocation)
                    or not isinstance(result, ResultEnvelope)
                    or invocation.work_order_ref != work.id
                    or invocation.parent_ref != route["id"]
                    or result.work_order_ref != work.id
                    or result.invocation_ref != invocation.id
                    or result.status != "failed"
                    or (result.error or {}).get("code")
                       != "POST_SEND_RESULT_UNKNOWN"
                    or result.metadata.get("route_decision_ref") != route["id"]
                ):
                    raise LLMResearchPlannerWorkerConflict(
                        "adapter post-send unknown evidence is invalid"
                    ) from exc
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
                completion = self.scheduler.complete(
                    work.id,
                    attempt_number,
                    WORKER_REF,
                    lease["lease_token"],
                    result,
                    idempotency_key=(
                        f"llm-planner-complete:{work.id}:{attempt_number}"
                    ),
                )
                return {
                    "status": "failed",
                    "work_order_ref": work.id,
                    "route": route,
                    "profile": profile,
                    "invocation": invocation.to_dict(),
                    "result": result.to_dict(),
                    "accounting": accounting,
                    "completion": completion,
                    "post_send_result_unknown": True,
                    "replayed": False,
                    "budget": self._budget_report(
                        admission, reserved, "reserved"
                    ),
                }
            # Absence of typed post-send evidence is not proof of absence of
            # dispatch. Only the adapter's dedicated definitely-not-sent
            # exception can release the reservation; every other adapter
            # failure retains it in full.
            definitely_not_sent = isinstance(exc, BrokerDefinitelyNotSent)
            settled_micros = 0 if definitely_not_sent else reserved
            settled_status = "not_sent" if definitely_not_sent else "reserved"
            self._settle(admission, settled_micros)
            retryable = definitely_not_sent
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
                "budget": self._budget_report(
                    admission, settled_micros, settled_status
                ),
            }
        except BaseException:
            # C2b: an adapter failure this worker does not model -- a bug, an
            # interrupt, anything that is not an OpenClawModelAdapterError --
            # used to propagate with the reservation still open.  An open
            # reservation counts against the pool at its full reserved amount
            # until something settles it, and nothing ever would: the pool
            # would lose that money for the rest of the day, every time it
            # happened.  Hand it back, then let the exception continue -- this
            # clause changes what the ledger believes, not what the caller sees.
            self._settle(admission, 0)
            raise
        # Settled with the rate card of the link that actually served, exactly
        # as a cockpit call is; a non-succeeded envelope means the broker
        # refused rather than served, and is charged nothing.
        from .provider_retry import returned_provider_failure_proof

        provider_failure_proof = returned_provider_failure_proof(
            invocation, adapter_result
        )
        if adapter_result.status == "succeeded":
            from .cockpit_model import call_cost_micros

            cost_micros, cost_status = call_cost_micros(
                invocation, route, profile, reserved)
        else:
            from .cockpit_model import call_cost_micros

            cost_micros, cost_status = call_cost_micros(
                invocation, route, profile, reserved
            )
            error_code = str((adapter_result.error or {}).get("code", "")).upper()
            local_not_sent = (
                error_code in {
                    "BUSY", "CONCURRENCY_LIMIT", "BROKER_CONCURRENCY_LIMIT",
                    "QUEUE_TIMEOUT", "BROKER_CLOSED",
                }
                and adapter_result.metadata.get("dispatch_proof") == {
                    "authority": "openclaw-model-adapter",
                    "state": "definitely_not_sent",
                    "version": "0.1",
                }
            )
            if local_not_sent:
                cost_micros, cost_status = 0, "not_sent"
            elif cost_status != "actual":
                cost_micros, cost_status = reserved, "reserved"
        self._settle(admission, cost_micros)
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
            return {
                "status": "failed", "route": route, "completion": completion,
                "budget": self._budget_report(admission, cost_micros, cost_status),
            }

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
        elif provider_failure_proof is not None:
            paid_retry = self._paid_retry_result(
                work=work,
                lease=lease,
                route=route,
                profile=profile,
                invocation=invocation,
                result=result,
                state=provider_retry_state,
            )
            if paid_retry is not None:
                result = paid_retry
        completion = self.scheduler.complete(
            work.id,
            attempt_number,
            WORKER_REF,
            lease["lease_token"],
            result,
            idempotency_key=f"llm-planner-complete:{work.id}:{attempt_number}",
            retry_at=(
                self.clock() + timedelta(
                    seconds=self.provider_retry["retry_backoff_seconds"]
                )
                if self.provider_retry is not None
                and result.status == "retryable"
                and result.metadata.get("provider_retry_proof") is not None
                else None
            ),
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
            "budget": self._budget_report(admission, cost_micros, cost_status),
        }


__all__ = [
    "LLMResearchPlannerModelWorker",
    "LLMResearchPlannerWorkerConflict",
    "LLMResearchPlannerWorkerError",
    "LLMResearchPlannerWorkerRejected",
    "planner_provider_attempt_bound",
]
