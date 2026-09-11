"""One bounded model call for the cockpit (P9d-18, ADR-0006).

The cockpit answers ad-hoc questions and drafts goal or steering proposals
with the same model the extraction lane uses, under the same authorities:

- the route comes from the model router under the extraction routing policy;
- the spend is admitted in the shared day-budget ledger against the active
  mission's daily caps (paid calls and cost), exactly as an extraction window
  is, so a question costs the mission a paid call and the ledger fails closed
  when the day is exhausted;
- the WorkOrder is enqueued, leased and completed in the scheduler, so a
  repeated request replays the persisted result instead of paying twice.

Nothing here writes to the Core: the answer is a cockpit artifact, never a
Claim.  The control process owns no Core write handle and gets none.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import re
from importlib import resources
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .contracts import ResultEnvelope, WorkOrder
from .call_budget import budget_fingerprint, resolve_call_budget
from .document_extraction import validate_model_config
from .model_accounting import ModelAccountingError, _route_estimate_micros
from .model_router import ModelRouter, RoutingPolicyNotFound, independent_families
from .openclaw_model_adapter import (
    BrokerDefinitelyNotSent,
    OpenClawModelAdapter,
    OpenClawModelAdapterError,
)
from .scheduler import Scheduler, SchedulerConflict
from .store import canonical_json, content_hash
from .budget_pools import POOL_EXHAUSTED_STATUS, mission_pool_scope
from .thesis_impact_budget import ThesisImpactBudgetError, ThesisImpactBudgetStore

WORKER_REF = "worker:cockpit-model:0.1"
SCHEMA_VERSION = "0.1"
# "draft" is P10c: one section of a mission deliverable, drafted by the lane
# rather than by the owner, under the same route, budget and replay.
# P13n: "plan" is the research planner deciding what to work on next. It is a
# cockpit-shaped call -- one bounded, budgeted, replayable model call against
# the mission -- but it is not the cockpit answering the owner, so it is named
# rather than folded into "ask".
# P13al: "model_spec" is deciding how one company should be modelled -- what
# drives its revenue, how its costs behave, which statements matter, which
# operating metrics the market watches. Named rather than folded into "plan"
# because it is a judgement about a company, not about this system's own work,
# and the two are routed and budgeted separately.
# P14-0: the seed. A lane with its own bounded, budgeted, replayable model
# call registers its purpose from its own module rather than editing this set,
# which is the whole of what a new lane used to have to do here.
_PURPOSE_RE = re.compile(r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$")
_SEED_PURPOSES = ("ask", "goal", "steer", "draft", "plan", "model_spec")
# Read through purposes(), never as a module constant: a name rebound on every
# registration is a snapshot waiting to go stale in whoever imported it first.
_PURPOSES: set[str] = set(_SEED_PURPOSES)

# Provider-enforced output contracts for independent Cockpit verifiers.  The
# adapter resolves these opaque allowlisted refs to packaged schemas; callers
# can never supply a filesystem path or arbitrary JSON schema.
_VERIFIER_PROVIDER_CONTRACTS = {
    "dossier_verifier": (
        "dossier-verifier-provider-output-0.1",
        "dossier-verifier-provider-output-v0.1.schema.json"),
    "industry_framework_verifier": (
        "dossier-verifier-provider-output-0.1",
        "dossier-verifier-provider-output-v0.1.schema.json"),
    "deep_insight_gate_verifier": (
        "deep-insight-gate-verifier-provider-output-0.1",
        "deep-insight-gate-verifier-provider-output-v0.1.schema.json"),
    "zero_base_review_verifier": (
        "zero-base-review-verifier-provider-output-0.1",
        "zero-base-review-verifier-provider-output-v0.1.schema.json"),
    "earnings_preview_verifier": (
        "earnings-verifier-provider-output-0.1",
        "earnings-verifier-provider-output-v0.1.schema.json"),
    "earnings_calibration_verifier": (
        "earnings-verifier-provider-output-0.1",
        "earnings-verifier-provider-output-v0.1.schema.json"),
    "quality_verifier": (
        "quality-verifier-provider-output-0.1",
        "quality-verifier-provider-output-v0.1.schema.json"),
    "investment_memo_verifier": (
        "investment-memo-verifier-provider-output-0.1",
        "investment-memo-verifier-provider-output-v0.1.schema.json"),
    "event_judgement_verifier": (
        "event-judgement-verifier-provider-output-0.1",
        "event-judgement-verifier-provider-output-v0.1.schema.json"),
    "thesis_reflection_verifier": (
        "event-judgement-verifier-provider-output-0.1",
        "event-judgement-verifier-provider-output-v0.1.schema.json"),
    "debate_map_verifier": (
        "debate-map-verifier-provider-output-0.1",
        "debate-map-verifier-provider-output-v0.1.schema.json"),
    "conviction_call_verifier": (
        "conviction-call-verifier-provider-output-0.1",
        "conviction-call-verifier-provider-output-v0.1.schema.json"),
}


def _verifier_provider_contract(purpose: str) -> tuple[str, str] | None:
    selected = _VERIFIER_PROVIDER_CONTRACTS.get(purpose)
    if selected is None:
        return None
    ref, resource = selected
    schema = json.loads(resources.files("dalton_core").joinpath(resource).read_text("utf-8"))
    return ref, content_hash(schema)


def verifier_provider_contract_fingerprint(*purposes: str) -> str:
    """Hash the packaged verifier contracts that make a failed batch retryable."""
    return content_hash({purpose: _verifier_provider_contract(purpose)
                         for purpose in purposes})

# Room for the completion write after the model answers, so a call that
# finishes right on its timeout still has a live lease to complete against.
_LEASE_GRACE_SECONDS = 30.0
# P13aa: which definition of "the same request" produced this identity.
#
# Version 1 hashed a wall-clock created_at into the WorkOrder wire while
# leaving it out of the id, so the same question asked twice reused one
# idempotency key with two different hashes -- a permanent conflict, and every
# Initial Screen section sat in it for two days. Fixing the definition is not
# enough on its own: the keys written under the old definition still hold the
# old hash, so a corrected request collides with its own history. A changed
# identity definition is a changed identity, and it is versioned like every
# other frozen definition here rather than quietly reusing the old keys.
IDENTITY_VERSION = 2
DOSSIER_REQUEST_IDENTITY_VERSION = "0.1"
_DOSSIER_PURPOSES = frozenset({"dossier", "dossier_verifier"})


class CockpitModelError(RuntimeError):
    """The call was refused or failed; the message is safe to show."""

    def __init__(self, message: str, *, failure_trace: Mapping[str, Any] | None = None) -> None:
        super().__init__(message)
        self.failure_trace = None if failure_trace is None else dict(failure_trace)


class CockpitModelRouteUnavailable(CockpitModelError):
    """No route was selected, so no model content was produced or refused."""

    lane_status = "model_unavailable"


def model_failure_trace(exc: BaseException) -> dict[str, Any] | None:
    """Return the bounded immutable-work binding carried by a model failure."""

    trace = getattr(exc, "failure_trace", None)
    if not isinstance(trace, Mapping) or set(trace) != {
        "schema_version", "purpose", "base_request_id", "work_order_ref",
        "work_request_id", "work_order_hash", "formal_result_envelope_hash",
    }:
        return None
    copied = dict(trace)
    if (
        copied["schema_version"] != "0.1"
        or not isinstance(copied["purpose"], str)
        or copied["purpose"] not in _PURPOSES
        or not isinstance(copied["base_request_id"], str)
        or not 1 <= len(copied["base_request_id"]) <= 512
        or not isinstance(copied["work_request_id"], str)
        or not 1 <= len(copied["work_request_id"]) <= 600
        or re.fullmatch(
            r"work:cockpit-[a-z0-9_]+-[0-9a-f]{32}",
            str(copied["work_order_ref"]),
        ) is None
        or re.fullmatch(r"[0-9a-f]{64}", str(copied["work_order_hash"])) is None
        or re.fullmatch(
            r"[0-9a-f]{64}", str(copied["formal_result_envelope_hash"])
        ) is None
    ):
        return None
    return copied


def _failed_work_trace(scheduler: Any, work: WorkOrder, *, purpose: str,
                       request_id: str) -> dict[str, Any] | None:
    """Bind a surfaced failure to Scheduler's immutable WorkOrder and result."""

    row = scheduler.connection.execute(
        "SELECT * FROM scheduler_formal_results WHERE work_order_id=?", (work.id,)
    ).fetchone()
    if row is None:
        return None
    try:
        formal = dict(row)
        envelope_wire = json.loads(formal["result_envelope_json"])
        if not isinstance(envelope_wire, Mapping):
            return None
        envelope_hash = content_hash(envelope_wire)
        formal_wire = {
            "id": formal["result_record_id"],
            "work_order_id": formal["work_order_id"],
            "attempt_number": formal["attempt_number"],
            "result_envelope_id": formal["result_envelope_id"],
            "result_envelope_hash": formal["result_envelope_hash"],
            "terminal_state": formal["terminal_state"],
            "created_at": formal["created_at"],
        }
        authority = scheduler.work_order_authority(work.id)
    except (KeyError, TypeError, ValueError, SchedulerConflict):
        return None
    if (
        authority is None
        or formal["terminal_state"] != "failed"
        or formal["work_order_id"] != work.id
        or envelope_wire.get("work_order_ref") != work.id
        or envelope_wire.get("id") != formal["result_envelope_id"]
        or envelope_wire.get("status") != "failed"
        or canonical_json(envelope_wire) != formal["result_envelope_json"]
        or envelope_hash != formal["result_envelope_hash"]
        or content_hash(formal_wire) != formal["content_hash"]
    ):
        return None
    metadata = authority["work_order"].get("metadata") or {}
    work_request_id = metadata.get("request_id")
    if (
        metadata.get("control_plane") != "cockpit"
        or metadata.get("purpose") != purpose
        or not isinstance(work_request_id, str)
        or not (
            work_request_id == request_id
            or re.fullmatch(
                re.escape(request_id) + r":producer:[0-9a-f]{16}", work_request_id
            ) is not None
        )
    ):
        return None
    return {
        "schema_version": "0.1",
        "purpose": purpose,
        "base_request_id": request_id,
        "work_request_id": work_request_id,
        "work_order_ref": work.id,
        "work_order_hash": authority["work_order_hash"],
        "formal_result_envelope_hash": envelope_hash,
    }


def independent_model_call(model: Any, *, producer_route_decision_refs: Sequence[str],
                           **kwargs: Any) -> dict[str, Any]:
    """Call a verifier with producer routes, preserving legacy test doubles."""
    supplied = tuple(producer_route_decision_refs)
    refs = tuple(ref for ref in supplied if isinstance(ref, str) and ref)
    if not refs or len(refs) != len(supplied):
        raise CockpitModelError(
            "independent verification requires every producer route decision")
    if "producer_route_decision_refs" in inspect.signature(model.call).parameters:
        return model.call(producer_route_decision_refs=refs, **kwargs)
    return model.call(**kwargs)


class CockpitModelPoolExhausted(CockpitModelError):
    """C2: this purpose's capacity pool is spent for today.

    A separate class because it is a different kind of "no": the day cap being
    exhausted is the mission out of money, while a spent pool is this kind of
    work having had its share -- another pool may still be running, and the
    lane's honest word for it is ``skipped:pool_exhausted`` rather than a
    failure.  ``lane_status`` is that word, so a caller reports the pool
    without having to know how the sentence was spelled.
    """

    lane_status = POOL_EXHAUSTED_STATUS

    def __init__(self, message: str, rejection: Mapping[str, Any], *,
                 failure_trace: Mapping[str, Any] | None = None) -> None:
        super().__init__(message, failure_trace=failure_trace)
        self.rejection = dict(rejection)
        self.pool = self.rejection.get("pool")
        self.spent = self.rejection.get("spent")
        self.cap = self.rejection.get("cap")


def lane_status_for(exc: BaseException, fallback: str) -> str:
    """The word a lane should report for a call the cockpit refused.

    Every caller used to flatten every refusal to one word -- usually
    ``model_unavailable`` -- and a spent pool is not an outage. The difference
    matters to two readers: the tick ledger, whose ``pool_exhausted`` column
    is how a week's worth of budget decisions is told apart from a week's
    worth of broken routes, and the owner, for whom "no model route" and "this
    kind of work has had its share of today" call for opposite actions.
    """

    if isinstance(exc, (CockpitModelPoolExhausted, CockpitModelRouteUnavailable)):
        return exc.lane_status
    return fallback


def _pool_refusal_message(rejection: Mapping[str, Any]) -> str:
    return (
        f"{POOL_EXHAUSTED_STATUS}: the {rejection.get('pool')} pool is spent "
        f"for {rejection.get('day')} ({rejection.get('spent')} of "
        f"{rejection.get('cap')} micros)"
    )


def _raise_failure(message: str, rejection: Mapping[str, Any] | None, *,
                   failure_trace: Mapping[str, Any] | None = None) -> None:
    """Raise the refusal in the shape that says which kind of no it was."""

    if rejection is not None:
        raise CockpitModelPoolExhausted(message, rejection, failure_trace=failure_trace)
    if message == "no model route is available right now":
        raise CockpitModelRouteUnavailable(message, failure_trace=failure_trace)
    raise CockpitModelError(message, failure_trace=failure_trace)


def pool_refusal_message(rejection: Mapping[str, Any]) -> str:
    """The sentence a spent pool is reported as, wherever it is refused."""

    return _pool_refusal_message(rejection)


def admit_day_ledger(
    budget: Any,
    *,
    policy_version_id: str,
    day: str,
    work_order_ref: str,
    attempt_number: int,
    phase: str,
    route_decision_ref: str,
    reserved_micros: int,
    mission_binding: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """One reservation in the shared day ledger, with C2's pool as the gate.

    C2b extracted this from :meth:`CockpitModel.call` because a second kind of
    paid call -- the Tier-1 bounded planner loop, which runs inside the writer
    rather than in the cockpit -- has to be admitted by exactly the same rules
    against exactly the same ledger.  Two copies of "admit, then read the three
    ways it can say no" would have drifted, and the way they drift is that one
    of them stops checking the pool.

    Returns one of three shapes, and never raises for a budget answer:

    ``{"status": "admitted", "admission": {...}}``
        the reservation is open and its ``admission_id`` must be settled.
    ``{"status": "pool_exhausted", "rejection": {...}, "failure": str}``
        this kind of work has had its share of the day.  A decision, not a
        fault: the pool refills at midnight and the identity is not poisoned.
    ``{"status": "refused", "failure": str}``
        the day cap itself refused, which is the mission out of money.
    """

    try:
        admission = budget.admit(
            policy_version_id=policy_version_id,
            day=day,
            work_order_ref=work_order_ref,
            attempt_number=attempt_number,
            phase=phase,
            route_decision_ref=route_decision_ref,
            reserved_micros=reserved_micros,
            mission_binding=mission_binding,
        )
    except ThesisImpactBudgetError as exc:
        return {
            "status": "refused",
            "failure": f"today's research budget refused the call: {exc}",
        }
    if admission.get("status") == "rejected":
        return {
            "status": "pool_exhausted",
            "rejection": dict(admission),
            "failure": _pool_refusal_message(admission),
        }
    return {"status": "admitted", "admission": admission}


def settle_day_ledger(budget: Any, admission: Mapping[str, Any], *,
                      actual_micros: int) -> dict[str, Any]:
    """Bind an open reservation to what the link that served it actually cost.

    The pool is not passed: :meth:`ThesisImpactBudgetStore.settle` copies it
    from the admission row inside the same transaction, which is what makes
    "the pool and the ledger cannot disagree" a property of the schema.
    """

    return budget.settle(admission["admission_id"], actual_micros=actual_micros)

def register_purpose(name: str) -> str:
    """Name one more thing a cockpit-shaped model call may be for.

    A purpose is not a label: it is what the WorkOrder is identified by and
    what the day ledger accounts against, so it is a closed vocabulary and a
    call with an unregistered purpose is refused. Registering the same purpose
    twice is a no-op; a purpose that is not a plain lowercase identifier is
    refused, because it ends up in a WorkOrder id.
    """

    if not isinstance(name, str) or not _PURPOSE_RE.fullmatch(name):
        raise CockpitModelError("a model purpose is lowercase words joined by _")
    _PURPOSES.add(name)
    return name


def purposes() -> frozenset[str]:
    """Every registered purpose, read at call time."""

    return frozenset(_PURPOSES)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def build_work(*, purpose: str, request_id: str, prompt: str, mission_version_ref: str,
               max_input_tokens: int, max_output_tokens: int, max_cost_usd: float, max_seconds: int,
               budget_identity: str | None = None,
               created_at: str | None = None,
               verifier_provider_contract: str | None = None,
               verifier_provider_schema_hash: str | None = None,
               mission_version_hash: str | None = None,
               request_identity: Mapping[str, Any] | None = None,
               producer_route_decision_refs: Sequence[str] = ()) -> WorkOrder:
    if purpose not in _PURPOSES:
        raise CockpitModelError("unknown cockpit model purpose")
    if len(prompt.encode("utf-8")) > max_input_tokens:
        raise CockpitModelError("the question and its context exceed the model input bound")
    identity = {"identity_version": IDENTITY_VERSION,
                "purpose": purpose, "request_id": request_id,
                "mission_version_ref": mission_version_ref,
                "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest()}
    if budget_identity is not None:
        identity["budget_fingerprint"] = budget_identity
    if verifier_provider_contract is not None:
        identity["verifier_provider_contract"] = verifier_provider_contract
        identity["verifier_provider_schema_hash"] = verifier_provider_schema_hash
        # Provider-control eligibility became a route-time requirement after
        # older verifier work had already been persisted with plain
        # ``research`` capability. Give the corrected admission contract a new
        # identity so it cannot conflict with or replay that old work.
        identity["provider_control_capability"] = "provider-controlled-verify"
    if mission_version_hash is not None:
        identity["mission_version_hash"] = mission_version_hash
    if producer_route_decision_refs:
        identity["producer_route_decision_refs"] = list(producer_route_decision_refs)
    if request_identity is not None:
        identity["request_identity_hash"] = content_hash(request_identity)
    digest = content_hash(identity)
    at = created_at or _now()
    capability = (
        "provider-controlled-verify"
        if verifier_provider_contract is not None
        else "research"
    )
    return WorkOrder(
        schema_version=SCHEMA_VERSION, id=f"work:cockpit-{purpose}-{digest[:32]}",
        created_at=at, updated_at=at, question=prompt,
        requested_capabilities=(capability,),
        runtime_profile_ref="runtime-profile:dalton-model-broker:0.1",
        budget={"max_input_tokens": max_input_tokens, "max_output_tokens": max_output_tokens,
                "max_total_tokens": max_input_tokens + max_output_tokens,
                "max_cost_usd": max_cost_usd, "max_seconds": max_seconds},
        idempotency_key=f"cockpit:{purpose}:{digest}", declared_side_effects=(), status="ready",
        input_refs=(), metadata={"control_plane": "cockpit", "purpose": purpose, "request_id": request_id,
                                     "mission_version_ref": mission_version_ref,
                                 **({} if mission_version_hash is None else {
                                     "mission_version_hash": mission_version_hash}),
                                 **({} if not producer_route_decision_refs else {
                                     "producer_route_decision_refs": list(producer_route_decision_refs)}),
                                 **({} if request_identity is None else {
                                     "request_identity": dict(request_identity)}),
                                 **({} if verifier_provider_contract is None else {
                                     "verifier_output_schema_version": "0.1",
                                     "verifier_provider_contract": verifier_provider_contract,
                                     "verifier_provider_schema_hash": verifier_provider_schema_hash,
                                 })},
    )


def _legacy_decorated_request_id(
    request_id: str,
    config: Mapping[str, Any],
    producer_refs: Sequence[str],
) -> str:
    """Rebuild the pre-binding identity solely for exact successful replay."""

    decorated = request_id
    if "capacity_retry" in config:
        decorated += ":capacity-policy:" + content_hash(
            _capacity_retry(config)
        )[:16]
    if "transport_retry" in config:
        decorated += ":transport-policy:" + content_hash(
            config["transport_retry"]
        )[:16]
    if producer_refs:
        decorated += ":producer:" + content_hash(list(producer_refs))[:16]
    return decorated


def dossier_request_identity(
    *,
    semantic_request_id: str,
    config: Mapping[str, Any],
    producer_route_decision_refs: Sequence[str] = (),
    recovery_parent: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Bind one Dossier request to exact execution config and recovery authority."""

    producer_refs = tuple(sorted({str(ref) for ref in producer_route_decision_refs}))
    exact_config = {
        **(
            {"capacity_retry": dict(config["capacity_retry"])}
            if "capacity_retry" in config
            else {}
        ),
        **(
            {"transport_retry": dict(config["transport_retry"])}
            if "transport_retry" in config
            else {}
        ),
    }
    body = {
        "schema_version": DOSSIER_REQUEST_IDENTITY_VERSION,
        "semantic_request_id": semantic_request_id,
        "exact_config": exact_config,
        "recovery_parent": None if recovery_parent is None else dict(recovery_parent),
        "producer_route_decision_refs": list(producer_refs),
    }
    decorated = (
        semantic_request_id
        + ":request-binding:"
        + content_hash(body)[:32]
    )
    if producer_refs:
        decorated += ":producer:" + content_hash(list(producer_refs))[:16]
    return {**body, "decorated_request_id": decorated}


def validate_dossier_request_identity(
    value: Any,
    *,
    semantic_request_id: str,
    producer_route_decision_refs: Sequence[str],
) -> dict[str, Any]:
    """Validate and reconstruct a persisted Dossier request binding."""

    if not isinstance(value, Mapping) or set(value) != {
        "schema_version", "semantic_request_id", "exact_config",
        "recovery_parent", "producer_route_decision_refs", "decorated_request_id",
    }:
        raise ValueError("Dossier request identity has an invalid closed shape")
    copied = dict(value)
    if (
        copied["schema_version"] != DOSSIER_REQUEST_IDENTITY_VERSION
        or copied["semantic_request_id"] != semantic_request_id
        or not isinstance(copied["decorated_request_id"], str)
    ):
        raise ValueError("Dossier request identity semantic binding drifted")
    exact = copied["exact_config"]
    if not isinstance(exact, Mapping) or set(exact) - {
        "capacity_retry", "transport_retry"
    }:
        raise ValueError("Dossier request identity config is invalid")
    if "transport_retry" in exact:
        from .document_extraction import validate_transport_retry

        validate_transport_retry(exact["transport_retry"])
    if "capacity_retry" in exact:
        retry = exact["capacity_retry"]
        if not isinstance(retry, Mapping) or set(retry) != {
            "cooldown_seconds", "max_recovery_epochs", "scheduler_max_attempts"
        }:
            raise ValueError("Dossier capacity retry identity is invalid")
        for key, maximum in (
            ("cooldown_seconds", 86400),
            ("max_recovery_epochs", 10),
            ("scheduler_max_attempts", 10),
        ):
            item = retry[key]
            minimum = 0 if key == "max_recovery_epochs" else 1
            if (
                isinstance(item, bool)
                or not isinstance(item, int)
                or not minimum <= item <= maximum
            ):
                raise ValueError("Dossier capacity retry identity is invalid")
    stored_refs = copied["producer_route_decision_refs"]
    expected_refs = sorted({str(ref) for ref in producer_route_decision_refs})
    if stored_refs != expected_refs:
        raise ValueError("Dossier request identity producer binding drifted")
    recovery = copied["recovery_parent"]
    if recovery is not None:
        if not isinstance(recovery, Mapping) or set(recovery) != {
            "kind", "work_order_ref", "result_envelope_hash", "proof_ref"
        }:
            raise ValueError("Dossier request recovery parent is invalid")
        patterns = {
            "operator": r"operator-recovery:[0-9a-f]{16}",
            "route_admission": r"route-admission:[0-9a-f]{64}",
            "capacity": r"capacity-recovery:[1-9][0-9]*:[0-9a-f]{16}",
        }
        pattern = patterns.get(recovery.get("kind"))
        if (
            pattern is None
            or re.fullmatch(pattern, str(recovery.get("proof_ref"))) is None
            or re.fullmatch(
                r"work:cockpit-[a-z0-9_]+-[0-9a-f]{32}",
                str(recovery.get("work_order_ref")),
            ) is None
            or re.fullmatch(
                r"[0-9a-f]{64}",
                str(recovery.get("result_envelope_hash")),
            ) is None
        ):
            raise ValueError("Dossier request recovery parent is invalid")
    rebuilt = dossier_request_identity(
        semantic_request_id=semantic_request_id,
        config=exact,
        producer_route_decision_refs=expected_refs,
        recovery_parent=recovery,
    )
    if canonical_json(rebuilt) != canonical_json(copied):
        raise ValueError("Dossier decorated request identity drifted")
    return copied


def _make_dossier_recovery_parent(
    *,
    kind: str,
    proof_ref: str,
    work_order_ref: str,
    formal: Mapping[str, Any],
) -> dict[str, Any]:
    envelope_hash = formal.get("result_envelope_hash")
    if not isinstance(envelope_hash, str):
        envelope = formal.get("result_envelope")
        if not isinstance(envelope, Mapping):
            raise CockpitModelError("Dossier recovery has no formal parent proof")
        envelope_hash = content_hash(envelope)
    parent = {
        "kind": kind,
        "work_order_ref": work_order_ref,
        "result_envelope_hash": envelope_hash,
        "proof_ref": proof_ref,
    }
    # Validate the closed recovery shape before it can enter an identity.
    probe = dossier_request_identity(
        semantic_request_id="recovery-shape-probe",
        config={},
        recovery_parent=parent,
    )
    validate_dossier_request_identity(
        probe,
        semantic_request_id="recovery-shape-probe",
        producer_route_decision_refs=(),
    )
    return parent


def _failure(work: WorkOrder, code: str, route_ref: str | None,
             *, message: str | None = None,
             chain_failures: Sequence[Mapping[str, Any]] = (),
             status: str = "failed") -> ResultEnvelope:
    identity = {"work_order_ref": work.id, "code": code, "route_ref": route_ref}
    return ResultEnvelope(
        schema_version=SCHEMA_VERSION, id=f"result:cockpit-control-{content_hash(identity)[:32]}",
        created_at=_now(), work_order_ref=work.id,
        invocation_ref=f"invocation:not-started:{content_hash(identity)[:32]}", status=status,
        outputs={}, actual_side_effects=(), usage_refs=(), artifact_refs=(),
        error={"code": code, **({} if message is None else {"message": message[:1000]})},
        metadata={"control_plane_failure": True, "route_decision_ref": route_ref,
                  "chain_failures": list(chain_failures)[:12]},
    )


def _legacy_broker_busy_failure(formal: Mapping[str, Any] | None) -> bool:
    """Recognize only the old signed chain envelope that misclassified BUSY."""

    if not isinstance(formal, Mapping) or formal.get("terminal_state") != "failed":
        return False
    envelope = formal.get("result_envelope")
    if not isinstance(envelope, Mapping):
        return False
    error = envelope.get("error")
    metadata = envelope.get("metadata")
    if (not isinstance(error, Mapping)
            or error.get("code") != "MODEL_CHAIN_EXHAUSTED"
            or not isinstance(metadata, Mapping)):
        return False
    failures = metadata.get("chain_failures")
    return (
        isinstance(failures, list)
        and len(failures) == 1
        and isinstance(failures[0], Mapping)
        and failures[0].get("code") == "BUSY"
        and failures[0].get("failure_class") == "unclassified_failure"
    )


def _capacity_busy_terminal(formal: Mapping[str, Any] | None) -> bool:
    if _legacy_broker_busy_failure(formal):
        return True
    if not isinstance(formal, Mapping) or formal.get("terminal_state") != "failed":
        return False
    envelope = formal.get("result_envelope") or {}
    failures = (envelope.get("metadata") or {}).get("chain_failures")
    return (
        envelope.get("error", {}).get("code") == "MODEL_CHAIN_EXHAUSTED"
        and isinstance(failures, list)
        and len(failures) == 1
        and isinstance(failures[0], Mapping)
        and failures[0].get("failure_class") == "capacity_busy"
        and failures[0].get("code") in {
            "BUSY", "CONCURRENCY_LIMIT", "BROKER_CONCURRENCY_LIMIT",
            "QUEUE_TIMEOUT", "BROKER_CLOSED"}
    )


def _capacity_retry(config: Mapping[str, Any]) -> dict[str, int]:
    return dict(config.get("capacity_retry") or {
        "cooldown_seconds": 1800,
        "max_recovery_epochs": 1,
        "scheduler_max_attempts": 3,
    })


def call_cost_micros(invocation: Any, route: Mapping[str, Any],
                     profile: Mapping[str, Any], reserved: int) -> tuple[int, str]:
    """What the link that actually served this call cost, and how we know.

    Provider telemetry when the broker reported it, else the rate card of the
    route that served -- never the rate card of the route that was asked for
    first, and never the reservation while a better number exists.  C2b made
    this public because the planner worker settles by the same rule.
    """

    return _cost_micros(invocation, route, profile, reserved)


def _cost_micros(invocation: Any, route: Mapping[str, Any], profile: Mapping[str, Any], reserved: int) -> tuple[int, str]:
    usage = dict(getattr(invocation, "usage", {}) or {})
    raw = usage.get("raw_provider_telemetry", {}).get("cost", {}) if isinstance(usage.get("raw_provider_telemetry"), Mapping) else {}
    if isinstance(raw, Mapping) and raw.get("available") is True and isinstance(raw.get("usd"), (int, float)) \
            and not isinstance(raw.get("usd"), bool) and float(raw["usd"]) >= 0:
        return int((Decimal(str(raw["usd"])) * 1_000_000).quantize(Decimal("1"), rounding=ROUND_HALF_UP)), "actual"
    try:
        return _route_estimate_micros(route, profile), "estimated"
    except ModelAccountingError:
        return reserved, "reserved"


class CockpitModel:
    """Run one bounded, budgeted, replayable model call for the cockpit."""

    def __init__(self, model_config: Mapping[str, Any], *, scheduler_db: str | Path,
                 adapter_factory: Callable[..., Any] | None = None, clock: Callable[[], datetime] | None = None,
                 max_input_tokens: int = 120_000, max_output_tokens: int = 3_000, max_cost_usd: float = 0.05,
                 timeout_seconds: int = 120) -> None:
        self.config = validate_model_config(model_config)
        self.scheduler_db = str(Path(scheduler_db).expanduser().resolve())
        self.adapter_factory = adapter_factory
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.max_input_tokens = max_input_tokens
        self.max_output_tokens = max_output_tokens
        self.max_cost_usd = max_cost_usd
        self.timeout_seconds = timeout_seconds

    def budget_for(self, purpose: str) -> dict[str, Any]:
        """The effective immutable budget for one call purpose."""
        return resolve_call_budget(self.config, purpose, defaults={
            "max_input_tokens": self.max_input_tokens,
            "max_output_tokens": self.max_output_tokens,
            "max_cost_usd": self.max_cost_usd,
            "timeout_seconds": self.timeout_seconds,
        })

    def _adapter(self, router: ModelRouter, *, timeout_seconds: int) -> Any:
        if self.adapter_factory is not None:
            return self.adapter_factory(router)
        config = self.config
        return OpenClawModelAdapter(
            config["broker_socket"], route_resolver=router.get_decision, auth_client_id=config["broker_client_id"],
            auth_key_provider=lambda: Path(config["broker_auth_key"]).read_bytes().strip(),
            expected_agent_id=config["expected_agent_id"], timeout_seconds=float(timeout_seconds),
            queue_wait_seconds=float((config.get("transport_retry") or {}).get(
                "queue_wait_seconds", 0)),
        )

    def _execute_with_safe_retry(self, adapter: Any, work: WorkOrder,
                                 route: Mapping[str, Any],
                                 profile: Mapping[str, Any]) -> Any:
        """Retry only the adapter's proof that no request crossed its boundary."""
        from .openclaw_model_adapter import BrokerDefinitelyNotSent

        maximum = int((self.config.get("transport_retry") or {}).get(
            "max_definitely_not_sent_retries", 0))
        for retry_number in range(maximum + 1):
            try:
                return adapter.execute(work, route, profile)
            except BrokerDefinitelyNotSent:
                if retry_number >= maximum:
                    raise
                backoff = int((self.config.get("transport_retry") or {}).get(
                    "retry_backoff_seconds", 0))
                if backoff:
                    import time
                    time.sleep(backoff)

    def call(self, *, purpose: str, request_id: str, prompt: str,
             mission: Mapping[str, Any],
             producer_route_decision_refs: Sequence[str] = (),
             _dossier_recovery_parent: Mapping[str, Any] | None = None,
             ) -> dict[str, Any]:
        """Return ``{text, replayed, cost_micros, cost_status, work_order_ref, ...}`` or raise."""
        capacity_retry = _capacity_retry(self.config)
        producer_refs = tuple(sorted({str(ref) for ref in producer_route_decision_refs}))
        semantic_request_id = request_id
        request_identity = None
        legacy_request_id = None
        if (
            purpose in _DOSSIER_PURPOSES
            and (
                "capacity_retry" in self.config
                or "transport_retry" in self.config
                or _dossier_recovery_parent is not None
            )
        ):
            request_identity = dossier_request_identity(
                semantic_request_id=semantic_request_id,
                config=self.config,
                producer_route_decision_refs=producer_refs,
                recovery_parent=_dossier_recovery_parent,
            )
            request_id = request_identity["decorated_request_id"]
            base_request_id = request_id
            if _dossier_recovery_parent is None:
                legacy_request_id = _legacy_decorated_request_id(
                    semantic_request_id, self.config, producer_refs
                )
        else:
            base_request_id = request_id
            if "capacity_retry" in self.config:
                policy_suffix = ":capacity-policy:" + content_hash(capacity_retry)[:16]
                canonical = re.search(
                    re.escape(policy_suffix)
                    + r"(?::route-admission:[0-9a-f]{64})?"
                    + r"(?::operator-recovery:[0-9a-f]{16})?"
                    + r"(?::capacity-recovery:\d+:[0-9a-f]{16})?$",
                    base_request_id,
                )
                if canonical is None:
                    base_request_id += policy_suffix
                request_id = base_request_id
            if "transport_retry" in self.config:
                retry_suffix = ":transport-policy:" + content_hash(
                    self.config["transport_retry"]
                )[:16]
                if retry_suffix not in base_request_id:
                    base_request_id += retry_suffix
                request_id = base_request_id
        legacy_budget = {
            "max_input_tokens": self.max_input_tokens,
            "max_output_tokens": self.max_output_tokens,
            "max_cost_usd": self.max_cost_usd,
            "timeout_seconds": self.timeout_seconds,
        }
        effective = self.budget_for(purpose)
        # Preserve the historical identity only when the effective wire is
        # exactly the constructor contract. A packaged purpose default is a
        # real budget change even when the installed JSON needs no new field.
        budget_changed = effective != legacy_budget
        explicit_budget = (budget_changed or "call_budget" in self.config
                           or purpose in self.config.get("purpose_call_budgets", {}))
        if producer_refs and request_identity is None:
            request_id = f"{request_id}:producer:{content_hash(list(producer_refs))[:16]}"
        # P13aa: the WorkOrder id is content-addressed on (purpose, request_id,
        # mission version, prompt) and deliberately excludes the clock -- but
        # the scheduler hashes the whole wire, which carried a wall-clock
        # created_at. So asking the identical question a second time produced
        # the same idempotency key with a different hash, which is the
        # scheduler's definition of a conflict: "this request is bound to
        # different content; ask again". Asking again could never help.
        #
        # Live, every Initial Screen section had been failing that way since
        # 2026-09-07 -- eight sections, every run, for two days, on a
        # deliverable whose inputs were by then complete.
        #
        # The timestamp is therefore derived from the same thing the id is: the
        # mission version this work belongs to. The scheduler still records its
        # own insertion time, so nothing loses the real clock; what goes into
        # the identity now agrees with the identity. (The SEC lane froze its
        # perception snapshot's generated_at for exactly this reason.)
        created_at = mission.get("created_at") or self.clock().isoformat(timespec="microseconds")
        provider_contract = _verifier_provider_contract(purpose) if producer_refs else None
        work_args = {
            "purpose": purpose,
            "prompt": prompt,
            "mission_version_ref": mission["id"],
            "max_input_tokens": effective["max_input_tokens"],
            "max_output_tokens": effective["max_output_tokens"],
            "max_cost_usd": effective["max_cost_usd"],
            "max_seconds": effective["timeout_seconds"],
            "budget_identity": (
                budget_fingerprint(effective) if explicit_budget else None
            ),
            "created_at": created_at,
            "verifier_provider_contract": (
                provider_contract[0] if provider_contract else None
            ),
            "verifier_provider_schema_hash": (
                provider_contract[1] if provider_contract else None
            ),
            "mission_version_hash": (
                mission["content_hash"]
                if purpose in {
                    "investment_memo", "investment_memo_verifier",
                    "dossier", "dossier_verifier",
                }
                else None
            ),
            "producer_route_decision_refs": producer_refs,
        }
        work = build_work(
            request_id=request_id,
            request_identity=request_identity,
            **work_args,
        )
        legacy_work = (
            build_work(request_id=legacy_request_id, **work_args)
            if legacy_request_id is not None
            else None
        )
        scope = {"mission_ref": mission["mission_ref"], "mission_version_ref": mission["id"],
                 "mission_version_hash": mission["content_hash"],
                 "max_daily_paid_calls": int(mission["budget"]["max_daily_paid_calls"]),
                 "max_daily_cost_micros": int(Decimal(str(mission["budget"]["max_daily_cost_usd"])) * 1_000_000),
                 # C2: which of the day's four pools this purpose spends from,
                 # and that mission version's split of the day. The caps
                 # travel with the admission because the day ledger is a
                 # separate authority and must not open the Core to learn what
                 # a mission said.
                 **mission_pool_scope(mission, purpose=purpose)}
        # P13s: the lease has to outlast the call it covers. The scheduler's
        # default lease is 30 s and its ceiling 60; a cockpit call is allowed
        # up to its own timeout, and a reasoning model on a large prompt takes
        # longer than either. When the lease lapsed mid-call the completion was
        # refused with "attempt is not the current leased attempt" -- the work
        # was done and paid for, and the answer was thrown away.
        transport = self.config.get("transport_retry") or {}
        retries = int(transport.get("max_definitely_not_sent_retries", 0))
        per_try = (float(effective["timeout_seconds"])
                   + float(transport.get("queue_wait_seconds", 0)))
        with ModelRouter(self.config["model_router_db"]) as lease_router:
            lease_policy = lease_router.get_policy(self.config["routing_policy_ref"])
        declared_chains = [
            len(chain) for chain in (lease_policy.get("fallback_chains") or {}).get(
                "tiers", {}).values()
        ] + [
            len(entry.get("chain") or ())
            for entry in (lease_policy.get("purpose_overrides") or {}).values()
            if entry.get("mode") == "explicit"
        ]
        candidates = max([1, *declared_chains])
        lease_seconds = (candidates * (retries + 1) * per_try
                         + candidates * retries * int(transport.get("retry_backoff_seconds", 0))
                         + _LEASE_GRACE_SECONDS)
        # The lease bounds are a frozen versioned policy: the same
        # policy_version_id with different settings is a conflict, and the
        # shared "scheduler-policy-0.1" is sized for calls that finish in
        # seconds. So this names its own version after the bound it needs --
        # the id and the settings can never disagree, and the lanes that are
        # fast keep the policy they have.
        with Scheduler(
            self.scheduler_db,
            clock=self.clock,
            policy_version_id=(f"scheduler-policy-lease-{int(lease_seconds)}s-"
                               f"attempts-{capacity_retry['scheduler_max_attempts']}-"
                               f"routes-{lease_policy['content_hash'][:16]}-0.1"),
            max_attempts=capacity_retry["scheduler_max_attempts"],
            max_lease_seconds=lease_seconds,
            max_total_lease_seconds=lease_seconds * 2,
        ) as scheduler:
            if legacy_work is not None:
                legacy_formal = scheduler.formal_result(legacy_work.id)
                if legacy_formal is not None:
                    # Old successful work remains replayable byte-for-byte.
                    # Failed history has no closed request/config/recovery
                    # binding, so it remains terminal rather than becoming a
                    # newly decorated paid call.
                    return self._answer(
                        legacy_formal,
                        legacy_work,
                        True,
                        0,
                        "replayed",
                    )
            if scheduler.enqueue(work)["status"] == "conflict":
                raise CockpitModelError("this request is bound to different content; ask again")
            formal = scheduler.formal_result(work.id)
            dossier_bound = request_identity is not None
            prior_recovery_kind = (
                (request_identity.get("recovery_parent") or {}).get("kind")
                if dossier_bound
                else None
            )
            if (formal is not None and formal.get("terminal_state") == "failed"
                    and (
                        prior_recovery_kind != "operator"
                        if dossier_bound
                        else ":operator-recovery:" not in base_request_id
                    )):
                from .controlled_failure_redrive import approved_request

                recovery_suffix = approved_request(
                    self.scheduler_db,
                    self.config["budget_db"],
                    old_work_order_ref=work.id,
                    formal=formal,
                    mission=mission,
                )
                if recovery_suffix is not None:
                    if dossier_bound:
                        parent = _make_dossier_recovery_parent(
                            kind="operator",
                            proof_ref=recovery_suffix.removeprefix(":"),
                            work_order_ref=work.id,
                            formal=formal,
                        )
                        return self.call(
                            purpose=purpose,
                            request_id=semantic_request_id,
                            prompt=prompt,
                            mission=mission,
                            producer_route_decision_refs=producer_refs,
                            _dossier_recovery_parent=parent,
                        )
                    return self.call(
                        purpose=purpose,
                        request_id=base_request_id + recovery_suffix,
                        prompt=prompt,
                        mission=mission,
                        producer_route_decision_refs=producer_refs,
                    )
            from .model_route_recovery import route_recovery_request
            recovery_request = route_recovery_request(
                formal, work_order_ref=work.id, request_id=base_request_id,
                config=self.config,
            )
            if recovery_request is not None:
                if dossier_bound:
                    marker = ":route-admission:"
                    proof = recovery_request.rsplit(marker, 1)[-1]
                    parent = _make_dossier_recovery_parent(
                        kind="route_admission",
                        proof_ref="route-admission:" + proof,
                        work_order_ref=work.id,
                        formal=formal,
                    )
                    return self.call(
                        purpose=purpose,
                        request_id=semantic_request_id,
                        prompt=prompt,
                        mission=mission,
                        producer_route_decision_refs=producer_refs,
                        _dossier_recovery_parent=parent,
                    )
                return self.call(
                    purpose=purpose, request_id=recovery_request, prompt=prompt,
                    mission=mission, producer_route_decision_refs=producer_refs,
                )
            capacity_terminal = formal
            if formal is None and scheduler.status(work.id)["state"] == "failed":
                row = scheduler.connection.execute(
                    "SELECT result_envelope_json,result_envelope_hash,created_at "
                    "FROM scheduler_result_envelopes "
                    "WHERE work_order_id=? ORDER BY attempt_number DESC LIMIT 1",
                    (work.id,),
                ).fetchone()
                if row is not None:
                    capacity_terminal = {
                        "terminal_state": "failed",
                        "result_envelope": json.loads(row["result_envelope_json"]),
                        "result_envelope_hash": row["result_envelope_hash"],
                        "created_at": row["created_at"],
                    }
            match = (
                None
                if dossier_bound
                else re.search(
                    r":capacity-recovery:(\d+):[0-9a-f]{16}$",
                    base_request_id,
                )
            )
            if dossier_bound and prior_recovery_kind == "capacity":
                proof_ref = (request_identity["recovery_parent"] or {})["proof_ref"]
                epoch = int(proof_ref.split(":", 2)[1])
            else:
                epoch = int(match.group(1)) if match else 0
            if (_capacity_busy_terminal(capacity_terminal)
                    and epoch < capacity_retry["max_recovery_epochs"]):
                completed_at = datetime.fromisoformat(str(capacity_terminal["created_at"]))
                elapsed = (self.clock().astimezone(timezone.utc)
                           - completed_at.astimezone(timezone.utc)).total_seconds()
                if elapsed >= capacity_retry["cooldown_seconds"]:
                    policy_hash = content_hash(capacity_retry)[:16]
                    if dossier_bound:
                        parent = _make_dossier_recovery_parent(
                            kind="capacity",
                            proof_ref=(
                                f"capacity-recovery:{epoch + 1}:{policy_hash}"
                            ),
                            work_order_ref=work.id,
                            formal=capacity_terminal,
                        )
                        return self.call(
                            purpose=purpose,
                            request_id=semantic_request_id,
                            prompt=prompt,
                            mission=mission,
                            producer_route_decision_refs=producer_refs,
                            _dossier_recovery_parent=parent,
                        )
                    clean_request = (base_request_id[:match.start()] if match
                                     else base_request_id)
                    recovery_request = (f"{clean_request}:capacity-recovery:"
                                        f"{epoch + 1}:{policy_hash}")
                    return self.call(
                        purpose=purpose,
                        request_id=recovery_request,
                        prompt=prompt,
                        mission=mission,
                        producer_route_decision_refs=producer_refs,
                    )
            if _capacity_busy_terminal(capacity_terminal):
                if epoch >= capacity_retry["max_recovery_epochs"]:
                    raise CockpitModelError("capacity_recovery_exhausted")
                raise CockpitModelError("capacity_busy; recovery cooldown has not elapsed")
            replayed = formal is not None
            cost_micros, cost_status = 0, "replayed"
            if formal is None:
                lease = scheduler.claim(WORKER_REF, work_order_id=work.id,
                                        lease_seconds=lease_seconds)
                if lease is None:
                    raise CockpitModelError("this request is already running")
                attempt = lease["attempt"]["attempt_number"]
                with ModelRouter(self.config["model_router_db"]) as router, \
                        ThesisImpactBudgetStore(self.config["budget_db"]) as budget:
                    prompt_bytes = len(prompt.encode("utf-8"))
                    pool_rejection: dict[str, Any] | None = None
                    result: ResultEnvelope
                    from .model_fallback_chain import served_family
                    try:
                        producer_families = {
                            served_family(router, ref) for ref in producer_refs}
                    except Exception as exc:  # noqa: BLE001 - unknown is not independent
                        result = _failure(work, "PRODUCER_ROUTE_UNRESOLVED", None)
                        scheduler.complete(
                            work.id, attempt, WORKER_REF, lease["lease_token"], result,
                            idempotency_key=f"cockpit-complete:{work.id}:{attempt}")
                        raise CockpitModelError(
                            "a producer route decision could not prove its model family"
                        ) from exc
                    if any(family.startswith("unclassified:")
                           for family in producer_families):
                        result = _failure(work, "MODEL_ROUTE_REJECTED", None)
                        scheduler.complete(
                            work.id, attempt, WORKER_REF, lease["lease_token"], result,
                            idempotency_key=f"cockpit-complete:{work.id}:{attempt}")
                        raise CockpitModelError("verifier_not_independent")
                    tier = self._chain_tier(router, purpose)
                    if tier is not None:
                        # P14-M: the pinned policy carries this tier's fallback
                        # chain, so the call walks it. One provider being down
                        # stops being a lost call and becomes a second route
                        # decision on a named alternative.
                        outcome = self._chained(
                            router, budget, work=work, purpose=purpose, tier=tier,
                            attempt=attempt, prompt_bytes=prompt_bytes, scope=scope,
                            call_budget=effective,
                            producer_route_decision_refs=producer_refs,
                        )
                        result = outcome["result"]
                        failure = outcome["failure"]
                        cost_micros, cost_status = outcome["cost_micros"], outcome["cost_status"]
                        completion = scheduler.complete(
                            work.id, attempt, WORKER_REF, lease["lease_token"], result,
                            idempotency_key=f"cockpit-complete:{work.id}:{attempt}")
                        if completion["status"] == "conflict":
                            raise CockpitModelError("the request completion conflicted; ask again")
                        if failure is not None:
                            _raise_failure(
                                failure, outcome.get("pool_rejection"),
                                failure_trace=_failed_work_trace(
                                    scheduler, work, purpose=purpose,
                                    request_id=base_request_id),
                            )
                        formal = scheduler.formal_result(work.id)
                        return self._answer(
                            formal, work, replayed, cost_micros, cost_status,
                            failure_trace=_failed_work_trace(
                                scheduler, work, purpose=purpose,
                                request_id=base_request_id),
                        )
                    route = router.route(
                        work, attempt_number=attempt,
                        capability=work.requested_capabilities[0],
                        policy_version_ref=self.config["routing_policy_ref"],
                        credential_slot_refs=self.config["credential_slot_refs"], required_modalities=("text",),
                        required_context_tokens=prompt_bytes + effective["max_output_tokens"],
                        estimated_input_tokens=prompt_bytes, estimated_output_tokens=effective["max_output_tokens"],
                        idempotency_key=f"cockpit-route:{work.id}:{attempt}",
                        producer_family=next(iter(producer_families), None),
                    )["decision"]
                    if route["outcome"] != "selected":
                        result = _failure(work, "MODEL_ROUTE_REJECTED", route["id"])
                        failure = "no model route is available right now"
                    else:
                        profile = router.get_profile(route["selected_profile_version_ref"])
                        if producer_refs:
                            if any(not independent_families(
                                profile["family"], producer_family
                            ) for producer_family in producer_families):
                                result = _failure(work, "MODEL_ROUTE_REJECTED", route["id"])
                                failure = "verifier_not_independent"
                                completion = scheduler.complete(
                                    work.id, attempt, WORKER_REF, lease["lease_token"], result,
                                    idempotency_key=f"cockpit-complete:{work.id}:{attempt}")
                                _raise_failure(
                                    failure, None,
                                    failure_trace=_failed_work_trace(
                                        scheduler, work, purpose=purpose,
                                        request_id=base_request_id),
                                )
                        reserved = int(Decimal(str(effective["max_cost_usd"])) * 1_000_000)
                        decision = admit_day_ledger(
                            budget,
                            policy_version_id=self.config["budget_policy_ref"],
                            day=self.clock().astimezone(timezone.utc).date().isoformat(),
                            work_order_ref=work.id, attempt_number=attempt, phase="assessment",
                            route_decision_ref=route["id"], reserved_micros=reserved,
                            mission_binding=scope,
                        )
                        admission = decision.get("admission")
                        if decision["status"] == "refused":
                            result = _failure(work, "BUDGET_REFUSED", route["id"])
                            failure = decision["failure"]
                        elif decision["status"] == "pool_exhausted":
                            # A spent pool is returned rather than raised: this
                            # kind of work has had its share of the day, which
                            # is a decision and not a fault.
                            pool_rejection = decision["rejection"]
                            result = _failure(work, "POOL_EXHAUSTED", route["id"])
                            failure = decision["failure"]
                        if admission is not None:
                            try:
                                invocation, result = self._execute_with_safe_retry(
                                    self._adapter(
                                        router, timeout_seconds=effective["timeout_seconds"]),
                                    work, route, profile,
                                )
                                cost_micros, cost_status = _cost_micros(invocation, route, profile, reserved)
                                if result.status == "succeeded":
                                    failure = None
                                else:
                                    error = result.error or {}
                                    code = str(error.get("code", "")).upper()
                                    broker_local_not_sent = code in {
                                        "BUSY", "CONCURRENCY_LIMIT",
                                        "BROKER_CONCURRENCY_LIMIT",
                                        "QUEUE_TIMEOUT", "BROKER_CLOSED",
                                    }
                                    if broker_local_not_sent:
                                        cost_micros, cost_status = 0, "failed"
                                    elif cost_status != "actual":
                                        cost_micros, cost_status = reserved, "reserved"
                                    failure = str(error.get("message") or "the model call failed")
                            except OpenClawModelAdapterError as exc:
                                result = _failure(work, "MODEL_ADAPTER_REJECTED_OR_FAILED", route["id"])
                                if isinstance(exc, BrokerDefinitelyNotSent):
                                    cost_micros, cost_status = 0, "not_sent"
                                else:
                                    cost_micros, cost_status = reserved, "reserved"
                                failure = f"the model call failed: {exc}"
                            settle_day_ledger(budget, admission, actual_micros=cost_micros)
                    completion = scheduler.complete(work.id, attempt, WORKER_REF, lease["lease_token"], result,
                                                    idempotency_key=f"cockpit-complete:{work.id}:{attempt}")
                    if completion["status"] == "conflict":
                        raise CockpitModelError("the request completion conflicted; ask again")
                    if failure is not None:
                        _raise_failure(
                            failure, pool_rejection,
                            failure_trace=_failed_work_trace(
                                scheduler, work, purpose=purpose,
                                request_id=base_request_id),
                        )
                formal = scheduler.formal_result(work.id)
            return self._answer(
                formal, work, replayed, cost_micros, cost_status,
                failure_trace=_failed_work_trace(
                    scheduler, work, purpose=purpose, request_id=base_request_id),
            )

    @staticmethod
    def _answer(formal: Any, work: WorkOrder, replayed: bool, cost_micros: int,
                cost_status: str, *,
                failure_trace: Mapping[str, Any] | None = None) -> dict[str, Any]:
        if formal is None or formal["terminal_state"] != "succeeded":
            envelope = {} if formal is None else formal.get("result_envelope") or {}
            error = envelope.get("error") or {}
            code = error.get("code")
            suffix = f" ({code})" if isinstance(code, str) and code else ""
            error_type = (
                CockpitModelRouteUnavailable
                if code == "MODEL_ROUTE_REJECTED" else CockpitModelError
            )
            raise error_type(
                f"the model call did not succeed{suffix}",
                failure_trace=failure_trace,
            )
        envelope = formal["result_envelope"]
        text = envelope.get("outputs", {}).get("text")
        if not isinstance(text, str):
            raise CockpitModelError("the model returned no text")
        return {"text": text, "replayed": replayed, "cost_micros": cost_micros, "cost_status": cost_status,
                "work_order_ref": work.id, "result_envelope_ref": formal["result_envelope_id"],
                "invocation_ref": envelope.get("invocation_ref"),
                "route_decision_ref": envelope.get("metadata", {}).get("route_decision_ref")}

    def _chain_tier(self, router: ModelRouter, purpose: str) -> str | None:
        """The tier to walk, or None to route the single-shot way.

        A chain runs only when the *pinned policy version* declares one. That
        keeps the choice where every other routing choice already is -- in the
        version a lane pinned -- and it means an installation that has not been
        repointed at a tier keeps behaving exactly as it did.
        """

        from .model_fallback_chain import purpose_tiers

        try:
            policy = router.get_policy(self.config["routing_policy_ref"])
        except RoutingPolicyNotFound:
            return None
        declared = (policy.get("fallback_chains") or {}).get("tiers", {})
        tier = purpose_tiers().get(purpose)
        override = (policy.get("purpose_overrides") or {}).get(purpose)
        if not declared and (
            override is None or override.get("mode") != "explicit"
        ):
            return None
        if tier is None:
            # The policy offers chains and this purpose has not said which one
            # it belongs to. Refusing beats guessing: the tier decides what kind
            # of model answers, and no default is the right default.
            raise CockpitModelError(
                f"the purpose {purpose!r} has no model tier; register one before routing"
            )
        return tier if tier in declared or override is not None else None

    def _chain_ceiling(self, router: ModelRouter, tier: str, prompt_bytes: int,
                       *, purpose: str, call_budget: Mapping[str, Any]) -> int:
        """The most this attempt could cost, whichever link ends up serving.

        The day ledger identifies an admission by (work order, attempt, phase),
        so one attempt reserves once -- it cannot hold a separate reservation
        per link without claiming to be a different attempt, which it is not.
        The reservation is therefore the dearest link the chain could reach, and
        the *settlement* -- the number that actually moves the day's spend -- is
        the served link's own rate card. Reserve the ceiling, pay what ran.
        """

        from .model_fallback_chain import tier_chain
        from .model_router import resolve_chain

        profiles = router.latest_profiles()
        policy = router.get_policy(self.config["routing_policy_ref"])
        resolved = resolve_chain(
            policy, tier=tier, purpose=purpose,
            profiles={profile["id"]: profile for profile in profiles},
        )
        wanted = set(resolved["chain"] if resolved is not None else tier_chain(tier))
        ceiling = Decimal(0)
        for profile in profiles:
            if profile["id"] not in wanted or profile.get("status") == "retired":
                continue
            cost = profile["cost"]
            ceiling = max(ceiling, (
                Decimal(str(cost["input_per_million_usd"])) * prompt_bytes
                + Decimal(str(cost["output_per_million_usd"])) * call_budget["max_output_tokens"]
            ) / Decimal(1_000_000))
        if ceiling <= 0:
            ceiling = Decimal(str(call_budget["max_cost_usd"]))
        return int((ceiling * 1_000_000).quantize(Decimal("1"), rounding=ROUND_HALF_UP))

    def _chained(self, router: ModelRouter, budget: Any, *, work: WorkOrder, purpose: str,
                 tier: str, attempt: int, prompt_bytes: int,
                 scope: Mapping[str, Any],
                 call_budget: Mapping[str, Any],
                 producer_route_decision_refs: Sequence[str] = ()) -> dict[str, Any]:
        """Walk the tier's chain under one reservation, retaining uncertain spend."""

        from .model_fallback_chain import classify_model_failure, execute_chain

        day = self.clock().astimezone(timezone.utc).date().isoformat()
        ceiling = self._chain_ceiling(
            router, tier, prompt_bytes, purpose=purpose, call_budget=call_budget)
        admission: dict[str, Any] | None = None
        first_route_ref: str | None = None
        spend: dict[str, tuple[int, str]] = {}
        uncertain_spend = False
        refusal: list[str] = []
        pool_rejection: dict[str, Any] | None = None

        def admit(route: Mapping[str, Any], profile: Mapping[str, Any], micros: int) -> Any:
            nonlocal admission, first_route_ref, pool_rejection
            if admission is not None:
                # One attempt, one reservation. The later links of a chain run
                # under the reservation the first one took out.
                return {"status": "admitted"}
            try:
                admission = budget.admit(
                    policy_version_id=self.config["budget_policy_ref"], day=day,
                    work_order_ref=work.id, attempt_number=attempt, phase="assessment",
                    route_decision_ref=route["id"],
                    reserved_micros=max(ceiling, micros), mission_binding=scope,
                )
            except ThesisImpactBudgetError as exc:
                refusal.append(str(exc))
                return None
            if admission.get("status") == "rejected":
                # The chain halts on the first link: every link in a tier
                # spends from the same pool, so a spent pool refuses all of
                # them and trying the next one only writes another refusal.
                pool_rejection = admission
                admission = None
                refusal.append(_pool_refusal_message(pool_rejection))
                return None
            first_route_ref = route["id"]
            return {"status": "admitted"}

        def call(route: Mapping[str, Any], profile: Mapping[str, Any]) -> dict[str, Any]:
            nonlocal uncertain_spend
            try:
                invocation, envelope = self._execute_with_safe_retry(
                    self._adapter(router, timeout_seconds=call_budget["timeout_seconds"]),
                    work, route, profile,
                )
            except OpenClawModelAdapterError as exc:
                definitely_not_sent = isinstance(exc, BrokerDefinitelyNotSent)
                spend[route["id"]] = (
                    (0, "not_sent") if definitely_not_sent else (ceiling, "reserved")
                )
                uncertain_spend = uncertain_spend or not definitely_not_sent
                return {"outcome": "failed",
                        "failure_class": (classify_model_failure(exc)
                                          if definitely_not_sent else "unclassified_failure"),
                        "reason": f"the model call failed: {exc}"}
            if envelope.status != "succeeded":
                # The broker answered and the answer is a failure. Its error
                # code, not a guess, decides whether another model may be asked.
                failure_class = classify_model_failure(envelope.error or {})
                # Only broker-local admission responses are known to precede
                # a provider call. Every other failed host envelope may have
                # consumed the full bounded call before validation failed.
                code = str((envelope.error or {}).get("code", "")).upper()
                may_have_reached_provider = code not in {
                    "BUSY", "CONCURRENCY_LIMIT", "BROKER_CONCURRENCY_LIMIT",
                    "QUEUE_TIMEOUT", "BROKER_CLOSED",
                }
                spend[route["id"]] = ((ceiling, "reserved")
                                      if may_have_reached_provider else (0, "failed"))
                uncertain_spend = uncertain_spend or may_have_reached_provider
                return {"outcome": "failed",
                        "failure_class": (
                            "unclassified_failure"
                            if may_have_reached_provider and failure_class in {
                                "transport_failure", "provider_failure", "model_unavailable"
                            }
                            else failure_class
                        ),
                        "error_code": (envelope.error or {}).get("code"),
                        "reason": (envelope.error or {}).get("message", "the model call failed"),
                        "value": envelope}
            spend[route["id"]] = _cost_micros(invocation, route, profile, ceiling)
            return {"outcome": "served", "value": envelope}

        served_micros, served_status = 0, "failed"
        try:
            outcome = execute_chain(
                router, work, purpose=purpose, tier=tier,
                capability=work.requested_capabilities[0],
                attempt_number=attempt,
                policy_version_ref=self.config["routing_policy_ref"],
                credential_slot_refs=self.config["credential_slot_refs"],
                required_modalities=("text",),
                required_context_tokens=prompt_bytes + call_budget["max_output_tokens"],
                estimated_input_tokens=prompt_bytes,
                estimated_output_tokens=call_budget["max_output_tokens"],
                idempotency_prefix=f"cockpit-route:{work.id}:{attempt}",
                call=call, admit=admit,
                producer_decision_refs=producer_route_decision_refs,
            )
            if outcome["status"] == "served":
                served_micros, served_status = spend.get(
                    outcome["route_decision_ref"], (0, "failed"))
        finally:
            # A proved pre-send failure costs zero. Once bytes may have left,
            # missing telemetry cannot release the reservation: the provider
            # may still have completed and charged the call.
            if admission is not None:
                settlement = ceiling if uncertain_spend else served_micros
                budget.settle(admission["admission_id"], actual_micros=settlement)

        route_ref = outcome.get("route_decision_ref") or first_route_ref
        if outcome["status"] == "served":
            return {"result": outcome["value"], "failure": None,
                    "cost_micros": served_micros, "cost_status": served_status,
                    "pool_rejection": None}
        if outcome["status"] == "halted" and outcome.get("reason") == "budget_refused":
            if pool_rejection is not None:
                return {"result": _failure(work, "POOL_EXHAUSTED", route_ref),
                        "failure": _pool_refusal_message(pool_rejection),
                        "cost_micros": 0, "cost_status": "failed",
                        "pool_rejection": pool_rejection}
            reason = refusal[-1] if refusal else "the day cap is exhausted"
            return {"result": _failure(work, "BUDGET_REFUSED", route_ref),
                    "failure": f"today's research budget refused the call: {reason}",
                    "cost_micros": ceiling if uncertain_spend else 0,
                    "cost_status": "reserved" if uncertain_spend else "failed",
                    "pool_rejection": None}
        if not outcome["links"]:
            return {"result": _failure(work, "MODEL_ROUTE_REJECTED", route_ref),
                    "failure": "no model route is available right now",
                    "cost_micros": 0, "cost_status": "failed",
                    "pool_rejection": None}
        skipped = ", ".join(
            f"{link['profile_id']} ({link['skip_reason']})"
            for link in outcome["links"] if not link["served"]
        )
        details = list(outcome.get("failures") or [])
        detail_text = "; ".join(
            f"{item['profile_id']} [{item['code']}]: {item['message']}"
            for item in details)
        if outcome["status"] == "halted":
            failure = f"the {tier} chain halted on {outcome.get('reason')}: {skipped}"
        else:
            failure = f"every model in the {tier} chain failed: {skipped}"
        if detail_text:
            failure += f"; broker details: {detail_text}"
        retryable = outcome["status"] == "halted" and outcome.get("reason") == "capacity_busy"
        return {"result": _failure(
                    work, "MODEL_CHAIN_EXHAUSTED", route_ref,
                    message=failure, chain_failures=details,
                    status="retryable" if retryable else "failed"),
                "failure": failure,
                "cost_micros": ceiling if uncertain_spend else 0,
                "cost_status": "reserved" if uncertain_spend else "failed",
                "pool_rejection": None}


def unwrap_json_object(text: str) -> dict[str, Any] | None:
    """Best-effort: the first JSON object in a model reply, fence or prose around it tolerated."""
    stripped = text.strip()
    candidates = [stripped]
    if stripped.startswith("```"):
        body = stripped.split("\n", 1)[1] if "\n" in stripped else ""
        if body.rstrip().endswith("```"):
            body = body.rstrip()[:-3]
        candidates.insert(0, body.strip())
    start, end = stripped.find("{"), stripped.rfind("}")
    if start != -1 and end > start:
        candidates.append(stripped[start:end + 1])
    for candidate in candidates:
        try:
            value = json.loads(candidate)
        except ValueError:
            continue
        if isinstance(value, dict):
            return value
    return None


__all__ = [
    "CockpitModel", "CockpitModelError", "CockpitModelPoolExhausted",
    "CockpitModelRouteUnavailable",
    "WORKER_REF", "admit_day_ledger", "build_work", "call_cost_micros",
    "dossier_request_identity", "independent_model_call", "lane_status_for",
    "pool_refusal_message", "purposes", "register_purpose",
    "settle_day_ledger", "unwrap_json_object",
    "validate_dossier_request_identity",
]
