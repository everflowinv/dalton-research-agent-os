"""ModelRouter/OpenClaw execution lane for thesis-impact WorkOrders.

The worker owns no research state.  It claims an immutable Scheduler
WorkOrder, obtains one exact ModelRouter decision, executes only that route
through the existing OpenClaw adapter, commits the ModelInvocation and its
usage/cost authority, validates the phase output contract, and only then
completes the Scheduler attempt.

Verification routes derive the producer model family from the exact persisted
assessment; callers cannot choose the independence constraint.  When a phase-
pinned verifier policy is configured, verification must route under that exact
immutable policy and a same-family producer stays fail-closed.  When a day
budget authority is configured, every broker call first reserves against the
immutable day cap (over-cap stops with a durable rejection and a formal
DAY_BUDGET_EXCEEDED failure) and settles to actual accounted cost afterwards.
Invalid model JSON becomes a bounded retryable result instead of a succeeded
formal result.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from fractions import Fraction
from typing import Any

from .contracts import ModelInvocation, ResultEnvelope, WorkOrder
from .model_router import ModelRouter, RoutingPolicyNotFound
from .observability import ObservabilityStore
from .openclaw_model_adapter import (
    BrokerConnectionError,
    OpenClawModelAdapter,
    OpenClawModelAdapterError,
)
from .research_context import count_dalton_search_tokens
from .scheduler import Scheduler
from .store import canonical_json, content_hash
from .thesis_impact import (
    VERIFIER_BINDING_MODE,
    VERIFIER_DECISION_SCHEMA_VERSION,
    VERIFIER_OUTPUT_SCHEMA_VERSION,
    ThesisImpactAuthority,
    bind_thesis_impact_verifier_decision,
    validate_thesis_impact_model_output,
    validate_thesis_impact_verifier_consistency,
    validate_thesis_impact_verifier_output,
)
from .thesis_impact_budget import (
    ThesisImpactBudgetStore,
    ThesisImpactDayBudgetExceeded,
)
from .thesis_impact_control import ResearchPlanThesisImpactCoordinator


SCHEMA_VERSION = "0.1"
WORKER_REF = "worker:thesis-impact-model"
ACTOR_REF = "system:thesis-impact-model-worker"


class ThesisImpactModelWorkerError(RuntimeError):
    """Base error for the routed thesis-impact execution lane."""


class ThesisImpactModelWorkerConflict(ThesisImpactModelWorkerError):
    """An immutable WorkOrder, route, invocation or output binding drifted."""


class ThesisImpactModelWorkerRejected(ThesisImpactModelWorkerError):
    """The exact WorkOrder cannot be admitted to an authorized model route."""


def _utc(value: datetime) -> str:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ThesisImpactModelWorkerError("worker clock must include timezone")
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


class ThesisImpactModelWorker:
    """Execute one assessment/verifier WorkOrder with durable accounting."""

    def __init__(
        self,
        *,
        scheduler: Scheduler,
        router: ModelRouter,
        adapter: OpenClawModelAdapter,
        impact: ThesisImpactAuthority,
        observability: ObservabilityStore,
        routing_policy_ref: str,
        credential_slot_refs: Sequence[str],
        verifier_routing_policy_ref: str | None = None,
        budget: ThesisImpactBudgetStore | None = None,
        budget_policy_version_id: str | None = None,
        token_counter: Callable[[str], int] = count_dalton_search_tokens,
        lease_seconds: float | None = None,
        clock: Callable[[], datetime] | None = None,
        fault_hook: Callable[[str], None] | None = None,
    ) -> None:
        if impact.scheduler is not scheduler:
            raise TypeError("worker and impact must share one Scheduler authority")
        if observability.store is not impact.store:
            raise TypeError("worker accounting and impact must share one Core authority")
        if not isinstance(routing_policy_ref, str) or not routing_policy_ref:
            raise ValueError("routing_policy_ref must be a non-empty string")
        if verifier_routing_policy_ref is not None and (
            not isinstance(verifier_routing_policy_ref, str)
            or not verifier_routing_policy_ref
        ):
            raise ValueError(
                "verifier_routing_policy_ref must be a non-empty string or None"
            )
        if (budget is None) != (budget_policy_version_id is None):
            raise ValueError(
                "budget and budget_policy_version_id must be supplied together"
            )
        if budget_policy_version_id is not None and (
            not isinstance(budget_policy_version_id, str) or not budget_policy_version_id
        ):
            raise ValueError(
                "budget_policy_version_id must be a non-empty string or None"
            )
        slots = tuple(credential_slot_refs)
        if not slots or any(not isinstance(item, str) or not item for item in slots):
            raise ValueError("credential_slot_refs must be a non-empty string sequence")
        if len(set(slots)) != len(slots):
            raise ValueError("credential_slot_refs must be unique")
        if not callable(token_counter):
            raise TypeError("token_counter must be callable")
        if lease_seconds is not None and (
            isinstance(lease_seconds, bool)
            or not isinstance(lease_seconds, (int, float))
            or not math.isfinite(float(lease_seconds))
            or lease_seconds <= 0
        ):
            raise ValueError("lease_seconds must be a positive finite number")
        if fault_hook is not None and not callable(fault_hook):
            raise TypeError("fault_hook must be callable")
        self.scheduler = scheduler
        self.router = router
        self.adapter = adapter
        self.impact = impact
        self.store = impact.store
        self.observability = observability
        self.routing_policy_ref = routing_policy_ref
        self.verifier_routing_policy_ref = verifier_routing_policy_ref
        self.budget = budget
        self.budget_policy_version_id = budget_policy_version_id
        self.credential_slot_refs = slots
        self.token_counter = token_counter
        self.lease_seconds = (
            None if lease_seconds is None else float(lease_seconds)
        )
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.fault_hook = fault_hook

    @staticmethod
    def _work(value: WorkOrder | Mapping[str, Any]) -> WorkOrder:
        try:
            return value if isinstance(value, WorkOrder) else WorkOrder.from_dict(value)
        except Exception as exc:
            raise ThesisImpactModelWorkerConflict(
                "thesis-impact WorkOrder is invalid"
            ) from exc

    @staticmethod
    def _phase(work: WorkOrder) -> tuple[str, str]:
        phase = work.metadata.get("phase")
        if phase == "assessment":
            capability = "research"
            expected_inputs = 2
        elif phase == "verification":
            capability = "verify"
            expected_inputs = 3
        else:
            raise ThesisImpactModelWorkerRejected(
                "WorkOrder is not a thesis-impact assessment or verification"
            )
        if (
            work.metadata.get("control_plane") != "research-plan-thesis-impact"
            or work.requested_capabilities != (capability,)
            or len(work.input_refs) != expected_inputs
            or work.declared_side_effects
        ):
            raise ThesisImpactModelWorkerRejected(
                "WorkOrder phase, capability, inputs or side effects are not admitted"
            )
        return phase, capability

    def _phase_policy_ref(self, phase: str) -> str:
        """Return the immutable routing policy admitted for one phase.

        A verifier policy is only admitted when it phase-pins exactly one
        broker profile; a shared cost-sorted policy cannot prove which model
        verified an assessment, so it fails closed before any lease or broker
        call.
        """

        if phase != "verification" or self.verifier_routing_policy_ref is None:
            return self.routing_policy_ref
        try:
            policy = self.router.get_policy(self.verifier_routing_policy_ref)
        except RoutingPolicyNotFound as exc:
            raise ThesisImpactModelWorkerRejected(
                "verifier routing policy is not registered in the router authority"
            ) from exc
        allowed = policy.get("filters", {}).get("allowed_profile_ids")
        if not isinstance(allowed, list) or len(allowed) != 1:
            raise ThesisImpactModelWorkerRejected(
                "verifier routing policy is not pinned to exactly one profile"
            )
        return self.verifier_routing_policy_ref

    def _producer_family(self, work: WorkOrder, phase: str) -> str | None:
        if phase == "assessment":
            return None
        assessment = self.impact.assessment(work.input_refs[0])
        if (
            assessment["id"] != work.input_refs[0]
            or assessment["content_hash"] != work.metadata.get("assessment_hash")
            or assessment["claim_version_ref"] != work.input_refs[1]
            or assessment["claim_version_hash"]
            != work.metadata.get("claim_version_hash")
            or assessment["thesis_version_ref"] != work.input_refs[2]
            or assessment["thesis_version_hash"]
            != work.metadata.get("thesis_version_hash")
        ):
            raise ThesisImpactModelWorkerConflict(
                "verifier WorkOrder input refs drifted from its exact assessment"
            )
        invocation = self.impact.invocation(assessment["producer_invocation_ref"])
        return invocation["model_family"]

    def _budget_day(self) -> str:
        return self.clock().astimezone(timezone.utc).date().isoformat()

    def _budget_reserved_micros(self, work: WorkOrder) -> int:
        return int(
            (
                Decimal(str(work.budget["max_cost_usd"])) * Decimal(1_000_000)
            ).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
        )

    def _record_failure_alert(
        self,
        *,
        kind: str,
        work: WorkOrder,
        phase: str,
        attempt_number: int,
        route_ref: str | None,
        detail: Mapping[str, Any],
    ) -> None:
        if self.budget is None:
            return
        self.budget.record_alert(
            alert_id="thesis-impact-alert:"
            + content_hash({
                "work_order_ref": work.id,
                "attempt_number": attempt_number,
                "kind": kind,
            })[:32],
            kind=kind,
            severity="high" if kind == "day_budget_exceeded" else "medium",
            work_order_ref=work.id,
            phase=phase,
            detail={"attempt_number": attempt_number, "route_decision_ref": route_ref, **detail},
        )

    def _validate_output(
        self, work: WorkOrder, phase: str, result: ResultEnvelope
    ) -> None:
        if result.status != "succeeded":
            return
        if set(result.outputs) != {"text", "content_hash"}:
            raise ThesisImpactModelWorkerConflict(
                "successful model output has an invalid envelope shape"
            )
        text = result.outputs["text"]
        if (
            not isinstance(text, str)
            or not text
            or result.outputs["content_hash"]
            != hashlib.sha256(text.encode("utf-8")).hexdigest()
        ):
            raise ThesisImpactModelWorkerConflict(
                "successful model output text/hash binding is invalid"
            )
        try:
            raw = json.loads(
                text,
                object_pairs_hook=_reject_duplicate_keys,
                parse_constant=lambda value: (_ for _ in ()).throw(
                    ValueError(f"invalid JSON constant: {value}")
                ),
            )
        except (TypeError, ValueError) as exc:
            raise ThesisImpactModelWorkerConflict(
                "model output is not strict JSON"
            ) from exc
        if phase == "assessment":
            output = validate_thesis_impact_model_output(raw)
            thesis_view = self.store.get_version(work.input_refs[1])
            if thesis_view is None:
                raise ThesisImpactModelWorkerConflict(
                    "assessment ThesisVersion disappeared"
                )
            if (
                output["claim_version_ref"] != work.input_refs[0]
                or output["claim_version_hash"]
                != work.metadata.get("claim_version_hash")
                or output["thesis_version_ref"] != work.input_refs[1]
                or output["thesis_version_hash"]
                != work.metadata.get("thesis_version_hash")
                or output["driver_statement"]
                != thesis_view["content"]["mechanism"]
            ):
                raise ThesisImpactModelWorkerConflict(
                    "assessment output drifted from the exact Claim or Thesis"
                )
        else:
            required_schema_version = work.metadata.get(
                "verifier_output_schema_version", "0.1"
            )
            binding_mode = work.metadata.get("verifier_binding_mode")
            if binding_mode == VERIFIER_BINDING_MODE:
                if (
                    required_schema_version != VERIFIER_OUTPUT_SCHEMA_VERSION
                    or work.metadata.get("verifier_decision_schema_version")
                    != VERIFIER_DECISION_SCHEMA_VERSION
                ):
                    raise ThesisImpactModelWorkerConflict(
                        "verifier WorkOrder binding contract is unsupported"
                    )
                output = bind_thesis_impact_verifier_decision(
                    raw,
                    assessment_ref=work.input_refs[0],
                    assessment_hash=work.metadata.get("assessment_hash"),
                )
            elif binding_mode is None:
                output = validate_thesis_impact_verifier_output(
                    raw, required_schema_version=required_schema_version
                )
                if (
                    output["assessment_ref"] != work.input_refs[0]
                    or output["assessment_hash"]
                    != work.metadata.get("assessment_hash")
                ):
                    raise ThesisImpactModelWorkerConflict(
                        "verifier output drifted from the exact assessment"
                    )
            else:
                raise ThesisImpactModelWorkerConflict(
                    "verifier WorkOrder binding mode is unsupported"
                )
            if required_schema_version == VERIFIER_OUTPUT_SCHEMA_VERSION:
                assessment = self.impact.assessment(work.input_refs[0])
                thesis_view = self.store.get_version(work.input_refs[2])
                if thesis_view is None:
                    raise ThesisImpactModelWorkerConflict(
                        "verifier ThesisVersion disappeared"
                    )
                self._validate_verifier_authority_consistency(
                    output, assessment, thesis_view["content"]["mechanism"]
                )

    @staticmethod
    def _validate_verifier_authority_consistency(
        output: Mapping[str, Any],
        assessment: Mapping[str, Any],
        thesis_mechanism: str,
    ) -> None:
        """Reject findings contradicted by already-validated authority facts."""
        try:
            validate_thesis_impact_verifier_consistency(
                output, assessment, thesis_mechanism
            )
        except Exception as exc:
            raise ThesisImpactModelWorkerConflict(str(exc)) from exc

    @staticmethod
    def _route_estimate_micros(
        route: Mapping[str, Any], profile: Mapping[str, Any]
    ) -> int:
        selected = [
            item
            for item in route.get("candidate_snapshot", [])
            if isinstance(item, Mapping)
            and item.get("profile_version_ref") == profile["profile_version_ref"]
            and item.get("eligible") is True
        ]
        if len(selected) != 1:
            raise ThesisImpactModelWorkerConflict(
                "selected route has no exact cost estimate"
            )
        try:
            value = Decimal(str(selected[0]["estimated_cost_usd"]))
        except (KeyError, ValueError, ArithmeticError) as exc:
            raise ThesisImpactModelWorkerConflict(
                "selected route cost estimate is invalid"
            ) from exc
        if not value.is_finite() or value < 0:
            raise ThesisImpactModelWorkerConflict(
                "selected route cost estimate is invalid"
            )
        return int(
            (value * Decimal(1_000_000)).quantize(
                Decimal("1"), rounding=ROUND_HALF_UP
            )
        )

    def _account(
        self,
        invocation: ModelInvocation,
        route: Mapping[str, Any],
        profile: Mapping[str, Any],
    ) -> dict[str, Any]:
        usage = dict(invocation.usage)
        input_tokens = usage.get("input_tokens")
        output_tokens = usage.get("output_tokens")
        total_tokens = usage.get("total_tokens")
        if (
            all(
                isinstance(item, int) and not isinstance(item, bool)
                for item in (input_tokens, output_tokens, total_tokens)
            )
            and total_tokens != input_tokens + output_tokens
        ):
            total_tokens = None
        normalized = {
            "input_tokens": input_tokens if isinstance(input_tokens, int) else None,
            "output_tokens": output_tokens if isinstance(output_tokens, int) else None,
            "reasoning_tokens": None,
            "cache_read_tokens": (
                usage.get("cache_read_tokens")
                if isinstance(usage.get("cache_read_tokens"), int)
                else None
            ),
            "cache_write_tokens": (
                usage.get("cache_write_tokens")
                if isinstance(usage.get("cache_write_tokens"), int)
                else None
            ),
            "total_tokens": total_tokens if isinstance(total_tokens, int) else None,
            "requests": 1,
            "duration_ms": None,
            "input_bytes": None,
            "output_bytes": None,
        }
        usage_entry_id = "usage-entry:" + hashlib.sha256(
            invocation.id.encode("utf-8")
        ).hexdigest()[:32]
        usage_entry = self.observability.record_usage(
            invocation.id,
            entry_id=usage_entry_id,
            occurred_at=invocation.completed_at or invocation.created_at,
            metering_source=(
                "provider_reported"
                if normalized["input_tokens"] is not None
                or normalized["output_tokens"] is not None
                else "launcher_measured"
            ),
            measurement_status="partial",
            raw_usage=usage,
            workflow_ref=None,
            provider_usage_ref=invocation.parent_ref,
            correction_of_ref=None,
            actor_ref=ACTOR_REF,
            idempotency_key=f"usage:{invocation.id}",
            **normalized,
        )

        raw_cost = usage.get("raw_provider_telemetry", {}).get("cost", {})
        reported = (
            isinstance(raw_cost, Mapping)
            and raw_cost.get("available") is True
            and isinstance(raw_cost.get("usd"), (int, float))
            and not isinstance(raw_cost.get("usd"), bool)
            and math.isfinite(float(raw_cost["usd"]))
            and float(raw_cost["usd"]) >= 0
        )
        rates: list[dict[str, Any]] = []
        amount_micros: int
        cost_status: str
        calculation_ref: str
        if reported:
            amount_micros = int(
                (Decimal(str(raw_cost["usd"])) * Decimal(1_000_000)).quantize(
                    Decimal("1"), rounding=ROUND_HALF_UP
                )
            )
            charge_specs = [("request", 1, amount_micros)]
            cost_status = "actual"
            calculation_ref = "calculator:broker-reported-request-cost:0.1"
        elif (
            normalized["input_tokens"] is not None
            and normalized["output_tokens"] is not None
        ):
            charge_specs = []
            amount = Fraction(0, 1)
            for charge, metric, price_field in (
                ("input_tokens", normalized["input_tokens"], "input_per_million_usd"),
                ("output_tokens", normalized["output_tokens"], "output_per_million_usd"),
            ):
                unit_price = int(
                    (Decimal(str(profile["cost"][price_field])) * Decimal(1_000_000)).quantize(
                        Decimal("1"), rounding=ROUND_HALF_UP
                    )
                )
                charge_specs.append((charge, 1_000_000, unit_price))
                amount += Fraction(metric * unit_price, 1_000_000)
            amount_micros = (
                2 * amount.numerator + amount.denominator
            ) // (2 * amount.denominator)
            cost_status = "estimated"
            calculation_ref = "calculator:profile-token-rates:0.1"
        else:
            amount_micros = self._route_estimate_micros(route, profile)
            charge_specs = [("request", 1, amount_micros)]
            cost_status = "estimated"
            calculation_ref = "calculator:model-route-estimate:0.1"

        for charge, quantity, unit_price in charge_specs:
            identity = {
                "invocation_ref": invocation.id,
                "profile_version_ref": profile["profile_version_ref"],
                "charge_type": charge,
                "unit_quantity": quantity,
                "unit_price_micros": unit_price,
            }
            digest = content_hash(identity)[:32]
            rates.append(self.observability.create_price_rate_version(
                f"price-rate:thesis-impact:{digest}",
                provider=profile["provider"],
                model=profile["model"],
                charge_type=charge,
                unit_quantity=quantity,
                unit_price_micros=unit_price,
                currency="USD",
                effective_from=invocation.created_at,
                effective_until=None,
                source_ref=(
                    invocation.parent_ref
                    if charge == "request"
                    else profile["profile_version_ref"]
                ),
                actor_ref=ACTOR_REF,
                prior_version_ref=None,
                version_id=f"price-rate-version:{digest}",
                idempotency_key=f"price-rate:thesis-impact:{digest}",
            ))
        cost_entry_id = "cost-entry:" + hashlib.sha256(
            usage_entry_id.encode("utf-8")
        ).hexdigest()[:32]
        cost = self.observability.record_cost(
            usage_entry_id,
            price_rate_refs=[item["id"] for item in rates],
            amount_micros=amount_micros,
            currency="USD",
            cost_status=cost_status,
            calculation_ref=calculation_ref,
            correction_of_ref=None,
            cost_entry_id=cost_entry_id,
            actor_ref=ACTOR_REF,
            idempotency_key=f"cost:{usage_entry_id}",
        )
        return {"usage": usage_entry, "cost": cost}

    def _control_result(
        self,
        work: WorkOrder,
        attempt_number: int,
        *,
        code: str,
        status: str,
        invocation_ref: str | None = None,
        usage_refs: Sequence[str] = (),
        created_at: str | None = None,
        route_ref: str | None = None,
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
            id="result:thesis-impact-control-" + content_hash(identity)[:32],
            created_at=created_at or _utc(self.clock()),
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

    @staticmethod
    def _bounded_failure_status(lease: Mapping[str, Any]) -> str:
        """Retry only while Scheduler can create another durable attempt."""

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
            raise ThesisImpactModelWorkerConflict(
                "Scheduler lease has invalid retry bounds"
            )
        return "failed" if attempt >= maximum else "retryable"

    def _accepted_attempts(self, work_order_id: str) -> set[int]:
        return {
            event["attempt_number"]
            for event in self.scheduler.attempt_history(work_order_id)
            if event.get("result_envelope_id") is not None
        }

    @staticmethod
    def _reuse_invocation(
        saved: Mapping[str, Any], replayed: ModelInvocation
    ) -> ModelInvocation:
        """Bind a broker replay to the invocation committed before the crash."""

        replayed_wire = replayed.to_dict()
        stable_fields = {
            "schema_version",
            "id",
            "work_order_ref",
            "profile_ref",
            "granularity",
            "capability",
            "provider",
            "model",
            "model_family",
            "input_refs",
            "output_refs",
            "side_effects",
            "runtime_ref",
            "actor_ref",
            "parent_ref",
            "environment_hash",
        }
        if any(saved.get(field) != replayed_wire.get(field) for field in stable_fields):
            raise ThesisImpactModelWorkerConflict(
                "replayed broker result differs from the committed ModelInvocation"
            )
        try:
            return ModelInvocation.from_dict(saved)
        except Exception as exc:
            raise ThesisImpactModelWorkerConflict(
                "committed ModelInvocation is invalid during recovery"
            ) from exc

    def run_once(
        self, work_order: WorkOrder | Mapping[str, Any]
    ) -> dict[str, Any]:
        """Run or replay one exact thesis-impact model WorkOrder."""

        work = self._work(work_order)
        phase, capability = self._phase(work)
        phase_policy_ref = self._phase_policy_ref(phase)
        status = self.scheduler.status(work.id)
        if status["work_order_hash"] != content_hash(work.to_dict()):
            raise ThesisImpactModelWorkerConflict(
                "Scheduler WorkOrder differs from the supplied exact WorkOrder"
            )
        formal = self.scheduler.formal_result(work.id)
        if formal is not None:
            return {
                "status": formal["terminal_state"],
                "phase": phase,
                "work_order_ref": work.id,
                "formal_result": formal,
                "invocation_ref": formal["result_envelope"]["invocation_ref"],
                "replayed": True,
            }
        lease = self.scheduler.claim(
            WORKER_REF,
            work_order_id=work.id,
            lease_seconds=self.lease_seconds,
        )
        if lease is None:
            return {
                "status": "waiting",
                "phase": phase,
                "work_order_ref": work.id,
            }
        if (
            canonical_json(lease["work_order"]) != canonical_json(work.to_dict())
            or lease["work_order_hash"] != content_hash(work.to_dict())
        ):
            raise ThesisImpactModelWorkerConflict(
                "Scheduler lease does not retain the exact WorkOrder"
            )
        attempt_number = lease["attempt"]["attempt_number"]
        producer_family = self._producer_family(work, phase)
        previous = self.router.list_decisions(work_order_id=work.id)
        accepted_attempts = self._accepted_attempts(work.id)
        recovery_route = (
            previous[-1]
            if previous and previous[-1]["attempt_number"] not in accepted_attempts
            else None
        )
        route_replayed = recovery_route is not None
        if recovery_route is not None:
            routed = recovery_route
        else:
            decision_kind = "initial" if not previous else "retry"
            previous_ref = None if not previous else previous[-1]["id"]
            estimated_input = self.token_counter(work.question)
            estimated_output = int(work.budget["max_output_tokens"])
            routed = self.router.route(
                work,
                attempt_number=attempt_number,
                capability=capability,
                policy_version_ref=phase_policy_ref,
                credential_slot_refs=self.credential_slot_refs,
                required_modalities=("text",),
                required_context_tokens=estimated_input + estimated_output,
                estimated_input_tokens=estimated_input,
                estimated_output_tokens=estimated_output,
                decision_kind=decision_kind,
                previous_decision_ref=previous_ref,
                producer_family=producer_family,
                idempotency_key=(
                    f"thesis-impact-route:{work.id}:{attempt_number}"
                ),
            )["decision"]
        if routed["outcome"] != "selected":
            self._record_failure_alert(
                kind="work_order_failed",
                work=work,
                phase=phase,
                attempt_number=attempt_number,
                route_ref=routed["id"],
                detail={"code": "MODEL_ROUTE_REJECTED"},
            )
            result = self._control_result(
                work,
                attempt_number,
                code="MODEL_ROUTE_REJECTED",
                status="failed",
                route_ref=routed["id"],
            )
            completed = self.scheduler.complete(
                work.id,
                attempt_number,
                WORKER_REF,
                lease["lease_token"],
                result,
                idempotency_key=f"thesis-impact-complete:{work.id}:{attempt_number}",
            )
            return {
                "status": "failed",
                "phase": phase,
                "route": routed,
                "completion": completed,
            }
        profile = self.router.get_profile(routed["selected_profile_version_ref"])
        admission = None
        if self.budget is not None:
            try:
                admission = self.budget.admit(
                    policy_version_id=self.budget_policy_version_id,
                    day=self._budget_day(),
                    work_order_ref=work.id,
                    attempt_number=attempt_number,
                    phase=phase,
                    route_decision_ref=routed["id"],
                    reserved_micros=self._budget_reserved_micros(work),
                )
            except ThesisImpactDayBudgetExceeded as exc:
                rejection = exc.rejection
                self._record_failure_alert(
                    kind="day_budget_exceeded",
                    work=work,
                    phase=phase,
                    attempt_number=attempt_number,
                    route_ref=routed["id"],
                    detail={
                        "day": rejection["day"],
                        "day_committed_micros": rejection["day_committed_micros"],
                        "reserved_micros": rejection["reserved_micros"],
                        "day_cap_micros": rejection["day_cap_micros"],
                    },
                )
                result = self._control_result(
                    work,
                    attempt_number,
                    code="DAY_BUDGET_EXCEEDED",
                    status="failed",
                    route_ref=routed["id"],
                )
                completed = self.scheduler.complete(
                    work.id,
                    attempt_number,
                    WORKER_REF,
                    lease["lease_token"],
                    result,
                    idempotency_key=f"thesis-impact-complete:{work.id}:{attempt_number}",
                )
                return {
                    "status": "failed",
                    "phase": phase,
                    "work_order_ref": work.id,
                    "route": routed,
                    "completion": completed,
                    "budget_rejection": rejection,
                }
        try:
            if route_replayed:
                invocation, adapter_result = self.adapter.replay(work, routed, profile)
            else:
                invocation, adapter_result = self.adapter.execute(work, routed, profile)
        except OpenClawModelAdapterError as exc:
            retryable = isinstance(exc, BrokerConnectionError)
            failure_status = (
                self._bounded_failure_status(lease) if retryable else "failed"
            )
            if failure_status == "failed":
                self._record_failure_alert(
                    kind="work_order_failed",
                    work=work,
                    phase=phase,
                    attempt_number=attempt_number,
                    route_ref=routed["id"],
                    detail={
                        "code": (
                            "MODEL_ADAPTER_UNAVAILABLE"
                            if retryable
                            else "MODEL_ADAPTER_REJECTED"
                        ),
                        "error_type": type(exc).__name__,
                    },
                )
            result = self._control_result(
                work,
                attempt_number,
                code="MODEL_ADAPTER_UNAVAILABLE" if retryable else "MODEL_ADAPTER_REJECTED",
                status=failure_status,
                route_ref=routed["id"],
            )
            completed = self.scheduler.complete(
                work.id,
                attempt_number,
                WORKER_REF,
                lease["lease_token"],
                result,
                idempotency_key=f"thesis-impact-complete:{work.id}:{attempt_number}",
            )
            return {
                "status": (
                    "retryable"
                    if result.status == "retryable" and completed["work_state"] == "ready"
                    else "failed"
                ),
                "phase": phase,
                "route": routed,
                "completion": completed,
                "error_type": type(exc).__name__,
            }

        if (
            route_replayed
            and adapter_result.status == "failed"
            and adapter_result.error is not None
            and adapter_result.error.get("code") == "IDEMPOTENCY_MISS"
        ):
            self._record_failure_alert(
                kind="work_order_failed",
                work=work,
                phase=phase,
                attempt_number=attempt_number,
                route_ref=routed["id"],
                detail={"code": "MODEL_RECOVERY_MISS"},
            )
            result = self._control_result(
                work,
                attempt_number,
                code="MODEL_RECOVERY_MISS",
                status="failed",
                created_at=adapter_result.created_at,
                route_ref=routed["id"],
            )
            completed = self.scheduler.complete(
                work.id,
                attempt_number,
                WORKER_REF,
                lease["lease_token"],
                result,
                idempotency_key=f"thesis-impact-complete:{work.id}:{attempt_number}",
            )
            return {
                "status": "failed",
                "phase": phase,
                "work_order_ref": work.id,
                "route": routed,
                "result": result.to_dict(),
                "completion": completed,
                "route_replayed": True,
                "replayed": False,
            }

        saved_invocation = self.impact.find_invocation(invocation.id)
        if saved_invocation is None:
            self.store.register_invocation(invocation.to_dict())
        else:
            invocation = self._reuse_invocation(saved_invocation, invocation)
        accounting = self._account(invocation, routed, profile)
        if admission is not None:
            self.budget.settle(
                admission["admission_id"],
                actual_micros=accounting["cost"]["amount_micros"],
                usage_entry_ref=accounting["usage"]["id"],
            )
        if self.fault_hook is not None:
            self.fault_hook("after_model_accounting")
        result = adapter_result
        if result.status == "succeeded":
            try:
                self._validate_output(work, phase, result)
            except Exception as exc:
                result = self._control_result(
                    work,
                    attempt_number,
                    code="MODEL_OUTPUT_CONTRACT_REJECTED",
                    status=self._bounded_failure_status(lease),
                    invocation_ref=invocation.id,
                    usage_refs=adapter_result.usage_refs,
                    created_at=adapter_result.created_at,
                    route_ref=routed["id"],
                )
                output_error = type(exc).__name__
            else:
                output_error = None
        else:
            output_error = None
        completed = self.scheduler.complete(
            work.id,
            attempt_number,
            WORKER_REF,
            lease["lease_token"],
            result,
            idempotency_key=f"thesis-impact-complete:{work.id}:{attempt_number}",
        )
        if result.status == "succeeded":
            normalized_status = "succeeded"
        elif result.status == "retryable" and completed["work_state"] == "ready":
            normalized_status = "retryable"
        else:
            normalized_status = "failed"
            if output_error is not None:
                self._record_failure_alert(
                    kind="work_order_failed",
                    work=work,
                    phase=phase,
                    attempt_number=attempt_number,
                    route_ref=routed["id"],
                    detail={
                        "code": "MODEL_OUTPUT_CONTRACT_REJECTED",
                        "error_type": output_error,
                    },
                )
        return {
            "status": normalized_status,
            "phase": phase,
            "work_order_ref": work.id,
            "route": routed,
            "profile": profile,
            "invocation": invocation.to_dict(),
            "result": result.to_dict(),
            "accounting": accounting,
            "completion": completed,
            "output_error": output_error,
            "route_replayed": route_replayed,
            "replayed": False,
        }


class ResearchPlanThesisImpactRuntime:
    """Advance a closed plan through both bounded model WorkOrders."""

    def __init__(
        self,
        *,
        control: ResearchPlanThesisImpactCoordinator,
        worker: ThesisImpactModelWorker,
    ) -> None:
        if control.impact is not worker.impact:
            raise TypeError("runtime control and worker must share impact authority")
        self.control = control
        self.worker = worker

    def run_once(
        self, *, plan_version_ref: str, thesis_ref: str
    ) -> dict[str, Any]:
        started = self.control.start_from_closed_plan(
            plan_version_ref=plan_version_ref, thesis_ref=thesis_ref
        )
        if started["status"] == "follow_up_recorded":
            return {"status": "follow_up_recorded", "start": started}
        assessment_run = self.worker.run_once(started["assessment_work_order"])
        if assessment_run["status"] != "succeeded":
            return {
                "status": f"assessment_{assessment_run['status']}",
                "start": started,
                "assessment_run": assessment_run,
            }
        assessed = self.control.advance_assessment(
            plan_version_ref=plan_version_ref,
            thesis_ref=thesis_ref,
        )
        assessment = assessed["assessment"]["assessment"]
        verifier_run = self.worker.run_once(assessed["verifier_work_order"])
        if verifier_run["status"] != "succeeded":
            return {
                "status": f"verification_{verifier_run['status']}",
                "start": started,
                "assessment_run": assessment_run,
                "assessment": assessed,
                "verifier_run": verifier_run,
            }
        final = self.control.advance_verification(
            plan_version_ref=plan_version_ref,
            thesis_ref=thesis_ref,
            assessment_ref=assessment["id"],
        )
        return {
            "status": final["status"],
            "start": started,
            "assessment_run": assessment_run,
            "assessment": assessed,
            "verifier_run": verifier_run,
            "final": final,
        }


__all__ = [
    "ResearchPlanThesisImpactRuntime",
    "ThesisImpactModelWorker",
    "ThesisImpactModelWorkerConflict",
    "ThesisImpactModelWorkerError",
    "ThesisImpactModelWorkerRejected",
]
