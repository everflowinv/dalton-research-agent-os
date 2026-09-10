"""P14-M: what runs when the first model does not, and what that is allowed to mean.

Until now a Dalton model call picked one model and either got an answer or
failed.  The broker does not fall back either -- deliberately: the adapter
cannot choose an agent, endpoint, credential or *fallback model*, because a
silent provider-side substitution would mean a Claim was produced by a model
nobody named.  So a provider outage was simply a lost call.

A chain keeps that property and adds the missing one.  The alternatives are
named in advance, in a pinned routing-policy version, in order; each link that
is tried is its own immutable route decision, exactly as a switch already was;
and which link served, and why the earlier ones did not, is recorded next to
the decisions.  Nothing is substituted silently: a fallback is as auditable as
the first choice, and replaying the chain gives the same answer.

Two rules do the real work.

**A fallback is only for the provider failing, never for it disagreeing.**  If
the model returns a content refusal -- it declined the task, or answered
outside its contract -- the chain stops.  Otherwise a chain is a machine for
shopping a refused request around until some model says yes, which is the
opposite of what independent verification is for.

**The verifier tier stays independent of the producer.**  The chain for a
verification never starts with the producer's own model family, so the
router's family-independence filter is not the only thing standing between a
GPT-6 producer and a GPT-6 "independent" verifier -- the chain itself is
written so that the first link is already a different family.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from typing import Any

from .cockpit_model import purposes, register_purpose
from .model_accounting import ModelAccountingError, _route_estimate_micros
from .model_router import ModelRouter


class FallbackChainError(RuntimeError):
    """The chain cannot be built or run as asked."""


_NAME_RE = re.compile(r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$")

# The three tiers.  A tier is "what kind of judgement is this", not "how much
# does it cost": the cheap tier is cheap because tagging a claim index does not
# need a frontier model, not because someone wanted to save money on thinking.
TIER_BRAIN = "brain"
TIER_CHEAP = "cheap"
TIER_VERIFIER = "verifier"

# The chains, by profile id, first choice first.
#
# brain: the model that carries an argument -- planning, drafting a deliverable
# or a dossier, producing a thesis-impact assessment, research. gpt-6-astra
# first because that is the model the owner chose for this work; claude-fable-5-1
# behind it because it is a different provider *and* a different family, so an
# OpenAI outage is survivable and a producer/verifier pair drawn from the two is
# independent by construction.
#
# cheap: the passes that are numerous rather than hard -- pulling figures out of
# a window of a filing, tagging a claim index, deterministic-assisted quality
# judging, batch classification. Three links because these are the calls that
# run thousands of times a day and are the ones an outage actually hurts;
# gemini-3.5-flash-lite is the last resort rather than a peer, hence its place.
#
# verifier: independent verification. Every link is a different family from
# every brain link, so a verification can never fall back onto the family that
# produced what it is checking. The router still applies the independence
# filter on top -- the chain makes the common case right, the filter makes the
# uncommon case safe.
_TIER_CHAINS: dict[str, tuple[str, ...]] = {
    TIER_BRAIN: (
        "profile:gpt-6-astra",
        "profile:claude-fable-5-1",
    ),
    TIER_CHEAP: (
        "profile:deepseek-v4-flash",
        "profile:zai-glm-5-3-flash",
        "profile:gemini-3-5-flash-lite",
    ),
    TIER_VERIFIER: (
        "profile:claude-fable-5-1",
        "profile:zai-glm-5-3",
        "profile:gemini-3-5-flash-lite",
    ),
}

TIERS: tuple[str, ...] = (TIER_BRAIN, TIER_CHEAP, TIER_VERIFIER)

# Every purpose registered today, mapped explicitly.  The six brain ones are a
# statement rather than a default: the cockpit's ask, the goal and steering
# proposals, the deliverable draft, the planner's decision and the company
# model specification are all "form a view and argue it".  The extraction lane
# is cheap-tier work too but has no cockpit purpose -- it takes the tier by
# pinning the cheap policy.
#
# This map is code with a test, not policy content.  A lane registering its
# tier must not append a routing-policy version to every pinned policy.
_PURPOSE_TIERS: dict[str, str] = {
    "ask": TIER_BRAIN,
    "goal": TIER_BRAIN,
    "steer": TIER_BRAIN,
    "draft": TIER_BRAIN,
    "plan": TIER_BRAIN,
    "model_spec": TIER_BRAIN,
    # Wave 1's two: tagging a claim index against a fixed aspect vocabulary and
    # scoring an artefact against a rubric are both "apply a stated standard to
    # a lot of items", which is the cheap tier's whole description.
    "claim_index": TIER_CHEAP,
    "quality": TIER_CHEAP,
    # The judgement and cognition layers: each call weighs evidence and writes
    # a position (a decision word, a dossier section, a debate, a call). That
    # is the brain's job, and the verifier chain is what keeps it honest.
    "event_judgement": TIER_BRAIN,
    "thesis_reflection": TIER_BRAIN,
    "dossier": TIER_BRAIN,
    "debate_map": TIER_BRAIN,
    "deep_insight_gate": TIER_BRAIN,
    "industry_framework": TIER_BRAIN,
    "earnings_preview": TIER_BRAIN,
    "earnings_calibration": TIER_BRAIN,
    "conviction_call": TIER_BRAIN,
    # W4: asking, from a zero position, whether we would form this view
    # today is the hardest question in the set -- there is no event to
    # anchor it and the whole file argues for the answer we already hold.
    "zero_base_review": TIER_BRAIN,
    # Reading a rating and a target price off page one of a broker note is
    # "apply a stated standard to a lot of items" -- cheap.
    "street_estimate": TIER_CHEAP,
}

# A link may be skipped for these and only these, and each one means "the
# model did not answer", not "the model answered something unwelcome".
FALLBACK_FAILURES: frozenset[str] = frozenset({
    "transport_failure",
    "provider_failure",
    "model_unavailable",
})
# Named rather than "everything else", so an unclassified failure fails closed
# instead of quietly earning a retry on a second provider.
HALTING_FAILURES: frozenset[str] = frozenset({
    "content_refusal",
    "budget_refused",
    "contract_violation",
    # What the classifier returns when it does not recognise the failure. It is
    # a halt, not an exception: a lane that cannot name why the model failed
    # must not go shopping for one that will answer, and it must not crash the
    # mission tick either.
    "unclassified_failure",
})

# Broker error codes, by what they mean for the chain. The broker's codes are
# provider-agnostic uppercase tokens; the substrings below are matched against
# the whole code so a provider-specific suffix still lands in the right class.
_FAILURE_CODES: tuple[tuple[tuple[str, ...], str], ...] = (
    (("TIMEOUT", "TIMED_OUT", "DEADLINE"), "transport_failure"),
    (("CONNECTION", "NETWORK", "SOCKET", "TRANSPORT", "BROKEN_PIPE", "EOF"),
     "transport_failure"),
    (("MODEL_UNAVAILABLE", "MODEL_NOT_FOUND", "NO_CAPACITY", "OVERLOADED",
      "CAPACITY", "UNAVAILABLE"), "model_unavailable"),
    (("RATE_LIMIT", "RATE_LIMITED", "TOO_MANY_REQUESTS", "THROTTLED",
      "PROVIDER_ERROR", "UPSTREAM", "INTERNAL_ERROR", "SERVER_ERROR",
      "BAD_GATEWAY", "SERVICE_ERROR"), "provider_failure"),
    (("CONTENT_REFUSAL", "CONTENT_FILTER", "REFUSED", "SAFETY", "BLOCKED",
      "MODERATION"), "content_refusal"),
    (("BUDGET",), "budget_refused"),
    (("INVALID", "SCHEMA", "CONTRACT", "UNAUTHORIZED", "FORBIDDEN",
      "AUTH", "IDEMPOTENCY", "MALFORMED", "UNSUPPORTED"), "contract_violation"),
)


def classify_model_failure(failure: Any) -> str:
    """Name what went wrong, in the vocabulary the chain reasons in.

    Takes whatever the caller has: an adapter exception, a failed
    ``ResultEnvelope``'s ``error`` mapping, a bare broker error code, or an
    HTTP status.  Always returns a class -- never raises -- because this runs
    inside a lane tick, and a classifier that throws turns a recoverable
    provider outage into a crashed mission.

    The bias is deliberate: only failures we can positively name as "the
    provider did not answer" earn a fallback.  Everything else halts.
    """

    from .openclaw_model_adapter import (
        BrokerBudgetExceeded,
        BrokerConnectionError,
        BrokerIdempotencyConflict,
        BrokerProtocolError,
        BrokerTimeout,
        ModelAdmissionError,
        OpenClawModelAdapterError,
    )

    if isinstance(failure, BaseException):
        if isinstance(failure, BrokerTimeout):
            return "transport_failure"
        if isinstance(failure, BrokerConnectionError):
            return "transport_failure"
        if isinstance(failure, BrokerBudgetExceeded):
            return "budget_refused"
        if isinstance(failure, (BrokerIdempotencyConflict, ModelAdmissionError)):
            return "contract_violation"
        if isinstance(failure, BrokerProtocolError):
            # The broker answered, but not in a shape this can trust. That is
            # the broker or the provider misbehaving, not the model declining.
            return "provider_failure"
        if isinstance(failure, (TimeoutError, ConnectionError, OSError)):
            return "transport_failure"
        if isinstance(failure, OpenClawModelAdapterError):
            return "unclassified_failure"
        return "unclassified_failure"

    status: int | None = None
    code = ""
    if isinstance(failure, Mapping):
        raw = failure.get("code")
        code = raw.upper() if isinstance(raw, str) else ""
        raw_status = failure.get("status") or failure.get("http_status")
        if isinstance(raw_status, int) and not isinstance(raw_status, bool):
            status = raw_status
    elif isinstance(failure, int) and not isinstance(failure, bool):
        status = failure
    elif isinstance(failure, str):
        code = failure.upper()

    if code:
        for needles, outcome in _FAILURE_CODES:
            if any(needle in code for needle in needles):
                return outcome
        digits = "".join(character for character in code if character.isdigit())
        if len(digits) == 3:
            status = int(digits)
    if status is not None:
        if status == 429 or 500 <= status <= 599:
            return "provider_failure"
        if 400 <= status <= 499:
            return "contract_violation"
    return "unclassified_failure"


def register_purpose_tier(purpose: str, tier: str) -> str:
    """Map one cockpit purpose to a tier, registering the purpose with it.

    Registering the two together is the point: a purpose with no tier is a call
    with no chain and no declared kind of judgement, and it is refused at route
    time.  Re-registering the same pair is a no-op; re-registering a purpose
    onto a different tier is refused, because a lane's tier is not something
    another lane gets to change underneath it.
    """

    if not isinstance(purpose, str) or not _NAME_RE.fullmatch(purpose):
        raise FallbackChainError("a purpose is lowercase words joined by _")
    if tier not in _TIER_CHAINS:
        raise FallbackChainError(f"unknown model tier: {tier!r}")
    existing = _PURPOSE_TIERS.get(purpose)
    if existing is not None and existing != tier:
        raise FallbackChainError(
            f"purpose {purpose!r} is already mapped to tier {existing!r}"
        )
    register_purpose(purpose)
    _PURPOSE_TIERS[purpose] = tier
    return tier


def purpose_tiers() -> dict[str, str]:
    """Every purpose that has a tier, read at call time."""

    return dict(_PURPOSE_TIERS)


def tier_chain(tier: str) -> tuple[str, ...]:
    """The ordered profile ids for one tier."""

    if tier not in _TIER_CHAINS:
        raise FallbackChainError(f"unknown model tier: {tier!r}")
    return _TIER_CHAINS[tier]


def tier_for(purpose: str) -> str:
    """The tier this purpose routes under.  An unmapped purpose is refused."""

    tier = _PURPOSE_TIERS.get(purpose)
    if tier is None:
        raise FallbackChainError(
            f"purpose {purpose!r} has no model tier; register one before routing"
        )
    return tier


def unmapped_purposes() -> tuple[str, ...]:
    """Registered purposes with no tier -- the deployment's check."""

    return tuple(sorted(purposes() - set(_PURPOSE_TIERS)))


def fallback_chains() -> dict[str, Any]:
    """The tier chains, as a routing policy carries them.

    Chains only.  The purpose-to-tier map is deliberately *not* in here: it is a
    registry a lane adds itself to, and a policy that embedded it would append a
    new immutable version -- with a new hash, for every pinned lane -- every
    time some unrelated lane registered a purpose. What is pinned is what
    routing actually reads.
    """

    return {"tiers": {tier: list(chain) for tier, chain in _TIER_CHAINS.items()}}


def may_fall_back(failure_class: str) -> bool:
    """Whether this failure lets the chain try the next link."""

    if failure_class in FALLBACK_FAILURES:
        return True
    if failure_class in HALTING_FAILURES:
        return False
    raise FallbackChainError(
        f"unclassified model failure {failure_class!r}; a chain does not guess"
    )


def served_family(router: ModelRouter, decision_ref: str) -> str:
    """The model family that actually produced something, read from its decision.

    The verifier's independence has to be checked against the model that *ran*,
    not against whatever the caller believes ran.  A brain-tier call that fell
    back from gpt-6-astra to claude-fable-5-1 and then told the verifier its
    producer was OpenAI would let an Anthropic verifier check Anthropic work,
    and the check would pass.  So the family comes out of the producer's own
    immutable route decision -- the same record the assessment is bound to --
    and there is no caller-supplied path to it.
    """

    decision = router.get_decision(decision_ref)
    if decision.get("outcome") != "selected":
        raise FallbackChainError(
            "a rejected route decision produced nothing to be independent of"
        )
    endpoint = decision.get("selected_endpoint") or {}
    family = endpoint.get("family")
    if not isinstance(family, str) or not family:
        raise FallbackChainError("producer route decision names no model family")
    return family


def reserved_micros(route: Mapping[str, Any], profile: Mapping[str, Any]) -> int:
    """The served link's own estimated spend, in micro-USD.

    The estimate comes out of the route decision's candidate snapshot for
    exactly this profile version, so admitting a fallback against the budget
    charges the model that is about to run rather than the one that did not.
    """

    try:
        return _route_estimate_micros(route, profile)
    except ModelAccountingError as exc:
        raise FallbackChainError(str(exc)) from exc


def execute_chain(
    router: ModelRouter,
    work_order: Any,
    *,
    purpose: str,
    capability: str,
    attempt_number: int,
    policy_version_ref: str,
    credential_slot_refs: Sequence[str],
    required_modalities: Sequence[str],
    required_context_tokens: int,
    estimated_input_tokens: int,
    estimated_output_tokens: int,
    idempotency_prefix: str,
    call: Callable[[Mapping[str, Any], Mapping[str, Any]], Mapping[str, Any]],
    admit: Callable[[Mapping[str, Any], Mapping[str, Any], int], Any] | None = None,
    producer_decision_ref: str | None = None,
    tier: str | None = None,
) -> dict[str, Any]:
    """Walk the chain until one link serves, and record every step of it.

    ``call`` is handed the accepted route decision and the exact profile it
    selected, and returns ``{"outcome": "served", ...}`` or ``{"outcome":
    "failed", "failure_class": ...}``.  ``admit`` -- if given -- is handed the
    same pair plus the served link's own estimated spend, so the day budget is
    charged for the model that is actually about to run, and must answer
    ``{"status": "admitted"}`` for the call to be made.

    ``producer_decision_ref`` is how a verifier says what it is verifying: the
    producer's model family is read out of that immutable decision rather than
    taken on the caller's word.

    Each link is a real route decision: the first is ``initial``, every one
    after it is a ``switch`` in the same attempt referencing the one before, so
    the router's own "do not cycle back to a profile already tried" rule holds
    the chain to going forwards.
    """

    tier = tier or tier_for(purpose)
    chain = tier_chain(tier)
    # Independence is measured against the producer's own route decision, so a
    # verification cannot be told a producer it did not have.
    producer_family = (
        served_family(router, producer_decision_ref)
        if producer_decision_ref is not None
        else None
    )
    links: list[dict[str, Any]] = []
    previous_decision_ref: str | None = None
    for step in range(1, len(chain) + 1):
        result = router.route(
            work_order,
            attempt_number=attempt_number,
            capability=capability,
            policy_version_ref=policy_version_ref,
            credential_slot_refs=credential_slot_refs,
            required_modalities=required_modalities,
            required_context_tokens=required_context_tokens,
            estimated_input_tokens=estimated_input_tokens,
            estimated_output_tokens=estimated_output_tokens,
            idempotency_key=f"{idempotency_prefix}:{tier}:{step}",
            decision_kind="initial" if previous_decision_ref is None else "switch",
            previous_decision_ref=previous_decision_ref,
            producer_family=producer_family,
            tier=tier,
        )
        if result.get("status") == "conflict":
            raise FallbackChainError(result.get("reason", "route request conflicted"))
        route = result["decision"]
        if route["outcome"] != "selected":
            # No link of the chain is routable right now: retired, unavailable,
            # not independent of the producer, over budget, or already tried.
            # The decision's candidate snapshot names every link and the reason
            # each one was refused, so the chain does not have to restate it.
            return {
                "status": "exhausted",
                "tier": tier,
                "purpose": purpose,
                "reason": "no link of the chain is routable",
                "route_decision_ref": route["id"],
                "rejection_reasons": route["rejection_reasons"],
                "links": links,
                "served": None,
            }
        profile = router.get_profile(route["selected_profile_version_ref"])
        profile_id = profile["id"]
        if profile_id not in chain:
            # The pinned policy let something through that the chain does not
            # name. Refusing is the only safe answer: accepting would mean the
            # served model is not one this tier declared.
            raise FallbackChainError(
                f"policy selected {profile_id}, which is not in the {tier} chain"
            )
        position = chain.index(profile_id) + 1

        def _record(*, served: bool, skip_reason: str | None) -> dict[str, Any]:
            link = router.record_chain_link(
                work_order_id=route["work_order_ref"],
                capability=capability,
                attempt_number=attempt_number,
                purpose=purpose,
                tier=tier,
                chain_position=position,
                profile_id=profile_id,
                decision_id=route["id"],
                policy_version_ref=policy_version_ref,
                served=served,
                skip_reason=skip_reason,
            )["link"]
            links.append(link)
            return link

        previous_decision_ref = route["id"]
        if admit is not None:
            admission = admit(route, profile, reserved_micros(route, profile))
            # Explicit, not truthy. A budget authority that returns something
            # this does not understand has not admitted the call, and treating
            # an unrecognised answer as a yes is how money gets spent by
            # accident.
            if not isinstance(admission, Mapping) or admission.get("status") != "admitted":
                _record(served=False, skip_reason="budget_refused")
                return {
                    "status": "halted",
                    "tier": tier,
                    "purpose": purpose,
                    "reason": "budget_refused",
                    "links": links,
                    "served": None,
                }
        outcome = call(route, profile)
        if not isinstance(outcome, Mapping) or "outcome" not in outcome:
            raise FallbackChainError("a chain call must report an outcome")
        if outcome["outcome"] == "served":
            _record(served=True, skip_reason=None)
            return {
                "status": "served",
                "tier": tier,
                "purpose": purpose,
                "chain_position": position,
                "profile_id": profile_id,
                "route_decision_ref": route["id"],
                "decision": route,
                "profile": profile,
                "value": outcome.get("value"),
                "links": links,
                "served": links[-1],
            }
        failure_class = str(outcome.get("failure_class", ""))
        _record(served=False, skip_reason=failure_class or "unclassified")
        if not may_fall_back(failure_class):
            return {
                "status": "halted",
                "tier": tier,
                "purpose": purpose,
                "reason": failure_class,
                "links": links,
                "served": None,
            }
    return {
        "status": "exhausted",
        "tier": tier,
        "purpose": purpose,
        "reason": "every link in the chain was tried and failed",
        "links": links,
        "served": None,
    }


def routing_overview(
    router: ModelRouter,
    *,
    openclaw_config: Mapping[str, Any] | None = None,
    checked_at: datetime | None = None,
    work_order_id: str | None = None,
) -> dict[str, Any]:
    """What the cockpit or a report shows: tiers, chains, last served, sync.

    One reader rather than four, because the four questions are one question --
    "is the model side of this system wired the way it says it is" -- and they
    were previously answerable only by opening the router database by hand.
    """

    latest = {profile["id"]: profile for profile in router.latest_profiles()}
    links = router.chain_links(work_order_id=work_order_id)
    served_by_tier: dict[str, dict[str, Any]] = {}
    for link in links:
        if link["served"]:
            served_by_tier[link["tier"]] = link
    tiers: dict[str, Any] = {}
    for tier, chain in _TIER_CHAINS.items():
        served = served_by_tier.get(tier)
        tiers[tier] = {
            "chain": [
                {
                    "position": position,
                    "profile_id": profile_id,
                    "registered": profile_id in latest,
                    "status": (latest.get(profile_id) or {}).get("status", "live"),
                    "family": (latest.get(profile_id) or {}).get("family"),
                }
                for position, profile_id in enumerate(chain, start=1)
            ],
            "last_served": (
                None
                if served is None
                else {
                    "profile_id": served["profile_id"],
                    "chain_position": served["chain_position"],
                    "decision_id": served["decision_id"],
                    "purpose": served["purpose"],
                    "created_at": served["created_at"],
                }
            ),
            "skipped_since_last_served": [
                {
                    "profile_id": link["profile_id"],
                    "chain_position": link["chain_position"],
                    "skip_reason": link["skip_reason"],
                    "decision_id": link["decision_id"],
                }
                for link in links
                if link["tier"] == tier and not link["served"]
            ],
        }
    overview: dict[str, Any] = {
        "schema_version": "0.1",
        "purpose_tiers": dict(_PURPOSE_TIERS),
        "unmapped_purposes": list(unmapped_purposes()),
        "tiers": tiers,
        "catalog": None,
    }
    if openclaw_config is not None:
        from .openclaw_catalog_reconcile import catalog_sync_status

        if checked_at is None:
            raise FallbackChainError("a catalog status needs the moment it was checked")
        overview["catalog"] = catalog_sync_status(
            router, openclaw_config, checked_at=checked_at
        )
    return overview


__all__ = [
    "FALLBACK_FAILURES",
    "HALTING_FAILURES",
    "TIERS",
    "TIER_BRAIN",
    "TIER_CHEAP",
    "TIER_VERIFIER",
    "FallbackChainError",
    "execute_chain",
    "fallback_chains",
    "may_fall_back",
    "purpose_tiers",
    "register_purpose_tier",
    "classify_model_failure",
    "reserved_micros",
    "routing_overview",
    "served_family",
    "tier_chain",
    "tier_for",
    "unmapped_purposes",
]
