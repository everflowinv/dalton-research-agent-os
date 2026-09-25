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
import sqlite3
import sys
import time
from importlib import resources
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .contracts import ResultEnvelope, WorkOrder
from .call_budget import budget_fingerprint, default_call_budget, resolve_call_budget
from .model_transport import (
    broker_frame_execution_binding,
    resolve_broker_max_frame_bytes,
)
from .document_extraction import validate_model_config
from .model_accounting import ModelAccountingError, _route_estimate_micros
from .model_router import ModelRouter, RoutingPolicyNotFound, independent_families
from .openclaw_model_adapter import (
    BrokerDefinitelyNotSent,
    ModelAdmissionError,
    OpenClawModelAdapter,
    OpenClawModelAdapterError,
)
from .lease_holder_registry import LeaseHolderRegistry, holder_gone
from .scheduler import LeaseRejected, Scheduler, SchedulerConflict
from .sqlite_contention import (
    LEASE_RELEASE_RETRY_SECONDS,
    is_sqlite_lock_error,
    retry_on_sqlite_lock,
    sqlite_failure_location,
    traceback_digest,
)
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

SETUP_PLANNING_SCHEMA_VERSION = "workspace-setup-planning-context-0.1"


def validate_setup_planning_context(value: Mapping[str, Any]) -> dict[str, Any]:
    """Validate the authority and spend envelope for a pre-mission goal call."""
    fields = {"schema_version", "setup_ref", "workspace_id", "foundation_ref",
              "foundation_hash", "budget", "created_at", "content_hash"}
    if not isinstance(value, Mapping) or set(value) != fields:
        raise CockpitModelError("setup planning context has an invalid closed shape")
    wire = dict(value)
    asserted = wire.pop("content_hash")
    if (wire.get("schema_version") != SETUP_PLANNING_SCHEMA_VERSION
            or not isinstance(asserted, str) or content_hash(wire) != asserted):
        raise CockpitModelError("setup planning context content hash differs")
    for name in ("setup_ref", "workspace_id", "foundation_ref", "created_at"):
        if not isinstance(wire.get(name), str) or not wire[name].strip():
            raise CockpitModelError(f"setup planning context {name} is invalid")
    if not re.fullmatch(r"[0-9a-f]{64}", str(wire.get("foundation_hash"))):
        raise CockpitModelError("setup planning context foundation_hash is invalid")
    budget = wire.get("budget")
    required = {"max_daily_paid_calls", "max_daily_cost_usd",
                "max_alphaengine_calls_24h"}
    if not isinstance(budget, Mapping) or not required.issubset(budget):
        raise CockpitModelError("setup planning context lacks its bounded budget")
        if (isinstance(budget["max_daily_paid_calls"], bool)
            or not isinstance(budget["max_daily_paid_calls"], int)
            or not 1 <= budget["max_daily_paid_calls"] <= 30):
            raise CockpitModelError(
                "setup planning permits at most thirty paid calls")
    amount = budget["max_daily_cost_usd"]
    if (isinstance(amount, bool) or not isinstance(amount, (int, float))
            or not 0 <= float(amount) <= 10.0):
        raise CockpitModelError("setup planning cost must be between zero and ten dollars")
    return {**wire, "budget": dict(budget), "content_hash": asserted}

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
    "mission_directed_document_verifier": (
        "annual-report-verifier-provider-output-0.1",
        "annual-report-verifier-provider-output-v0.1.schema.json"),
    "debate_map_verifier": (
        "debate-map-verifier-provider-output-0.1",
        "debate-map-verifier-provider-output-v0.1.schema.json"),
    "conviction_call_verifier": (
        "conviction-call-verifier-provider-output-0.1",
        "conviction-call-verifier-provider-output-v0.1.schema.json"),
    "research_localization_verifier": (
        "research-localization-verifier-provider-output-0.1",
        "research-localization-verifier-provider-output-v0.1.schema.json"),
    # 2026-09-25: the statement-support check (e41b4df8, 52393693) always
    # names its producers, so its route is an independent verifier -- and
    # without a contract here its WorkOrder carried no output schema version
    # and the adapter refused every call before sending it ("independent
    # verifier WorkOrder lacks the required output schema version").
    "claim_support_verifier": (
        "claim-support-verifier-provider-output-0.1",
        "claim-support-verifier-provider-output-v0.1.schema.json"),
    "claim_support_backfill": (
        "claim-support-verifier-provider-output-0.1",
        "claim-support-verifier-provider-output-v0.1.schema.json"),
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
_LOCAL_NOT_SENT_PROOF = {
    "authority": "openclaw-model-adapter",
    "state": "definitely_not_sent",
    "version": "0.1",
}
# 2026-09-24: what a CLI-gateway call costs that its prompt does not show --
# the vendor CLI's own system prompt and tools, measured per gateway and kept
# with the rule in ``model_profile_bounds`` so the WorkOrder token ceilings the
# router and adapter enforce use the same number.  The old ceiling,
# ``input_rate * prompt_bytes + output_rate * max_output``, reserved 0.1128 USD
# for event judgements that cost 0.27-0.28 USD, and every one overran.  Here
# the prompt's bytes are counted on top of the fixed part (a byte over-counts a
# token), and all of the input is priced at the cache-write multiplier, which is
# exact for claude and conservative for the gateways that bill it as plain
# input.
from .model_profile_bounds import (  # noqa: E402 - re-exported for callers
    CLI_GATEWAY_CACHE_WRITE_MULTIPLIER,
    CLI_GATEWAY_PROVIDER_SUFFIX,
    CLI_GATEWAY_SYSTEM_PROMPT_TOKENS,
    is_cli_gateway_profile,
)


def profile_call_ceiling_usd(profile: Mapping[str, Any], *, prompt_bytes: int,
                             max_output_tokens: int) -> Decimal:
    """The most one call on this profile can cost, in USD.

    Prompt bytes stand in for input tokens (a token is never shorter than a
    byte).  A CLI-gateway profile also pays for the CLI's hidden system prompt,
    and pays for all of its input at the cache-write rate; see
    ``CLI_GATEWAY_SYSTEM_PROMPT_TOKENS``.
    """

    cost = profile["cost"]
    input_rate = Decimal(str(cost["input_per_million_usd"]))
    input_tokens = Decimal(prompt_bytes)
    if is_cli_gateway_profile(profile):
        input_rate *= CLI_GATEWAY_CACHE_WRITE_MULTIPLIER
        input_tokens += CLI_GATEWAY_SYSTEM_PROMPT_TOKENS
    return (
        input_rate * input_tokens
        + Decimal(str(cost["output_per_million_usd"])) * max_output_tokens
    ) / Decimal(1_000_000)


# WP-A/A1. The broker's own word that the provider ran and returned a failure.
# Its protocol also requires such a response to carry null usage and
# ``cost: {"available": false}`` -- a failed broker response that claimed usage
# is refused at the adapter -- so "provider completed a failure" and "the
# provider metered nothing for it" are the same statement.
_PROVIDER_COMPLETED_FAILURE_PROOF = {
    "authority": "openclaw-model-adapter",
    "state": "provider_completed_failure",
    "version": "0.1",
}
# Codes that mean the provider refused the request at its gate. A 429 does not
# reach a token: nothing was generated, so nothing was billed. This is not an
# inference about metering, it is what rate limiting *is*.
_NO_CHARGE_FAILURE_NEEDLES: tuple[str, ...] = (
    "RATE_LIMIT", "RATE_LIMITED", "TOO_MANY_REQUESTS", "THROTTLED",
    "QUOTA_EXCEEDED", "RESOURCE_EXHAUSTED",
)


def _billable_usage(invocation: Any) -> bool:
    """Whether this invocation's telemetry reports anything that could be billed.

    ``True`` only when the provider positively reported tokens or a cost. An
    absent or all-null usage block is *not* evidence of a charge; it is the
    shape the broker is required to return for a failed call.
    """

    usage = dict(getattr(invocation, "usage", {}) or {})
    for key in ("input_tokens", "output_tokens", "total_tokens",
                "cache_read_tokens", "cache_write_tokens"):
        value = usage.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            return True
    raw = usage.get("raw_provider_telemetry")
    cost = raw.get("cost") if isinstance(raw, Mapping) else None
    if isinstance(cost, Mapping) and cost.get("available") is True:
        amount = cost.get("usd")
        if isinstance(amount, (int, float)) and not isinstance(amount, bool):
            return float(amount) > 0
    return False


def no_charge_reason(invocation: Any, envelope: Any) -> str | None:
    """Why this failed call may be settled at zero, or ``None`` to keep charging.

    Two proofs, and no guesses. Either the provider refused at its gate -- a
    rate limit, which by definition generated nothing -- or the broker states
    the provider completed a failure and reports no usage and no cost for it,
    which is the only shape its protocol permits for such a response.

    Everything else keeps the conservative rule: once bytes may have left and
    we cannot tell what happened to them, the attempt's reservation stands.
    That asymmetry is the point. 2026-09-16: six hours of 100% HTTP 429 on
    ``profile:gpt-6-astra`` moved 286 USD through the day ledger against zero
    served calls, exhausted every pool, and refused every other lane. The
    provider charged none of it.
    """

    error = getattr(envelope, "error", None)
    error = error if isinstance(error, Mapping) else {}
    metadata = getattr(envelope, "metadata", None)
    metadata = metadata if isinstance(metadata, Mapping) else {}
    code = str(error.get("code") or "").upper()
    digits = "".join(character for character in code if character.isdigit())
    if any(needle in code for needle in _NO_CHARGE_FAILURE_NEEDLES) or (
        len(digits) == 3 and digits == "429"
    ):
        return "provider_rate_limited"
    if (metadata.get("dispatch_proof") == _PROVIDER_COMPLETED_FAILURE_PROOF
            and not _billable_usage(invocation)):
        return "provider_completed_without_usage"
    return None


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
        admission = _admit_through_lock(lambda: budget.admit(
            policy_version_id=policy_version_id,
            day=day,
            work_order_ref=work_order_ref,
            attempt_number=attempt_number,
            phase=phase,
            route_decision_ref=route_decision_ref,
            reserved_micros=reserved_micros,
            mission_binding=mission_binding,
        ), work_order_ref)
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


class _ReleaseLeaseOnError:
    """Own one claimed attempt's lease until a completion of it commits.

    Every normal path through :meth:`CockpitModel.call` completes its attempt
    before it returns or raises, and does so through :meth:`complete`, which
    retries a locked scheduler for up to ``LEASE_RELEASE_RETRY_SECONDS``.  If
    it still cannot commit, the exact completion is written to the lease-holder
    registry (:mod:`.lease_holder_registry`) before the lock error propagates,
    so the next ask of the same request commits it on the holder's behalf
    instead of waiting 2h10m for the lease to expire.

    This is also the backstop for the paths nobody wrote down: if anything
    escapes while the attempt is still leased to this call, the attempt is
    completed ``retryable`` with a control-plane envelope whose dispatch state
    is ``unknown`` -- the same outcome the lease's expiry would produce two
    hours later, produced now, so the next ask of the same request gets a
    fresh attempt instead of "this request is already running".  That
    completion goes through the same bounded retry and the same durable
    record; 2026-09-24 it only printed its own ``database is locked`` and the
    lease stayed held.  The original exception always propagates.
    """

    def __init__(self, scheduler: Scheduler, work: WorkOrder, attempt: int,
                 lease: Mapping[str, Any],
                 holders: LeaseHolderRegistry | None = None) -> None:
        self.scheduler = scheduler
        self.work = work
        self.attempt = attempt
        self.lease = lease
        self.holders = holders or LeaseHolderRegistry(getattr(scheduler, "path", ":memory:"))
        self.lease_revision_ref = str((lease.get("lease") or {}).get("id") or "")
        # Set once a completion committed or was handed to the registry: the
        # lease is then no longer this call's to release.
        self.settled = False

    def __enter__(self) -> "_ReleaseLeaseOnError":
        if self.lease_revision_ref:
            self.holders.record(
                work_order_id=self.work.id, attempt_number=self.attempt,
                lease_revision_ref=self.lease_revision_ref, owner_ref=WORKER_REF)
        return self

    def complete(self, result: ResultEnvelope, *, idempotency_key: str,
                 retry_at: datetime | None = None) -> dict[str, Any]:
        """``scheduler.complete`` for this attempt, surviving a locked file."""

        def attempt() -> dict[str, Any]:
            return self.scheduler.complete(
                self.work.id, self.attempt, WORKER_REF, self.lease["lease_token"],
                result, idempotency_key=idempotency_key, retry_at=retry_at)

        try:
            completion = retry_on_sqlite_lock(
                attempt, deadline_seconds=LEASE_RELEASE_RETRY_SECONDS,
                sleep=_lock_retry_sleep,
                on_retry=lambda n, exc, wait: print(
                    f"cockpit-model: completing {self.work.id} attempt {self.attempt} "
                    f"hit {exc}; retry {n} in {wait:.2f}s", file=sys.stderr))
        except sqlite3.OperationalError as exc:
            if not is_sqlite_lock_error(exc):
                raise
            self._abandon(result, idempotency_key=idempotency_key,
                          retry_at=retry_at, error=exc)
            raise
        self.settled = True
        if self.lease_revision_ref:
            self.holders.release(self.work.id, self.lease_revision_ref)
        return completion

    def _abandon(self, result: ResultEnvelope, *, idempotency_key: str,
                 retry_at: datetime | None, error: BaseException) -> None:
        self.settled = True
        database = sqlite_failure_location(error.__traceback__) or getattr(
            self.scheduler, "path", None)
        digest = traceback_digest(error)
        recorded = self.lease_revision_ref and self.holders.abandon(
            work_order_id=self.work.id, attempt_number=self.attempt,
            lease_revision_ref=self.lease_revision_ref, owner_ref=WORKER_REF,
            lease_token=self.lease["lease_token"],
            result_envelope=result.to_dict(), idempotency_key=idempotency_key,
            retry_at=None if retry_at is None else retry_at.isoformat(),
            error=f"{type(error).__name__}: {error}",
            database_path=database, traceback=digest)
        print(
            f"cockpit-model: gave up completing {self.work.id} attempt "
            f"{self.attempt} after {LEASE_RELEASE_RETRY_SECONDS:.0f}s of "
            f"{type(error).__name__}: {error} (database {database}); "
            + ("the completion is recorded for the next ask to commit"
               if recorded else "no durable record could be written; the lease "
               "is held until it expires"),
            file=sys.stderr,
        )

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        if exc_type is None or self.settled:
            return False
        try:
            status = retry_on_sqlite_lock(
                lambda: self.scheduler.status(self.work.id),
                deadline_seconds=LEASE_RELEASE_RETRY_SECONDS, sleep=_lock_retry_sleep)
            if (status["state"] != "leased"
                    or status["attempt_number"] != self.attempt):
                if self.lease_revision_ref:
                    self.holders.release(self.work.id, self.lease_revision_ref)
                return False
            envelope = _failure(
                self.work, "COCKPIT_ATTEMPT_ABANDONED", None,
                message=_abandon_message(exc_type, exc, tb),
                status="retryable", dispatch_state="unknown",
                diagnostics=_failure_diagnostics(exc, tb),
            )
            self.complete(
                envelope,
                idempotency_key=f"cockpit-abandon:{self.work.id}:{self.attempt}",
            )
        except Exception as release_error:  # noqa: BLE001 - never mask the cause
            print(
                f"cockpit-model: could not release {self.work.id} attempt "
                f"{self.attempt} after {exc_type.__name__}: "
                f"{type(release_error).__name__}: {release_error}",
                file=sys.stderr,
            )
        return False


# _call_once's answer when it freed an attempt whose holder is gone: ask again.
_RECLAIMED_LEASE = "_reclaimed_orphaned_lease"
# When this process started running cockpit work -- taken at import, which is
# no earlier than the process itself, so it can only err towards "younger".
_PROCESS_STARTED_AT = datetime.now(timezone.utc)
# How long before this process started an unrecorded lease must have been
# claimed.  Every holder since the 2026-09-24b release records itself right
# after its claim (milliseconds); this is the slack for that write.
_UNRECORDED_LEASE_MARGIN_SECONDS = 5.0


def _lock_retry_sleep(seconds: float) -> None:
    """Indirection so a test can run the bounded retry without waiting."""

    time.sleep(seconds)


def _failure_diagnostics(exc: BaseException | None, tb: Any) -> dict[str, Any] | None:
    """Which database and which frames an escaped error came from."""

    if exc is None:
        return None
    diagnostics: dict[str, Any] = {
        "exception_type": type(exc).__name__,
        "traceback": traceback_digest(exc),
    }
    database = sqlite_failure_location(tb)
    if database is not None:
        diagnostics["database_path"] = database
    return diagnostics


def _abandon_message(exc_type: Any, exc: BaseException | None, tb: Any) -> str:
    message = f"{exc_type.__name__}: {exc}"
    database = sqlite_failure_location(tb)
    if database is not None:
        message += f" [database: {database}]"
    return message


def _reclaim_orphaned_lease(scheduler: Scheduler, work: WorkOrder) -> str | None:
    """Free the current attempt of ``work`` if its holder is proved gone.

    Called only when a claim found the attempt already leased.  Two proofs
    are accepted, both from :mod:`.lease_holder_registry`:

    * the holder recorded an abandoned completion -- it is committed now,
      verbatim, under the holder's own lease token and idempotency key, which
      is exactly the completion the holder would have committed itself;
    * the holder's process no longer exists -- the attempt is expired through
      ``Scheduler.expire_orphaned_lease``, the same transition lease expiry
      makes, as a compare-and-set on the exact claimed lease revision.

    Returns what was done, or ``None`` when nothing could be proved and the
    request stays "already running".
    """

    holders = LeaseHolderRegistry(getattr(scheduler, "path", ":memory:"))
    for record in holders.holders_for(work.id):
        reason = holder_gone(record)
        if reason is None:
            continue
        revision = str(record.get("lease_revision_ref") or "")
        attempt = record.get("attempt_number")
        if not revision or not isinstance(attempt, int):
            continue
        pending = record.get("pending_completion") if reason == "abandoned" else None
        if isinstance(pending, Mapping):
            try:
                completion = retry_on_sqlite_lock(
                    lambda: scheduler.complete(
                        work.id, attempt, str(record.get("owner_ref") or WORKER_REF),
                        str(pending["lease_token"]), pending["result_envelope"],
                        idempotency_key=str(pending["idempotency_key"]),
                        retry_at=pending.get("retry_at")),
                    deadline_seconds=LEASE_RELEASE_RETRY_SECONDS,
                    sleep=_lock_retry_sleep)
            except LeaseRejected:
                # Not (or no longer) the current leased attempt: it expired,
                # or a sweep got there first. Either way it is not held.
                holders.release(work.id, revision)
                continue
            holders.release(work.id, revision)
            if completion.get("status") in {"fresh", "duplicate"}:
                print(f"cockpit-model: committed the abandoned completion of "
                      f"{work.id} attempt {attempt}", file=sys.stderr)
                return "committed_abandoned_completion"
            continue
        outcome = retry_on_sqlite_lock(
            lambda: scheduler.expire_orphaned_lease(
                work.id, attempt, revision, reason=reason),
            deadline_seconds=LEASE_RELEASE_RETRY_SECONDS, sleep=_lock_retry_sleep)
        holders.release(work.id, revision)
        if outcome["status"] == "expired":
            print(f"cockpit-model: expired {work.id} attempt {attempt}; its holder "
                  f"is gone ({reason})", file=sys.stderr)
            return f"expired_orphaned_lease:{reason}"
    if holders.enabled and not holders.holders_for(work.id):
        return _reclaim_unrecorded_lease(scheduler, work)
    return None


def _reclaim_unrecorded_lease(scheduler: Scheduler, work: WorkOrder,
                              *, now: datetime | None = None,
                              started_at: datetime | None = None) -> str | None:
    """Expire a lease claimed by a release that never recorded its holders.

    2026-09-25: the release before 2026-09-24b claimed without writing the
    holder registry, so a lease it left behind at the release switch could
    only wait out its whole frozen lifetime -- nothing proved its holder gone.
    Three facts together prove it here, and each rules out a live holder:

    * no holder record exists for the work at all -- every process of this
      release records one right after its claim;
    * the lease was claimed before this process started (with a margin for
      that record to be written), so it is not a claim racing this one;
    * it has been held longer than one call of it could take -- the
      WorkOrder's timeout, the completion grace and the bounded
      locked-completion retry -- so it is not a claim made in the moments
      around this process's start.

    The release switch restarts the writer, so the old holder is gone.  The
    one case this can misjudge is an old-release child still walking a long
    fallback chain after the switch: its late completion is then refused, as
    after any expiry, and the work is asked again -- one call's cost, against
    the lease's whole frozen lifetime (2h10m live) of "already running".
    """

    clock = getattr(scheduler, "_now", None)
    now = now or (clock() if callable(clock) else datetime.now(timezone.utc))
    started_at = started_at or _PROCESS_STARTED_AT
    event = scheduler.connection.execute(
        "SELECT * FROM scheduler_attempt_events WHERE work_order_id=? "
        "ORDER BY event_seq DESC LIMIT 1", (work.id,),
    ).fetchone()
    if event is None or event["state"] != "leased" or not event["lease_revision_id"]:
        return None
    if str(event["reason"] or "").startswith("operator_model_recovery_reserved:"):
        return None
    try:
        claimed_at = datetime.fromisoformat(str(event["created_at"]))
    except ValueError:
        return None
    if claimed_at.tzinfo is None:
        return None
    if claimed_at > started_at - timedelta(seconds=_UNRECORDED_LEASE_MARGIN_SECONDS):
        return None
    maximum = work.budget.get("max_seconds")
    if isinstance(maximum, bool) or not isinstance(maximum, (int, float)) or maximum <= 0:
        return None
    longest = float(maximum) + _LEASE_GRACE_SECONDS + LEASE_RELEASE_RETRY_SECONDS
    if (now - claimed_at).total_seconds() <= longest:
        return None
    attempt = int(event["attempt_number"])
    revision = str(event["lease_revision_id"])
    outcome = retry_on_sqlite_lock(
        lambda: scheduler.expire_orphaned_lease(
            work.id, attempt, revision, reason="unrecorded_before_process_start"),
        deadline_seconds=LEASE_RELEASE_RETRY_SECONDS, sleep=_lock_retry_sleep)
    if outcome["status"] == "expired":
        print(f"cockpit-model: expired {work.id} attempt {attempt}; it was claimed at "
              f"{claimed_at.isoformat()} by a process that recorded no holder, before "
              f"this one started", file=sys.stderr)
        return "expired_orphaned_lease:unrecorded_before_process_start"
    return None


def _admit_through_lock(admit: Callable[[], Any], work_order_ref: str) -> Any:
    """A day-ledger admission that waits out a locked budget file.

    2026-09-25 06:19: the planner's admission hit the budget database's
    ``BEGIN IMMEDIATE`` busy timeout (30 s) while another writer held it, and
    the call failed outright.  An admission is keyed on (WorkOrder, attempt,
    phase) and replays as the same admission, and a lock error at BEGIN or
    COMMIT leaves nothing written, so it is retried like the scheduler's own
    completion, for the same bounded time.
    """

    return retry_on_sqlite_lock(
        admit, deadline_seconds=LEASE_RELEASE_RETRY_SECONDS, sleep=_lock_retry_sleep,
        on_retry=lambda n, exc, wait: print(
            f"cockpit-model: admitting {work_order_ref} to the day budget hit {exc}; "
            f"retry {n} in {wait:.2f}s", file=sys.stderr))


def _settle_without_losing_the_lease(budget: Any, admission: Mapping[str, Any], *,
                                     actual_micros: int) -> dict[str, Any] | None:
    """Settle, and never let the ledger stop the Scheduler completion after it.

    2026-09-24: a settlement that raised used to escape before
    ``scheduler.complete``, so the attempt's lease stayed held for its whole
    frozen lifetime (2h10m) and every re-ask of the same request was told
    "this request is already running".  An overrun no longer raises (the
    ledger books it and alerts); anything else the ledger refuses leaves the
    admission *open*, which keeps charging its full reservation -- the same
    conservative state a crash between the call and the settlement leaves --
    and is reported here instead of being allowed to strand the lease.
    """

    try:
        # A locked ledger is waited out (bounded) rather than left open: an
        # open admission keeps charging its whole reservation to the day.
        return retry_on_sqlite_lock(
            lambda: settle_day_ledger(budget, admission, actual_micros=actual_micros),
            deadline_seconds=LEASE_RELEASE_RETRY_SECONDS, sleep=_lock_retry_sleep)
    except (ThesisImpactBudgetError, sqlite3.Error) as exc:
        print(
            "cockpit-model: settlement of "
            f"{admission.get('admission_id')} for {actual_micros} micros was not "
            f"recorded ({type(exc).__name__}: {exc}); the reservation stays open "
            "and the attempt is completed anyway",
            file=sys.stderr,
        )
        return None


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
               model_spec_request_identity: Mapping[str, Any] | None = None,
               structured_output_repair: Mapping[str, Any] | None = None,
               transport_retry: Mapping[str, Any] | None = None,
               provider_retry: Mapping[str, Any] | None = None,
               broker_frame_policy: Mapping[str, Any] | None = None,
               producer_route_decision_refs: Sequence[str] = (),
               authority_binding: Mapping[str, Any] | None = None) -> WorkOrder:
    if purpose not in _PURPOSES:
        raise CockpitModelError("unknown cockpit model purpose")
    if transport_retry is not None:
        from .document_extraction import validate_transport_retry
        if purpose != "model_spec" or (
            (model_spec_request_identity is None)
            == (structured_output_repair is None)
        ):
            raise CockpitModelError("transport retry authority requires bound model specification work")
        transport_retry = validate_transport_retry(transport_retry)
    if len(prompt.encode("utf-8")) > max_input_tokens:
        raise CockpitModelError("the question and its context exceed the model input bound")
    identity = {"identity_version": IDENTITY_VERSION,
                "purpose": purpose, "request_id": request_id,
                "mission_version_ref": mission_version_ref,
                "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest()}
    if budget_identity is not None:
        identity["budget_fingerprint"] = budget_identity
    if authority_binding is not None:
        identity["authority_binding"] = content_hash(authority_binding)
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
    if model_spec_request_identity is not None:
        identity["model_spec_request_identity_hash"] = content_hash(
            model_spec_request_identity
        )
    if structured_output_repair is not None:
        identity["structured_output_repair_hash"] = content_hash(
            structured_output_repair
        )
    if provider_retry is not None:
        from .provider_retry import validate_provider_retry
        provider_retry = validate_provider_retry(provider_retry)
        identity["provider_retry"] = content_hash(provider_retry)
    if broker_frame_policy is not None:
        expected_frame = broker_frame_execution_binding(broker_frame_policy)
        if dict(broker_frame_policy) != expected_frame:
            raise CockpitModelError("broker frame policy is invalid")
        identity["broker_frame_policy"] = content_hash(expected_frame)
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
                                 **({} if authority_binding is None else {
                                     "authority_binding": dict(authority_binding)}),
                                 **({} if not producer_route_decision_refs else {
                                     "producer_route_decision_refs": list(producer_route_decision_refs)}),
                                 **({} if request_identity is None else {
                                     "request_identity": dict(request_identity)}),
                                 **({} if broker_frame_policy is None else {
                                     "broker_frame_policy": dict(broker_frame_policy)}),
                                 **({} if model_spec_request_identity is None else {
                                     "model_spec_request_identity": dict(
                                         model_spec_request_identity)}),
                                 **({} if structured_output_repair is None else {
                                     "structured_output_repair": dict(
                                         structured_output_repair)}),
                                 **({} if provider_retry is None else {
                                     "provider_retry": dict(provider_retry)}),
                                 **({} if transport_retry is None else {
                                     "transport_retry": dict(transport_retry)}),
                                 **({} if verifier_provider_contract is None else {
                                     "verifier_output_schema_version": "0.1",
                                     "verifier_provider_contract": verifier_provider_contract,
                                     "verifier_provider_schema_hash": verifier_provider_schema_hash,
                                 })},
    )


def _preserve_legacy_model_spec_transport_work(
    scheduler: Scheduler, expected: WorkOrder,
) -> WorkOrder:
    """Keep an exact pre-metadata Work immutable, without changing its identity.

    The request suffix already binds this policy. New Work stores its source
    policy for independent audit; an existing Work must match every other byte.
    Scheduler still performs the atomic id/hash comparison at enqueue, including
    if another process inserts a different wire after this read.
    """
    if "transport_retry" not in expected.metadata:
        return expected
    try:
        authority = scheduler.work_order_authority(expected.id)
    except SchedulerConflict as exc:
        raise CockpitModelError("model specification Work authority drifted") from exc
    if authority is None:
        return expected
    wire = expected.to_dict()
    existing = authority["work_order"]
    if canonical_json(existing) == canonical_json(wire):
        return expected
    del wire["metadata"]["transport_retry"]
    if canonical_json(existing) != canonical_json(wire):
        raise CockpitModelError("model specification Work differs from this request")
    return WorkOrder.from_dict(existing)


def _validate_structured_output_repair_authority(
    scheduler: Scheduler, binding: Mapping[str, Any], child_work: WorkOrder,
    config: Mapping[str, Any],
) -> None:
    """Prove both named parent results before admitting a repair Work."""

    child_metadata = child_work.metadata
    expected_mission = (
        child_metadata.get("mission_version_ref"),
        child_metadata.get("mission_version_hash"),
    )
    if (
        child_metadata.get("purpose") != "model_spec"
        or child_metadata.get("control_plane") != "cockpit"
        or child_work.input_refs
        or expected_mission[0] is None
        or expected_mission[1] is None
        or child_metadata.get("structured_output_repair") != binding
    ):
        raise CockpitModelError("structured output repair child binding is invalid")
    proved: dict[str, tuple[dict[str, Any], str]] = {}
    for name in ("root_original", "repair_parent"):
        proof = binding.get(name)
        if not isinstance(proof, Mapping):
            raise CockpitModelError("structured output repair authority is invalid")
        work_ref = proof.get("work_order_ref")
        work = scheduler.work_order_authority(work_ref)
        formal_row = scheduler.connection.execute(
            "SELECT * FROM scheduler_formal_results WHERE work_order_id=?",
            (work_ref,),
        ).fetchone()
        if work is None or formal_row is None or formal_row["terminal_state"] != "succeeded":
            raise CockpitModelError("structured output repair parent is not a succeeded Work")
        try:
            envelope = ResultEnvelope.from_dict(
                json.loads(formal_row["result_envelope_json"])
            ).to_dict()
        except Exception as exc:
            raise CockpitModelError(
                "structured output repair result authority is invalid"
            ) from exc
        formal_record = {
            "id": formal_row["result_record_id"],
            "work_order_id": formal_row["work_order_id"],
            "attempt_number": formal_row["attempt_number"],
            "result_envelope_id": formal_row["result_envelope_id"],
            "result_envelope_hash": formal_row["result_envelope_hash"],
            "terminal_state": formal_row["terminal_state"],
            "created_at": formal_row["created_at"],
        }
        receipt = scheduler.connection.execute(
            "SELECT * FROM scheduler_result_envelopes WHERE result_envelope_id=?",
            (formal_row["result_envelope_id"],),
        ).fetchone()
        if receipt is None:
            raise CockpitModelError("structured output repair result authority is invalid")
        parent_work = work["work_order"]
        parent_metadata = parent_work.get("metadata") or {}
        text = envelope.get("outputs", {}).get("text")
        if (
            proof.get("work_order_hash") != work.get("work_order_hash")
            or formal_row["work_order_id"] != work_ref
            or envelope["work_order_ref"] != work_ref
            or envelope["id"] != formal_row["result_envelope_id"]
            or canonical_json(envelope) != formal_row["result_envelope_json"]
            or content_hash(envelope) != formal_row["result_envelope_hash"]
            or content_hash(formal_record) != formal_row["content_hash"]
            or receipt["work_order_id"] != work_ref
            or receipt["result_envelope_hash"] != formal_row["result_envelope_hash"]
            or receipt["outcome"] != "succeeded"
            or proof.get("result_envelope_ref") != formal_row["result_envelope_id"]
            or proof.get("result_envelope_hash") != formal_row["result_envelope_hash"]
            or proof.get("invocation_ref") != envelope.get("invocation_ref")
            or proof.get("route_decision_ref")
               != (envelope.get("metadata") or {}).get("route_decision_ref")
            or parent_metadata.get("purpose") != "model_spec"
            or parent_metadata.get("control_plane") != "cockpit"
            or parent_work.get("input_refs") != []
            or (
                parent_metadata.get("mission_version_ref"),
                parent_metadata.get("mission_version_hash"),
            ) != expected_mission
            or not isinstance(text, str)
        ):
            raise CockpitModelError("structured output repair parent authority drifted")
        proved[name] = (parent_work, text)

    root_work, root_text = proved["root_original"]
    parent_work, parent_text = proved["repair_parent"]
    from .company_model_cli import (
        _repair_prompt,
        validate_model_spec_request_identity,
    )
    from .company_model_spec import CompanyModelSpecError
    try:
        root_identity = validate_model_spec_request_identity(
            root_work["metadata"]["model_spec_request_identity"]
        )
    except (KeyError, TypeError, ValueError, CockpitModelError) as exc:
        raise CockpitModelError(
            "structured output repair root request identity is invalid"
        ) from exc
    if (
        root_identity["state_hash"] != binding.get("state_hash")
        or root_identity["task_hash"] != binding.get("task_hash")
        or root_identity["structured_output_repair"] != binding.get("repair_config")
        or hashlib.sha256(root_text.encode("utf-8")).hexdigest()
           != binding.get("original_text_sha256")
        or hashlib.sha256(parent_text.encode("utf-8")).hexdigest()
           != binding.get("parent_text_sha256")
    ):
        raise CockpitModelError("structured output repair semantic authority drifted")
    _validate_model_spec_request_namespace(
        root_work["metadata"].get("request_id", ""),
        content_hash(root_identity)[:32], config,
    )
    number = binding["repair_number"]
    root_proof = binding["root_original"]
    parent_proof = binding["repair_parent"]
    if number == 1:
        if parent_proof != root_proof:
            raise CockpitModelError("first structured output repair parent is not its root")
    else:
        parent_binding = (parent_work.get("metadata") or {}).get(
            "structured_output_repair"
        )
        if (
            not isinstance(parent_binding, Mapping)
            or parent_binding.get("root_original") != root_proof
            or parent_binding.get("state_hash") != binding.get("state_hash")
            or parent_binding.get("task_hash") != binding.get("task_hash")
            or parent_binding.get("repair_config") != binding.get("repair_config")
            or parent_binding.get("repair_number") != number - 1
        ):
            raise CockpitModelError("structured output repair ancestry is not contiguous")
        parent_base = "model-spec-repair:" + content_hash(parent_binding)[:32]
        _validate_model_spec_request_namespace(
            parent_work["metadata"].get("request_id", ""), parent_base, config,
        )
    error = CompanyModelSpecError(
        binding["validation_error"]["message"],
        code=binding["validation_error"]["code"],
    )
    if child_work.question != _repair_prompt(parent_text, error):
        raise CockpitModelError("structured output repair prompt differs from its parent")


def _validate_model_spec_request_namespace(
    request_id: str, base_request_id: str, config: Mapping[str, Any],
) -> None:
    """Accept only Cockpit's versioned policy/recovery decorations."""

    if request_id == base_request_id:
        return
    decorated = base_request_id
    if "capacity_retry" in config:
        decorated += ":capacity-policy:" + content_hash(
            _capacity_retry(config)
        )[:16]
    if "transport_retry" in config:
        decorated += ":transport-policy:" + content_hash(
            config["transport_retry"]
        )[:16]
    recovery = (
        r"(?::operator-recovery:[0-9a-f]{16}"
        r"|:route-admission:[0-9a-f]{64}"
        r"|:capacity-recovery:[0-9]+:[0-9a-f]{16})?"
    )
    remainder = request_id.removeprefix(decorated)
    if not request_id.startswith(decorated) or re.fullmatch(recovery, remainder) is None:
        raise CockpitModelError("model specification request namespace is invalid")


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
        "capacity_retry": _capacity_retry(config),
        **(
            {"broker_max_frame_bytes": resolve_broker_max_frame_bytes(config)}
            if "broker_max_frame_bytes" in config
            else {}
        ),
        **(
            {"transport_retry": dict(config["transport_retry"])}
            if "transport_retry" in config
            else {}
        ),
        **(
            {"provider_retry": dict(config["provider_retry"])}
            if "provider_retry" in config
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
        "capacity_retry", "transport_retry", "provider_retry",
        "broker_max_frame_bytes",
    }:
        raise ValueError("Dossier request identity config is invalid")
    if "broker_max_frame_bytes" in exact:
        try:
            frame_bytes = resolve_broker_max_frame_bytes(exact)
        except Exception as exc:
            raise ValueError("Dossier broker frame identity is invalid") from exc
        if exact["broker_max_frame_bytes"] != frame_bytes:
            raise ValueError("Dossier broker frame identity is invalid")
    if "transport_retry" in exact:
        from .document_extraction import validate_transport_retry

        validate_transport_retry(exact["transport_retry"])
    if "provider_retry" in exact:
        from .provider_retry import validate_provider_retry

        validate_provider_retry(exact["provider_retry"])
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


def _dossier_capacity_epoch(scheduler: Scheduler, identity: Mapping[str, Any]) -> int:
    """Read the complete recovery ancestry and enforce one monotonic epoch series."""

    semantic = str(identity["semantic_request_id"])
    producer_refs = tuple(identity["producer_route_decision_refs"])
    exact_config = identity["exact_config"]
    capacity = exact_config.get("capacity_retry") or _capacity_retry({})
    policy_hash = content_hash(capacity)[:16]
    epochs: list[int] = []
    seen: set[str] = set()
    current = dict(identity)
    while current.get("recovery_parent") is not None:
        parent = current["recovery_parent"]
        work_ref = parent["work_order_ref"]
        if work_ref in seen:
            raise CockpitModelError("Dossier recovery ancestry contains a cycle")
        seen.add(work_ref)
        if len(seen) > 32:
            raise CockpitModelError("Dossier recovery ancestry is too deep")
        if parent["kind"] == "capacity":
            match = re.fullmatch(
                r"capacity-recovery:([1-9][0-9]*):([0-9a-f]{16})",
                parent["proof_ref"],
            )
            if match is None or match.group(2) != policy_hash:
                raise CockpitModelError("Dossier capacity recovery proof drifted")
            epochs.append(int(match.group(1)))
        try:
            authority = scheduler.work_order_authority(work_ref)
        except SchedulerConflict as exc:
            raise CockpitModelError("Dossier recovery parent authority drifted") from exc
        metadata = ((authority or {}).get("work_order") or {}).get("metadata") or {}
        parent_identity = metadata.get("request_identity")
        if parent_identity is None:
            legacy_request = semantic
            if producer_refs:
                legacy_request += ":producer:" + content_hash(list(producer_refs))[:16]
            if (
                exact_config != {"capacity_retry": _capacity_retry({})}
                or metadata.get("request_id") != legacy_request
            ):
                raise CockpitModelError("Dossier legacy recovery root drifted")
            break
        try:
            parent_identity = validate_dossier_request_identity(
                parent_identity,
                semantic_request_id=semantic,
                producer_route_decision_refs=producer_refs,
            )
        except (TypeError, ValueError) as exc:
            raise CockpitModelError("Dossier recovery parent identity drifted") from exc
        if (
            canonical_json(parent_identity["exact_config"])
            != canonical_json(exact_config)
            or metadata.get("request_id") != parent_identity["decorated_request_id"]
        ):
            raise CockpitModelError("Dossier recovery parent configuration drifted")
        current = parent_identity
    chronological = list(reversed(epochs))
    if chronological != list(range(1, len(chronological) + 1)):
        raise CockpitModelError("Dossier capacity recovery epochs are not contiguous")
    if len(chronological) > capacity["max_recovery_epochs"]:
        raise CockpitModelError("capacity_recovery_exhausted")
    return len(chronological)


def _validated_dossier_success(
    scheduler: Scheduler,
    expected_work: WorkOrder,
) -> dict[str, Any] | None:
    """Return an exact immutable Dossier success, rejecting corrupt authority."""

    try:
        authority = scheduler.work_order_authority(expected_work.id)
    except SchedulerConflict as exc:
        raise CockpitModelError("Dossier WorkOrder authority drifted") from exc
    if authority is None:
        return None
    expected_wire = expected_work.to_dict()
    if (
        canonical_json(authority["work_order"]) != canonical_json(expected_wire)
        or authority["work_order_hash"] != content_hash(expected_wire)
    ):
        raise CockpitModelError("Dossier WorkOrder differs from this request")
    row = scheduler.connection.execute(
        "SELECT * FROM scheduler_formal_results WHERE work_order_id=?",
        (expected_work.id,),
    ).fetchone()
    if row is None:
        return None
    formal = dict(row)
    try:
        envelope = ResultEnvelope.from_dict(
            json.loads(formal["result_envelope_json"])
        ).to_dict()
    except Exception as exc:
        raise CockpitModelError("Dossier result authority is invalid") from exc
    record = {
        "id": formal["result_record_id"],
        "work_order_id": formal["work_order_id"],
        "attempt_number": formal["attempt_number"],
        "result_envelope_id": formal["result_envelope_id"],
        "result_envelope_hash": formal["result_envelope_hash"],
        "terminal_state": formal["terminal_state"],
        "created_at": formal["created_at"],
    }
    receipt = scheduler.connection.execute(
        "SELECT * FROM scheduler_result_envelopes WHERE result_envelope_id=?",
        (formal["result_envelope_id"],),
    ).fetchone()
    receipt_body = None if receipt is None else {
        "result_envelope_id": receipt["result_envelope_id"],
        "work_order_id": receipt["work_order_id"],
        "attempt_number": receipt["attempt_number"],
        "result_envelope_hash": receipt["result_envelope_hash"],
        "outcome": receipt["outcome"],
        "created_at": receipt["created_at"],
    }
    if (
        formal["terminal_state"] != "succeeded"
        or formal["work_order_id"] != expected_work.id
        or envelope["work_order_ref"] != expected_work.id
        or envelope["id"] != formal["result_envelope_id"]
        or canonical_json(envelope) != formal["result_envelope_json"]
        or content_hash(envelope) != formal["result_envelope_hash"]
        or content_hash(record) != formal["content_hash"]
        or receipt is None
        or canonical_json(envelope) != receipt["result_envelope_json"]
        or receipt["result_envelope_hash"] != formal["result_envelope_hash"]
        or receipt["outcome"] != "succeeded"
        or content_hash(receipt_body) != receipt["content_hash"]
    ):
        raise CockpitModelError("Dossier success authority drifted")
    return {**formal, "result_envelope": envelope}


def _failure(work: WorkOrder, code: str, route_ref: str | None,
             *, message: str | None = None,
             chain_failures: Sequence[Mapping[str, Any]] = (),
             status: str = "failed",
             dispatch_state: str = "not_started",
             diagnostics: Mapping[str, Any] | None = None) -> ResultEnvelope:
    if dispatch_state not in {"not_started", "unknown"}:
        raise CockpitModelError("failure dispatch state is not recognized")
    identity = {"work_order_ref": work.id, "code": code, "route_ref": route_ref}
    if dispatch_state != "not_started":
        # Preserve existing control-envelope identities byte-for-byte while
        # ensuring a post-call unknown state cannot reuse a not-started id.
        identity["dispatch_state"] = dispatch_state
    invocation_state = "not-started" if dispatch_state == "not_started" else "unavailable"
    metadata = {"control_plane_failure": True, "route_decision_ref": route_ref,
                "chain_failures": list(chain_failures)[:12]}
    if dispatch_state != "not_started":
        metadata["dispatch_state"] = dispatch_state
    if diagnostics:
        # Which database file and which frames: the exception type alone
        # ("OperationalError: database is locked") names none of the three
        # SQLite files a cockpit call writes.
        metadata["failure_diagnostics"] = dict(diagnostics)
    return ResultEnvelope(
        schema_version=SCHEMA_VERSION, id=f"result:cockpit-control-{content_hash(identity)[:32]}",
        created_at=_now(), work_order_ref=work.id,
        invocation_ref=f"invocation:{invocation_state}:{content_hash(identity)[:32]}", status=status,
        outputs={}, actual_side_effects=(), usage_refs=(), artifact_refs=(),
        error={"code": code, **({} if message is None else {"message": message[:1000]})},
        metadata=metadata,
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


def _proved_capacity_busy_terminal(formal: Mapping[str, Any] | None) -> bool:
    """Require the adapter's closed proof that no provider call was sent."""

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
        and failures[0].get("dispatch_proof") == _LOCAL_NOT_SENT_PROOF
        and failures[0].get("code") in {
            "BUSY", "CONCURRENCY_LIMIT", "BROKER_CONCURRENCY_LIMIT",
            "QUEUE_TIMEOUT", "BROKER_CLOSED"}
    )


def _capacity_busy_terminal(formal: Mapping[str, Any] | None) -> bool:
    return (
        _legacy_broker_busy_failure(formal)
        or _proved_capacity_busy_terminal(formal)
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
                 max_input_tokens: int = 120_000, max_output_tokens: int = 3_000,
                 max_cost_usd: float | None = None,
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
        configured_default = default_call_budget(purpose)
        return resolve_call_budget(self.config, purpose, defaults={
            "max_input_tokens": self.max_input_tokens,
            "max_output_tokens": self.max_output_tokens,
            "max_cost_usd": (configured_default["max_cost_usd"]
                             if self.max_cost_usd is None else self.max_cost_usd),
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
            max_frame_bytes=resolve_broker_max_frame_bytes(config),
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

    def _provider_retry_state(
        self, scheduler: Scheduler, router: ModelRouter, work: WorkOrder
    ) -> dict[str, Any] | None:
        """Reconstruct the last accepted paid-retry decision from authority."""

        policy = self.config.get("provider_retry")
        if policy is None:
            return None
        if work.metadata.get("provider_retry") != policy:
            raise CockpitModelError(
                "the WorkOrder provider retry policy differs from this model config"
            )
        rows = scheduler.connection.execute(
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
                raise CockpitModelError(
                    "the persisted provider retry result is invalid"
                ) from exc
            if (
                canonical_json(wire) != row["result_envelope_json"]
                or content_hash(wire) != row["result_envelope_hash"]
            ):
                raise CockpitModelError(
                    "the persisted provider retry result has drifted"
                )
            metadata = wire.get("metadata") if isinstance(wire, Mapping) else None
            if not isinstance(metadata, Mapping) or "provider_retry_proof" not in metadata:
                continue
            if "provider_retry_state" not in metadata:
                raise CockpitModelError(
                    "the persisted provider retry proof has no route state"
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
            or not all(isinstance(item, str) and item
                       for item in state["excluded_profile_ids"])
            or len(set(state["excluded_profile_ids"]))
               != len(state["excluded_profile_ids"])
            or state.get("retry_profile_version_ref") is not None
               and not isinstance(state["retry_profile_version_ref"], str)
            or isinstance(state.get("same_profile_retries"), bool)
            or not isinstance(state.get("same_profile_retries"), int)
            or state["same_profile_retries"] < 0
        ):
            raise CockpitModelError(
                "the persisted provider retry route state is invalid"
            )
        decisions = router.list_decisions(work_order_id=work.id)
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
            else router.get_profile(selected_version)["id"]
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
               > policy["max_same_profile_retries"]
        ):
            raise CockpitModelError(
                "the persisted provider retry state does not match route history"
            )
        return dict(state)

    def _paid_retry_result(
        self, *, work: WorkOrder, lease: Mapping[str, Any],
        route: Mapping[str, Any], profile: Mapping[str, Any],
        invocation: Any, result: ResultEnvelope,
        state: Mapping[str, Any] | None,
    ) -> ResultEnvelope | None:
        """Turn only a proved returned provider failure into a new attempt."""

        policy = self.config.get("provider_retry")
        if policy is None or state is None:
            return None
        from .provider_retry import returned_provider_failure_proof

        proof = returned_provider_failure_proof(invocation, result)
        if proof is None:
            return None
        used = int(state.get("same_profile_retries", 0))
        excluded = list(state.get("excluded_profile_ids", []))
        if used < policy["max_same_profile_retries"]:
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

    def call(self, *, purpose: str, request_id: str, prompt: str,
             mission: Mapping[str, Any],
             producer_route_decision_refs: Sequence[str] = (),
             _dossier_recovery_parent: Mapping[str, Any] | None = None,
             _model_spec_request_identity: Mapping[str, Any] | None = None,
             _structured_output_repair: Mapping[str, Any] | None = None,
             ) -> dict[str, Any]:
        """Run configured paid retries without retaining prior call contexts."""

        while True:
            outcome = self._call_once(
                purpose=purpose,
                request_id=request_id,
                prompt=prompt,
                mission=mission,
                producer_route_decision_refs=producer_route_decision_refs,
                _dossier_recovery_parent=_dossier_recovery_parent,
                _model_spec_request_identity=_model_spec_request_identity,
                _structured_output_repair=_structured_output_repair,
            )
            if outcome.get(_RECLAIMED_LEASE):
                continue
            if "_provider_retry_backoff_seconds" not in outcome:
                return outcome
            backoff = outcome["_provider_retry_backoff_seconds"]
            if backoff:
                import time
                time.sleep(backoff)

    def call_setup(self, *, request_id: str, prompt: str,
                   planning_context: Mapping[str, Any]) -> dict[str, Any]:
        """Run the first-goal call against an explicit setup authority.

        A blank workspace has no CoverageMission and must not manufacture one
        merely to spend on planning.  The setup context binds the workspace's
        method foundation and its separately configured one-call budget.  The
        scheduler's historical field remains named ``mission_version_ref``;
        for this call it contains the setup authority ref, and metadata marks
        the distinct authority kind.
        """
        context = validate_setup_planning_context(planning_context)
        from .workspace_runtime import validate_runtime_context
        workspace = validate_runtime_context(
            state_dir=Path(self.scheduler_db).parent)
        if workspace is not None:
            if context["workspace_id"] != workspace.workspace_id:
                raise CockpitModelError("setup planning context belongs to another workspace")
            foundation_path = workspace.state_dir / "research-foundation.json"
            try:
                foundation = json.loads(foundation_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                raise CockpitModelError("workspace research foundation is unavailable") from exc
            if (not isinstance(foundation, Mapping)
                    or foundation.get("content_hash") != context["foundation_hash"]):
                raise CockpitModelError("setup planning foundation differs from workspace authority")
        scope = {
            "id": context["setup_ref"],
            "mission_ref": f"workspace-setup:{context['workspace_id']}",
            "content_hash": context["content_hash"],
            "created_at": context["created_at"],
            "budget": context["budget"],
            "_authority_kind": "workspace_setup",
            "_foundation_ref": context["foundation_ref"],
            "_foundation_hash": context["foundation_hash"],
        }
        return self.call(
            purpose="goal", request_id=request_id, prompt=prompt, mission=scope)

    def _call_once(self, *, purpose: str, request_id: str, prompt: str,
                   mission: Mapping[str, Any],
                   producer_route_decision_refs: Sequence[str] = (),
                   _dossier_recovery_parent: Mapping[str, Any] | None = None,
                   _model_spec_request_identity: Mapping[str, Any] | None = None,
                   _structured_output_repair: Mapping[str, Any] | None = None,
                   ) -> dict[str, Any]:
        """Run one Scheduler attempt and request another through a private marker."""
        capacity_retry = _capacity_retry(self.config)
        producer_refs = tuple(sorted({str(ref) for ref in producer_route_decision_refs}))
        semantic_request_id = request_id
        if _model_spec_request_identity is not None:
            if purpose != "model_spec" or _structured_output_repair is not None:
                raise CockpitModelError("model specification request identity is invalid")
            from .company_model_cli import validate_model_spec_request_identity
            _model_spec_request_identity = validate_model_spec_request_identity(
                _model_spec_request_identity
            )
            expected = content_hash(_model_spec_request_identity)[:32]
            _validate_model_spec_request_namespace(request_id, expected, self.config)
        if _structured_output_repair is not None:
            if purpose != "model_spec" or not isinstance(
                _structured_output_repair, Mapping
            ):
                raise CockpitModelError(
                    "structured output repair binding is invalid"
                )
            from .company_model_cli import (
                validate_structured_output_repair_binding,
            )
            repair_base = "model-spec-repair:" + content_hash(
                _structured_output_repair
            )[:32]
            _validate_model_spec_request_namespace(
                request_id, repair_base, self.config
            )
            _structured_output_repair = validate_structured_output_repair_binding(
                _structured_output_repair, prompt=prompt, request_id=repair_base,
            )
        request_identity = None
        if (
            purpose in _DOSSIER_PURPOSES
            and (
                "capacity_retry" in self.config
                or "transport_retry" in self.config
                or "provider_retry" in self.config
                or "broker_max_frame_bytes" in self.config
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
                    "dossier", "dossier_verifier", "model_spec",
                }
                else None
            ),
            "producer_route_decision_refs": producer_refs,
            "provider_retry": self.config.get("provider_retry"),
            "broker_frame_policy": broker_frame_execution_binding(self.config),
            "model_spec_request_identity": _model_spec_request_identity,
            "structured_output_repair": _structured_output_repair,
            "authority_binding": ({
                "kind": mission["_authority_kind"],
                "foundation_ref": mission["_foundation_ref"],
                "foundation_hash": mission["_foundation_hash"],
            } if mission.get("_authority_kind") == "workspace_setup" else None),
            "transport_retry": (
                self.config.get("transport_retry")
                if purpose == "model_spec" and (
                    _model_spec_request_identity is not None
                    or _structured_output_repair is not None
                ) else None
            ),
        }
        work = build_work(
            request_id=request_id,
            request_identity=request_identity,
            **work_args,
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
        provider = self.config.get("provider_retry") or {}
        provider_attempts = (
            candidates * (int(provider["max_same_profile_retries"]) + 1)
            if provider else 1
        )
        scheduler_attempts = max(
            capacity_retry["scheduler_max_attempts"], provider_attempts
        )
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
                               f"attempts-{scheduler_attempts}-"
                               f"routes-{lease_policy['content_hash'][:16]}-0.1"),
            max_attempts=scheduler_attempts,
            max_lease_seconds=lease_seconds,
            max_total_lease_seconds=lease_seconds * 2,
        ) as scheduler:
            work = _preserve_legacy_model_spec_transport_work(scheduler, work)
            if _structured_output_repair is not None:
                _validate_structured_output_repair_authority(
                    scheduler, _structured_output_repair, work, self.config
                )
            if scheduler.enqueue(work)["status"] == "conflict":
                raise CockpitModelError("this request is bound to different content; ask again")
            formal = scheduler.formal_result(work.id)
            if (
                purpose in _DOSSIER_PURPOSES
                and formal is not None
                and formal.get("terminal_state") == "succeeded"
            ):
                formal = _validated_dossier_success(scheduler, work)
            dossier_bound = request_identity is not None
            dossier_purpose = purpose in _DOSSIER_PURPOSES
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
                if recovery_suffix is None:
                    from .controlled_budget_reentry import (
                        approved_request as approved_budget_request,
                    )
                    recovery_suffix = approved_budget_request(
                        self.scheduler_db, self.config["budget_db"],
                        old_work_order_ref=work.id, formal=formal,
                        mission=mission,
                    )
                if recovery_suffix is not None:
                    if dossier_purpose:
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
                            _model_spec_request_identity=_model_spec_request_identity,
                            _structured_output_repair=_structured_output_repair,
                        )
                    return self.call(
                        purpose=purpose,
                        request_id=base_request_id + recovery_suffix,
                        prompt=prompt,
                        mission=mission,
                        producer_route_decision_refs=producer_refs,
                        _model_spec_request_identity=_model_spec_request_identity,
                        _structured_output_repair=_structured_output_repair,
                    )
            from .model_route_recovery import route_recovery_request
            recovery_request = route_recovery_request(
                formal, work_order_ref=work.id, request_id=base_request_id,
                config=self.config,
            )
            if recovery_request is not None:
                if dossier_purpose:
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
                        _model_spec_request_identity=_model_spec_request_identity,
                        _structured_output_repair=_structured_output_repair,
                    )
                return self.call(
                    purpose=purpose, request_id=recovery_request, prompt=prompt,
                    mission=mission, producer_route_decision_refs=producer_refs,
                    _model_spec_request_identity=_model_spec_request_identity,
                    _structured_output_repair=_structured_output_repair,
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
            if dossier_bound:
                epoch = _dossier_capacity_epoch(scheduler, request_identity)
            else:
                epoch = int(match.group(1)) if match else 0
            capacity_recoverable = (
                _proved_capacity_busy_terminal(capacity_terminal)
                if dossier_purpose
                else _capacity_busy_terminal(capacity_terminal)
            )
            if (capacity_recoverable
                    and epoch < capacity_retry["max_recovery_epochs"]):
                completed_at = datetime.fromisoformat(str(capacity_terminal["created_at"]))
                elapsed = (self.clock().astimezone(timezone.utc)
                           - completed_at.astimezone(timezone.utc)).total_seconds()
                if elapsed >= capacity_retry["cooldown_seconds"]:
                    policy_hash = content_hash(capacity_retry)[:16]
                    if dossier_purpose:
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
                            _model_spec_request_identity=_model_spec_request_identity,
                            _structured_output_repair=_structured_output_repair,
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
                        _model_spec_request_identity=_model_spec_request_identity,
                        _structured_output_repair=_structured_output_repair,
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
                if lease is None and _reclaim_orphaned_lease(scheduler, work) is not None:
                    # The holder was proved gone and its attempt is settled:
                    # its abandoned completion may have been the answer itself,
                    # or a fresh attempt is ready. Ask again from the top so
                    # replay, recovery and claiming all run exactly as usual.
                    return {_RECLAIMED_LEASE: True}
                if lease is None:
                    raise CockpitModelError("this request is already running")
                attempt = lease["attempt"]["attempt_number"]
                # First in the list so it also covers the two stores failing
                # to open, and exits last, after they are closed.
                with _ReleaseLeaseOnError(scheduler, work, attempt, lease) as guard, \
                        ModelRouter(self.config["model_router_db"]) as router, \
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
                        guard.complete(
                            result, idempotency_key=f"cockpit-complete:{work.id}:{attempt}")
                        raise CockpitModelError(
                            "a producer route decision could not prove its model family"
                        ) from exc
                    if any(family.startswith("unclassified:")
                           for family in producer_families):
                        result = _failure(work, "MODEL_ROUTE_REJECTED", None)
                        guard.complete(
                            result, idempotency_key=f"cockpit-complete:{work.id}:{attempt}")
                        raise CockpitModelError("verifier_not_independent")
                    provider_retry_state = self._provider_retry_state(
                        scheduler, router, work
                    )
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
                            provider_retry_state=provider_retry_state,
                        )
                        result = outcome["result"]
                        failure = outcome["failure"]
                        cost_micros, cost_status = outcome["cost_micros"], outcome["cost_status"]
                        if outcome.get("invocation") is not None:
                            paid_retry = self._paid_retry_result(
                                work=work, lease=lease,
                                route=outcome["route"], profile=outcome["profile"],
                                invocation=outcome["invocation"], result=result,
                                state=provider_retry_state,
                            )
                            if paid_retry is not None:
                                result = paid_retry
                        completion = guard.complete(
                            result, idempotency_key=f"cockpit-complete:{work.id}:{attempt}",
                            retry_at=(
                                self.clock() + timedelta(
                                    seconds=self.config["provider_retry"][
                                        "retry_backoff_seconds"])
                                if result.status == "retryable"
                                and result.metadata.get("provider_retry_proof") is not None
                                else None
                            ),
                        )
                        if completion["status"] == "conflict":
                            raise CockpitModelError("the request completion conflicted; ask again")
                        if (
                            result.status == "retryable"
                            and completion["work_state"] == "ready"
                            and result.metadata.get("provider_retry_proof") is not None
                        ):
                            return {
                                "_provider_retry_backoff_seconds":
                                    self.config["provider_retry"][
                                        "retry_backoff_seconds"],
                            }
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
                    prior_routes = router.list_decisions(work_order_id=work.id)
                    route = router.route(
                        work, attempt_number=attempt,
                        capability=work.requested_capabilities[0],
                        policy_version_ref=self.config["routing_policy_ref"],
                        credential_slot_refs=self.config["credential_slot_refs"], required_modalities=("text",),
                        required_context_tokens=prompt_bytes + effective["max_output_tokens"],
                        estimated_input_tokens=prompt_bytes, estimated_output_tokens=effective["max_output_tokens"],
                        idempotency_key=f"cockpit-route:{work.id}:{attempt}",
                        producer_family=next(iter(producer_families), None),
                        decision_kind="initial" if not prior_routes else "retry",
                        previous_decision_ref=(
                            None if not prior_routes else prior_routes[-1]["id"]
                        ),
                        purpose=purpose,
                        excluded_profile_ids=(
                            () if provider_retry_state is None
                            else provider_retry_state["excluded_profile_ids"]
                        ),
                        required_profile_version_ref=(
                            None if provider_retry_state is None
                            else provider_retry_state["retry_profile_version_ref"]
                        ),
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
                                completion = guard.complete(
                                    result,
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
                        paid_retry = None
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
                                        "REQUIRED_CONTROLS_UNAVAILABLE",
                                    } and result.metadata.get(
                                        "dispatch_proof"
                                    ) == _LOCAL_NOT_SENT_PROOF
                                    free_of_charge = no_charge_reason(
                                        invocation, result)
                                    if broker_local_not_sent:
                                        cost_micros, cost_status = 0, "failed"
                                    elif cost_status == "actual":
                                        pass
                                    elif free_of_charge is not None:
                                        # WP-A/A1, single-shot path. Same rule
                                        # as the chained one: a provider that
                                        # refused at its gate, or a broker-
                                        # attested provider-completed failure
                                        # with no usage and no cost, billed
                                        # nothing and must release the whole
                                        # reservation.
                                        cost_micros, cost_status = 0, "failed"
                                    else:
                                        # A provider-completed failure proves a
                                        # paid call happened. It does not prove
                                        # what that call cost. Missing actual
                                        # telemetry therefore retains the full
                                        # reservation for this attempt.
                                        cost_micros, cost_status = reserved, "reserved"
                                    failure = str(error.get("message") or "the model call failed")
                                    if free_of_charge is not None:
                                        failure += f"（未计费：{free_of_charge}）"
                                    paid_retry = self._paid_retry_result(
                                        work=work, lease=lease, route=route,
                                        profile=profile, invocation=invocation,
                                        result=result, state=provider_retry_state,
                                    )
                                    if paid_retry is not None:
                                        result = paid_retry
                            except OpenClawModelAdapterError as exc:
                                result = _failure(work, "MODEL_ADAPTER_REJECTED_OR_FAILED", route["id"])
                                if isinstance(exc, BrokerDefinitelyNotSent):
                                    cost_micros, cost_status = 0, "not_sent"
                                else:
                                    cost_micros, cost_status = reserved, "reserved"
                                failure = f"the model call failed: {exc}"
                            _settle_without_losing_the_lease(
                                budget, admission, actual_micros=cost_micros)
                    completion = guard.complete(
                        result, idempotency_key=f"cockpit-complete:{work.id}:{attempt}",
                        retry_at=(
                            self.clock() + timedelta(
                                seconds=self.config["provider_retry"][
                                    "retry_backoff_seconds"])
                            if result.status == "retryable"
                            and result.metadata.get("provider_retry_proof") is not None
                            else None
                        ))
                    if completion["status"] == "conflict":
                        raise CockpitModelError("the request completion conflicted; ask again")
                    if (
                        result.status == "retryable"
                        and completion["work_state"] == "ready"
                        and result.metadata.get("provider_retry_proof") is not None
                    ):
                        return {
                            "_provider_retry_backoff_seconds":
                                self.config["provider_retry"][
                                    "retry_backoff_seconds"],
                        }
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
        answer = {"text": text, "replayed": replayed, "cost_micros": cost_micros,
                  "cost_status": cost_status, "work_order_ref": work.id,
                  "result_envelope_ref": formal["result_envelope_id"],
                  "invocation_ref": envelope.get("invocation_ref"),
                  "route_decision_ref": envelope.get("metadata", {}).get("route_decision_ref")}
        if work.metadata.get("purpose") == "model_spec":
            answer.update({
                "work_order_hash": content_hash(work.to_dict()),
                "result_envelope_hash": formal["result_envelope_hash"],
            })
        return answer

    def _chain_tier(self, router: ModelRouter, purpose: str) -> str | None:
        """The tier to walk, or None to route the single-shot way.

        A chain runs only when the *pinned policy version* declares one. That
        keeps the choice where every other routing choice already is -- in the
        version a lane pinned -- and it means an installation that has not been
        repointed at a tier keeps behaving exactly as it did.
        """

        from .model_fallback_chain import purpose_tiers
        from .model_router import declared_tier_chain

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
        # ``declared_tier_chain`` rather than ``tier in declared``: a tier
        # split out of another one after this policy version was written is
        # declared by the version it inherits from, and a stage must not fall
        # back to single-shot routing on the day its tier was named.
        return (tier if declared_tier_chain(policy, tier) is not None
                or override is not None else None)

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
            ceiling = max(ceiling, profile_call_ceiling_usd(
                profile, prompt_bytes=prompt_bytes,
                max_output_tokens=call_budget["max_output_tokens"]))
        if ceiling <= 0:
            ceiling = Decimal(str(call_budget["max_cost_usd"]))
        return int((ceiling * 1_000_000).quantize(Decimal("1"), rounding=ROUND_HALF_UP))

    def _chained(self, router: ModelRouter, budget: Any, *, work: WorkOrder, purpose: str,
                 tier: str, attempt: int, prompt_bytes: int,
                 scope: Mapping[str, Any],
                 call_budget: Mapping[str, Any],
                 producer_route_decision_refs: Sequence[str] = (),
                 provider_retry_state: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """Walk the tier's chain under one reservation, retaining uncertain spend."""

        from .model_fallback_chain import classify_model_failure, execute_chain

        day = self.clock().astimezone(timezone.utc).date().isoformat()
        ceiling = self._chain_ceiling(
            router, tier, prompt_bytes, purpose=purpose, call_budget=call_budget)
        admission: dict[str, Any] | None = None
        first_route_ref: str | None = None
        spend: dict[str, tuple[int, str]] = {}
        local_dispatch_proofs: dict[str, dict[str, str]] = {}
        # WP-A/A1: profile id -> why its failed call was settled at zero. It
        # travels into the chain failure detail, so "this attempt cost nothing"
        # is never a bare assertion on the ledger side.
        no_charge: dict[str, str] = {}
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
                admission = _admit_through_lock(lambda: budget.admit(
                    policy_version_id=self.config["budget_policy_ref"], day=day,
                    work_order_ref=work.id, attempt_number=attempt, phase="assessment",
                    route_decision_ref=route["id"],
                    reserved_micros=max(ceiling, micros), mission_binding=scope,
                ), work.id)
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
                # 2026-09-25: an adapter admission refusal is raised while the
                # adapter is still building the broker request -- every
                # ``ModelAdmissionError`` site precedes the exchange -- so the
                # call was never sent and cost nothing.  Settled at the full
                # reservation instead, the claim-support check's refusals
                # (no output schema version) charged $0.102 apiece with no
                # usage entry.  Nothing is released that could have been
                # spent: an error that carries post-send evidence is still
                # charged the reservation.
                definitely_not_sent = isinstance(exc, BrokerDefinitelyNotSent) or (
                    isinstance(exc, ModelAdmissionError)
                    and getattr(exc, "post_send_unknown_evidence", None) is None
                )
                spend[route["id"]] = (
                    (0, "not_sent") if definitely_not_sent else (ceiling, "reserved")
                )
                uncertain_spend = uncertain_spend or not definitely_not_sent
                return {"outcome": "failed",
                        "failure_class": (classify_model_failure(exc)
                                          if definitely_not_sent else "unclassified_failure"),
                        "reason": f"the model call failed: {exc}"}
            if envelope.status != "succeeded":
                if provider_retry_state is not None:
                    from .provider_retry import returned_provider_failure_proof

                    if returned_provider_failure_proof(invocation, envelope) is not None:
                        measured = _cost_micros(
                            invocation, route, profile, ceiling
                        )
                        free = no_charge_reason(invocation, envelope)
                        if measured[1] == "actual":
                            spend[route["id"]] = measured
                        elif free is not None:
                            # WP-A/A1. Retry eligibility proves the provider
                            # executed *the refusal*, not the call. Every code
                            # this path is eligible for is a gate refusal
                            # (RATE_LIMITED, PROVIDER_OVERLOADED and friends)
                            # carrying null usage and cost.available=false --
                            # the provider produced nothing and billed nothing.
                            # Reserving the ceiling for it is how a rate-limited
                            # morning emptied the day's pools.
                            spend[route["id"]] = (0, "failed")
                            no_charge[profile["id"]] = free
                        else:
                            # Retry eligibility proves provider execution, not
                            # metering. Preserve the whole attempt reservation
                            # when the provider did not return actual cost.
                            spend[route["id"]] = (ceiling, "reserved")
                            uncertain_spend = True
                        # A provider-completed failure is chargeable, but it did
                        # not serve model content. Stop this Scheduler attempt;
                        # the caller records its proof before another route.
                        return {
                            "outcome": "failed",
                            "failure_class": "provider_failure",
                            "error_code": (envelope.error or {}).get("code"),
                            "reason": (envelope.error or {}).get(
                                "message", "the provider call failed"),
                            "defer_attempt": True,
                            "value": (invocation, envelope),
                        }
                # The broker answered and the answer is a failure. Its error
                # code, not a guess, decides whether another model may be asked.
                failure_class = classify_model_failure(envelope.error or {})
                # Only broker-local admission responses are known to precede
                # a provider call. Every other failed host envelope may have
                # consumed the full bounded call before validation failed.
                code = str((envelope.error or {}).get("code", "")).upper()
                # The broker's own host-frame validation -- the CLI gateway
                # returned error text instead of model output, a subscription
                # weekly limit being the live case -- carries no usage and no
                # model content, the same reading thesis_impact_control makes
                # when it re-drives them. They must keep the fallback class
                # the classifier gives them instead of being downgraded to a
                # halt below; halting here left every brain chain dark for a
                # day on one gateway's quota wall.
                host_frame_failure = code in {
                    "HOST_COMPLETION_FAILED", "INVALID_HOST_RESULT"}
                dispatch_proof = (envelope.metadata or {}).get("dispatch_proof")
                broker_local_code = code in {
                    "BUSY", "CONCURRENCY_LIMIT", "BROKER_CONCURRENCY_LIMIT",
                    "QUEUE_TIMEOUT", "BROKER_CLOSED",
                } or (
                    code == "REQUIRED_CONTROLS_UNAVAILABLE"
                    and dispatch_proof == _LOCAL_NOT_SENT_PROOF
                )
                broker_local_not_sent = (
                    broker_local_code and dispatch_proof == _LOCAL_NOT_SENT_PROOF
                )
                # A host-frame failure's envelope carries no usage because the
                # broker validated the frame and nothing model-shaped came
                # back -- the same no-usage reading thesis_impact_control
                # re-drives on. Charging the day ledger its reserved ceiling
                # for each one is how a quota-walled gateway burned the whole
                # morning's pools without a single answer.
                # WP-A/A1: and the third exemption. A rate limit, or any
                # broker-attested provider-completed failure with no usage and
                # no cost, is a call the provider positively did not bill. It
                # reached the provider -- so it is not "not sent" -- but the
                # reservation must be released all the same, because holding it
                # charges the day ledger for a refusal at the provider's door.
                free_of_charge = no_charge_reason(invocation, envelope)
                may_have_reached_provider = (
                    not broker_local_not_sent and not host_frame_failure
                    and free_of_charge is None
                )
                if free_of_charge is not None:
                    no_charge[profile["id"]] = free_of_charge
                if broker_local_not_sent:
                    local_dispatch_proofs[profile["id"]] = dict(dispatch_proof)
                    if code == "REQUIRED_CONTROLS_UNAVAILABLE":
                        # The selected endpoint cannot satisfy this Work's
                        # mandatory controls. Exact broker-local no-send proof
                        # permits the already-authorized chain to try another
                        # profile; the same code without proof remains a
                        # terminal contract violation below.
                        failure_class = "model_unavailable"
                spend[route["id"]] = ((ceiling, "reserved")
                                      if may_have_reached_provider else (0, "failed"))
                uncertain_spend = uncertain_spend or may_have_reached_provider
                return {"outcome": "failed",
                        "failure_class": (
                            "unclassified_failure"
                            if may_have_reached_provider and not host_frame_failure
                            and failure_class in {
                                "transport_failure", "provider_failure", "model_unavailable"
                            }
                            else "unclassified_failure"
                            if broker_local_code and not broker_local_not_sent
                            else failure_class
                        ),
                        "error_code": (envelope.error or {}).get("code"),
                        "reason": (envelope.error or {}).get("message", "the model call failed"),
                        "value": envelope}
            spend[route["id"]] = _cost_micros(invocation, route, profile, ceiling)
            return {"outcome": "served", "value": (invocation, envelope)}

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
                excluded_profile_ids=(
                    () if provider_retry_state is None
                    else provider_retry_state["excluded_profile_ids"]
                ),
                required_profile_version_ref=(
                    None if provider_retry_state is None
                    else provider_retry_state["retry_profile_version_ref"]
                ),
            )
            if outcome["status"] == "served" or (
                outcome.get("reason") == "provider_retry"
                and outcome.get("value") is not None
            ):
                served_micros, served_status = spend.get(
                    outcome["route_decision_ref"], (0, "failed"))
        finally:
            # A proved pre-send failure costs zero. Once bytes may have left,
            # missing telemetry cannot release the reservation: the provider
            # may still have completed and charged the call.
            if admission is not None:
                settlement = ceiling if uncertain_spend else served_micros
                _settle_without_losing_the_lease(
                    budget, admission, actual_micros=settlement)

        route_ref = outcome.get("route_decision_ref") or first_route_ref
        if outcome["status"] == "served" or (
            outcome.get("reason") == "provider_retry"
            and outcome.get("value") is not None
        ):
            invocation, envelope = outcome["value"]
            failure = (
                None if envelope.status == "succeeded"
                else str((envelope.error or {}).get(
                    "message", "the model call failed"))
            )
            return {"result": envelope, "failure": failure,
                    "cost_micros": served_micros, "cost_status": served_status,
                    "pool_rejection": None, "route": outcome["decision"],
                    "profile": outcome["profile"], "invocation": invocation}
        # ``budget_refused`` has two different origins.  ``admit`` appends a
        # refusal when Dalton's day ledger stops the call before dispatch.  A
        # broker may also return a BUDGET_* failure after the adapter was
        # called; that path can have uncertain spend and must not be labelled
        # as Dalton's pre-dispatch ``BUDGET_REFUSED`` decision. Cockpit has no
        # canonical Core accounting writer, so the generic chain failure below
        # retains the broker detail without claiming an unpersisted invocation
        # or usage row. Its distinct error code prevents the synthetic
        # ``invocation:not-started`` marker from becoming no-send proof.
        if (outcome["status"] == "halted"
                and outcome.get("reason") == "budget_refused"
                and (pool_rejection is not None or refusal)):
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
        details = [
            {
                **item,
                **(
                    {"dispatch_proof": local_dispatch_proofs[item["profile_id"]]}
                    if item.get("profile_id") in local_dispatch_proofs
                    else {}
                ),
                **(
                    {"no_charge_reason": no_charge[item["profile_id"]]}
                    if item.get("profile_id") in no_charge
                    else {}
                ),
            }
            for item in (outcome.get("failures") or [])
        ]
        detail_text = "; ".join(
            f"{item['profile_id']} [{item['code']}]: {item['message']}"
            for item in details)
        if outcome["status"] == "halted":
            failure = f"the {tier} chain halted on {outcome.get('reason')}: {skipped}"
        else:
            failure = f"every model in the {tier} chain failed: {skipped}"
        unroutable = outcome.get("rejection_reasons") or []
        if unroutable:
            # 2026-09-15: the walk that ends in "no link of the chain is
            # routable" names why the links it never reached were refused --
            # excluded by provider-retry history above all. Without this the
            # message read as though the whole chain had been tried.
            failure += ("；未尝试的环节被拒绝："
                        + ", ".join(sorted(map(str, set(unroutable)))[:6]))
        if detail_text:
            failure += f"; broker details: {detail_text}"
        retryable = outcome["status"] == "halted" and outcome.get("reason") == "capacity_busy"
        return {"result": _failure(
                    work, "MODEL_CHAIN_EXHAUSTED", route_ref,
                    message=failure, chain_failures=details,
                    status="retryable" if retryable else "failed",
                    # WP-A/A1 split two questions that used to share one flag.
                    # "Might this have been billed" now answers no for a proved
                    # free failure; "might this have reached the provider" still
                    # answers yes, and it is the second one this marker means.
                    # Left joined, a rate-limited chain would have claimed the
                    # request was never dispatched.
                    dispatch_state=("unknown" if (uncertain_spend or no_charge)
                                    else "not_started")),
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
