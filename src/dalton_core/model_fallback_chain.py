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

# Every purpose registered today, mapped explicitly.  All six are brain, and
# that is a statement rather than a default: the cockpit's ask, the goal and
# steering proposals, the deliverable draft, the planner's decision and the
# company model specification are all "form a view and argue it".  The cheap
# tier's work -- extraction windows, claim-index tagging, quality judging,
# batch classification -- runs in lanes that have their own pinned policies and
# have not registered cockpit purposes; they get the tier by pinning the cheap
# policy, and will register a purpose here when they become cockpit-shaped.
_PURPOSE_TIERS: dict[str, str] = {
    "ask": TIER_BRAIN,
    "goal": TIER_BRAIN,
    "steer": TIER_BRAIN,
    "draft": TIER_BRAIN,
    "plan": TIER_BRAIN,
    "model_spec": TIER_BRAIN,
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
})


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
    """The whole tier table, as a routing policy carries it."""

    return {
        "tiers": {tier: list(chain) for tier, chain in _TIER_CHAINS.items()},
        "purpose_tiers": dict(_PURPOSE_TIERS),
    }


def may_fall_back(failure_class: str) -> bool:
    """Whether this failure lets the chain try the next link."""

    if failure_class in FALLBACK_FAILURES:
        return True
    if failure_class in HALTING_FAILURES:
        return False
    raise FallbackChainError(
        f"unclassified model failure {failure_class!r}; a chain does not guess"
    )


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
    producer_family: str | None = None,
    tier: str | None = None,
) -> dict[str, Any]:
    """Walk the chain until one link serves, and record every step of it.

    ``call`` is handed the accepted route decision and the exact profile it
    selected, and returns ``{"outcome": "served", ...}`` or ``{"outcome":
    "failed", "failure_class": ...}``.  ``admit`` -- if given -- is handed the
    same pair plus the served link's own estimated spend, so the day budget is
    charged for the model that is actually about to run.

    Each link is a real route decision: the first is ``initial``, every one
    after it is a ``switch`` in the same attempt referencing the one before, so
    the router's own "do not cycle back to a profile already tried" rule holds
    the chain to going forwards.
    """

    tier = tier or tier_for(purpose)
    chain = tier_chain(tier)
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
            if admission is None or admission is False:
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
    "reserved_micros",
    "routing_overview",
    "tier_chain",
    "tier_for",
    "unmapped_purposes",
]
