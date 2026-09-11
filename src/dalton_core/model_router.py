"""Versioned, deterministic model routing for Dalton Core.

The router stores logical credential-slot references, never credential values.
Every initial selection, retry, and switch produces a new immutable decision;
there is no in-process or provider-side silent fallback.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sqlite3
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterator

from .contracts import WorkOrder
from .store import authorization_flag, authorized_flag


SCHEMA_VERSION = "0.1"
_SCHEMA_PATH = Path(__file__).with_name("model_router_schema.sql")
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_REF_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+._-]*:[^\s]+$")
_SLOT_RE = re.compile(r"^credential[-_]slot:[A-Za-z0-9][A-Za-z0-9._:/-]*$")
_TOKEN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/+-]*$")
_WAL_CONVERSION_SECONDS = 5.0
_DECISION_KINDS = frozenset({"initial", "retry", "switch"})
_AVAILABILITY_STATES = frozenset({"available", "degraded", "unavailable"})
_PREFERENCE_FIELDS = frozenset(
    {
        "estimated_cost_usd",
        "context.max_context_tokens",
        "context.max_output_tokens",
        "limits.max_total_tokens",
        "cost.input_per_million_usd",
        "cost.output_per_million_usd",
        "provider",
        "model",
        "family",
        "profile_id",
        "profile_version_ref",
    }
)
_BUDGET_FIELDS = (
    "max_input_tokens",
    "max_output_tokens",
    "max_total_tokens",
    "max_cost_usd",
)
# P14-M: a profile the broker no longer offers is retired, not deleted.
#
# The catalog drifted because the only two moves available were "leave the
# stale profile there" and "delete it".  Deleting rewrites history -- a route
# decision from June names a profile version, and a version chain with a hole
# in it can no longer be replayed -- so nothing was ever deleted, and Dalton
# kept five profiles the broker had stopped offering.  Retirement is the third
# move: a new *version* of the same profile that says "not offered any more,
# here is why and here is the broker catalog that proved it".  The chain grows
# forward, every old decision still resolves, and routing refuses the profile
# from the next call onwards.
_PROFILE_STATUSES = frozenset({"live", "retired"})
_RETIREMENT_KEYS = {"reason", "retired_at", "broker_catalog_hash"}
RETIRED_REASON_NOT_IN_BROKER = "not_in_broker_catalog"


class ModelRouterError(Exception):
    """Base class for model-router failures."""


class ModelRouterValidationError(ModelRouterError, ValueError):
    pass


class ModelRouterConflict(ModelRouterError):
    pass


class ModelProfileNotFound(ModelRouterError):
    pass


class RoutingPolicyNotFound(ModelRouterError):
    pass


class RouteDecisionNotFound(ModelRouterError):
    pass


class RouteTransitionError(ModelRouterError):
    pass


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def canonical_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _timestamp(value: datetime) -> str:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ModelRouterValidationError(
            "model router clock must return a timezone-aware datetime"
        )
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _parse_time(value: Any, name: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise ModelRouterValidationError(f"{name} must be an RFC3339 timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ModelRouterValidationError(f"{name} must be an RFC3339 timestamp") from exc
    if parsed.tzinfo is None:
        raise ModelRouterValidationError(f"{name} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _closed(
    value: Any,
    *,
    allowed: set[str],
    required: set[str],
    name: str,
) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ModelRouterValidationError(f"{name} must be an object")
    unknown = set(value) - allowed
    missing = required - set(value)
    if unknown:
        raise ModelRouterValidationError(f"{name} unknown field(s): {sorted(unknown)}")
    if missing:
        raise ModelRouterValidationError(f"{name} missing field(s): {sorted(missing)}")
    return value


def _string(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ModelRouterValidationError(f"{name} must be a non-empty string")
    return value


def _token(value: Any, name: str) -> str:
    result = _string(value, name)
    if not _TOKEN_RE.fullmatch(result):
        raise ModelRouterValidationError(f"{name} must be a canonical token")
    return result


def _ref(value: Any, name: str, *, nullable: bool = False) -> str | None:
    if value is None and nullable:
        return None
    result = _string(value, name)
    if not _REF_RE.fullmatch(result):
        raise ModelRouterValidationError(f"{name} must be an opaque namespaced reference")
    return result


def _slot(value: Any, name: str = "credential_slot_ref") -> str:
    result = _string(value, name)
    if not _SLOT_RE.fullmatch(result):
        raise ModelRouterValidationError(
            f"{name} must be a logical credential-slot reference, not a credential"
        )
    return result


def _positive_int(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ModelRouterValidationError(f"{name} must be a positive integer")
    return value


def _nonnegative_int(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ModelRouterValidationError(f"{name} must be a non-negative integer")
    return value


def _decimal(value: Any, name: str, *, positive: bool = False) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ModelRouterValidationError(f"{name} must be a finite number")
    if isinstance(value, float) and not math.isfinite(value):
        raise ModelRouterValidationError(f"{name} must be a finite number")
    try:
        result = Decimal(str(value))
    except InvalidOperation as exc:
        raise ModelRouterValidationError(f"{name} must be a finite number") from exc
    if not result.is_finite() or result < 0 or (positive and result <= 0):
        qualifier = "positive" if positive else "non-negative"
        raise ModelRouterValidationError(f"{name} must be finite and {qualifier}")
    try:
        as_float = float(result)
    except (OverflowError, ValueError) as exc:
        raise ModelRouterValidationError(f"{name} is outside the supported range") from exc
    if not math.isfinite(as_float):
        raise ModelRouterValidationError(f"{name} is outside the supported range")
    return result


def _unique_tokens(
    value: Any, name: str, *, nonempty: bool = False
) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        raise ModelRouterValidationError(f"{name} must be an array")
    result = tuple(_token(item, f"{name}[]") for item in value)
    if nonempty and not result:
        raise ModelRouterValidationError(f"{name} must not be empty")
    if len(set(result)) != len(result):
        raise ModelRouterValidationError(f"{name} must contain unique values")
    return result


def _unique_refs(value: Any, name: str) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        raise ModelRouterValidationError(f"{name} must be an array")
    result = tuple(_ref(item, f"{name}[]") for item in value)
    if len(set(result)) != len(result):
        raise ModelRouterValidationError(f"{name} must contain unique values")
    return result  # type: ignore[return-value]


def _money_string(value: Decimal) -> str:
    # Six decimal places are enough for routing estimates while remaining
    # canonical and directly comparable in audit fixtures.
    return format(value, ".6f")


def independent_families(candidate_family: str, producer_family: str) -> bool:
    """Unknown lineage is never evidence of verifier independence."""

    return (
        not candidate_family.startswith("unclassified:")
        and not producer_family.startswith("unclassified:")
        and candidate_family != producer_family
    )


def _metadata_declaration_wire(value: Mapping[str, Any]) -> dict[str, Any]:
    expected = {
        "schema_version", "declaration_ref", "profile_id", "version",
        "prior_declaration_ref", "provider", "model", "family", "capabilities",
        "actor_ref", "created_at", "content_hash",
    }
    if not isinstance(value, Mapping) or set(value) != expected:
        raise ModelRouterValidationError("metadata declaration has an invalid schema")
    body = {
        "schema_version": _string(value["schema_version"], "schema_version"),
        "declaration_ref": _ref(value["declaration_ref"], "declaration_ref"),
        "profile_id": _ref(value["profile_id"], "profile_id"),
        "version": _positive_int(value["version"], "version"),
        "prior_declaration_ref": _ref(
            value["prior_declaration_ref"], "prior_declaration_ref", nullable=True
        ),
        "provider": _token(value["provider"], "provider"),
        "model": _token(value["model"], "model"),
        "family": _token(value["family"], "family"),
        "capabilities": list(_unique_tokens(
            value["capabilities"], "capabilities", nonempty=True
        )),
        "actor_ref": _string(value["actor_ref"], "actor_ref"),
        "created_at": _string(value["created_at"], "created_at"),
    }
    _parse_time(body["created_at"], "created_at")
    if value["schema_version"] != SCHEMA_VERSION:
        raise ModelRouterValidationError("metadata declaration schema_version is invalid")
    if value["content_hash"] != canonical_hash(body):
        raise ModelRouterConflict("metadata declaration content hash drifted")
    return {**body, "content_hash": value["content_hash"]}


def _retirement_wire(value: Any) -> dict[str, Any]:
    """The evidence a retirement carries, so it can be argued with later."""

    obj = _closed(
        value,
        allowed=_RETIREMENT_KEYS,
        required=_RETIREMENT_KEYS,
        name="profile.retirement",
    )
    reason = _token(obj["reason"], "profile.retirement.reason")
    retired_at = _string(obj["retired_at"], "profile.retirement.retired_at")
    _parse_time(retired_at, "profile.retirement.retired_at")
    catalog_hash = _string(
        obj["broker_catalog_hash"], "profile.retirement.broker_catalog_hash"
    )
    if not _HASH_RE.fullmatch(catalog_hash):
        raise ModelRouterValidationError(
            "profile.retirement.broker_catalog_hash must be a SHA-256 digest"
        )
    return {
        "reason": reason,
        "retired_at": retired_at,
        "broker_catalog_hash": catalog_hash,
    }


def _profile_wire(data: Mapping[str, Any]) -> dict[str, Any]:
    keys = {
        "schema_version",
        "profile_version_ref",
        "id",
        "version",
        "created_at",
        "prior_version_ref",
        "provider",
        "model",
        "family",
        "adapter_ref",
        "credential_slot_ref",
        "capabilities",
        "modalities",
        "context",
        "availability",
        "cost",
        "limits",
        "content_hash",
        # Optional, and absent means live. Absent rather than "live" so every
        # profile version written before retirement existed keeps the exact
        # content hash it was registered under: a hash that moves because a
        # field was added is a history that no longer verifies.
        "status",
        "retirement",
        # P14-M2: optional, and absent means "this model has a rate card".
        # A model the gateway offers with no published price can still be
        # registered -- refusing to register it would make it invisible -- but
        # it may only ever be the last link of a chain, because a call whose
        # cost cannot be estimated cannot be admitted against a day budget
        # honestly.  Absent rather than ``unpriced: false`` for the same reason
        # ``status`` is absent on a live profile: one way to say a thing, so
        # one content hash.
        "unpriced",
    }
    required = keys - {"content_hash", "status", "retirement", "unpriced"}
    obj = _closed(data, allowed=keys, required=required, name="model endpoint profile")
    if obj["schema_version"] != SCHEMA_VERSION:
        raise ModelRouterValidationError("profile schema_version is unsupported")
    created_at = _string(obj["created_at"], "profile.created_at")
    _parse_time(created_at, "profile.created_at")
    context = _closed(
        obj["context"],
        allowed={"max_context_tokens", "max_output_tokens"},
        required={"max_context_tokens", "max_output_tokens"},
        name="profile.context",
    )
    availability = _closed(
        obj["availability"],
        allowed={"state", "checked_at", "valid_until"},
        required={"state", "checked_at", "valid_until"},
        name="profile.availability",
    )
    state = _string(availability["state"], "profile.availability.state")
    if state not in _AVAILABILITY_STATES:
        raise ModelRouterValidationError("profile availability state is invalid")
    checked_at = _string(availability["checked_at"], "profile.availability.checked_at")
    valid_until = _string(availability["valid_until"], "profile.availability.valid_until")
    checked_time = _parse_time(checked_at, "profile.availability.checked_at")
    valid_time = _parse_time(valid_until, "profile.availability.valid_until")
    if valid_time <= checked_time:
        raise ModelRouterValidationError(
            "profile availability valid_until must be after checked_at"
        )
    cost = _closed(
        obj["cost"],
        allowed={"currency", "input_per_million_usd", "output_per_million_usd"},
        required={"currency", "input_per_million_usd", "output_per_million_usd"},
        name="profile.cost",
    )
    if cost["currency"] != "USD":
        raise ModelRouterValidationError("profile.cost.currency must be USD")
    input_cost = _decimal(cost["input_per_million_usd"], "input_per_million_usd")
    output_cost = _decimal(cost["output_per_million_usd"], "output_per_million_usd")
    limits = _closed(
        obj["limits"],
        allowed=set(_BUDGET_FIELDS),
        required=set(_BUDGET_FIELDS),
        name="profile.limits",
    )
    max_context = _positive_int(context["max_context_tokens"], "max_context_tokens")
    max_output = _positive_int(context["max_output_tokens"], "max_output_tokens")
    limit_input = _positive_int(limits["max_input_tokens"], "limits.max_input_tokens")
    limit_output = _positive_int(limits["max_output_tokens"], "limits.max_output_tokens")
    limit_total = _positive_int(limits["max_total_tokens"], "limits.max_total_tokens")
    limit_cost = _decimal(limits["max_cost_usd"], "limits.max_cost_usd", positive=True)
    if limit_input > max_context or limit_output > max_output:
        raise ModelRouterValidationError("profile limits exceed declared model context")
    if limit_total > max_context + max_output:
        raise ModelRouterValidationError("profile total token limit exceeds model capacity")
    prior = _ref(obj["prior_version_ref"], "prior_version_ref", nullable=True)
    wire = {
        "schema_version": SCHEMA_VERSION,
        "profile_version_ref": _ref(obj["profile_version_ref"], "profile_version_ref"),
        "id": _ref(obj["id"], "profile.id"),
        "version": _positive_int(obj["version"], "profile.version"),
        "created_at": created_at,
        "prior_version_ref": prior,
        "provider": _token(obj["provider"], "profile.provider"),
        "model": _token(obj["model"], "profile.model"),
        "family": _token(obj["family"], "profile.family"),
        "adapter_ref": _ref(obj["adapter_ref"], "profile.adapter_ref"),
        "credential_slot_ref": _slot(obj["credential_slot_ref"]),
        "capabilities": list(
            _unique_tokens(obj["capabilities"], "profile.capabilities", nonempty=True)
        ),
        "modalities": list(
            _unique_tokens(obj["modalities"], "profile.modalities", nonempty=True)
        ),
        "context": {
            "max_context_tokens": max_context,
            "max_output_tokens": max_output,
        },
        "availability": {
            "state": state,
            "checked_at": checked_at,
            "valid_until": valid_until,
        },
        "cost": {
            "currency": "USD",
            "input_per_million_usd": float(input_cost),
            "output_per_million_usd": float(output_cost),
        },
        "limits": {
            "max_input_tokens": limit_input,
            "max_output_tokens": limit_output,
            "max_total_tokens": limit_total,
            "max_cost_usd": float(limit_cost),
        },
    }
    unpriced = obj.get("unpriced")
    if unpriced is not None:
        if unpriced is not True:
            raise ModelRouterValidationError(
                "profile.unpriced is either absent or true; a priced profile omits it"
            )
        wire["unpriced"] = True
    status = obj.get("status")
    retirement = obj.get("retirement")
    if status is not None:
        status = _string(status, "profile.status")
        if status not in _PROFILE_STATUSES:
            raise ModelRouterValidationError("profile status is invalid")
        if status == "live":
            # Absent is what live means here, and the two must not both be
            # sayable: the same profile written once each way would have two
            # different content hashes and look like two different models.
            raise ModelRouterValidationError(
                "a live profile carries no status; omit it"
            )
    if status == "retired":
        if retirement is None:
            raise ModelRouterValidationError(
                "a retired profile must record why and against which catalog"
            )
        wire["status"] = status
        wire["retirement"] = _retirement_wire(retirement)
    elif retirement is not None:
        raise ModelRouterValidationError(
            "only a retired profile carries a retirement record"
        )
    digest = canonical_hash(wire)
    asserted = obj.get("content_hash")
    if asserted is not None and asserted != digest:
        raise ModelRouterValidationError("profile content_hash mismatch")
    wire["content_hash"] = digest
    return wire


def _fallback_chains_wire(value: Any) -> dict[str, Any]:
    """Per-tier chains: which model first, which one after it fails.

    A chain is policy, not code, for the same reason the pinned profile was:
    two jobs sharing one hard-coded ordering can never differ, and a chain that
    lives in a module cannot be pinned by version in a lane's configuration.

    The *chains* are pinned here and nothing else.  Which purpose belongs to
    which tier is a mutable registry -- a lane registers its own -- and folding
    it into the policy content would mean every new lane silently appended a
    routing-policy version to every policy that carried the map, changing hashes
    that nothing about routing had actually changed.
    """

    obj = _closed(
        value,
        allowed={"tiers"},
        required={"tiers"},
        name="policy.fallback_chains",
    )
    raw_tiers = obj["tiers"]
    if not isinstance(raw_tiers, Mapping) or not raw_tiers:
        raise ModelRouterValidationError("fallback_chains.tiers must be a non-empty object")
    tiers: dict[str, list[str]] = {}
    for name, chain in raw_tiers.items():
        tier = _token(name, "fallback_chains.tiers key")
        refs = _unique_refs(chain, f"fallback_chains.tiers.{tier}")
        if not refs:
            raise ModelRouterValidationError(f"tier {tier} must name at least one profile")
        tiers[tier] = list(refs)
    return {"tiers": tiers}


PURPOSE_OVERRIDE_MODES: frozenset[str] = frozenset({"tier", "explicit"})
_PURPOSE_RE = re.compile(r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$")


def _purpose_overrides_wire(value: Any) -> dict[str, Any]:
    """P14-M2: which models one calling stage uses, when the owner has said.

    The tier chains answer "what kind of judgement is this"; an override
    answers "and for *this* stage, use these models, in this order".  It lives
    in the policy content rather than in a table of its own for the same reason
    the chains do: a lane pins a policy version, so a selection that is part of
    that version is a selection the lane's next call reads, and every earlier
    version -- the previous selection -- stays byte-identical.  Publishing the
    old content again is the whole of rollback.

    Two modes and no third.  ``tier`` is "follow the tier", written down rather
    than left absent so the page can show that the owner looked and chose to
    follow; ``explicit`` names the chain, first choice first.
    """

    if not isinstance(value, Mapping) or not value:
        raise ModelRouterValidationError("purpose_overrides must be a non-empty object")
    overrides: dict[str, Any] = {}
    for name, raw in value.items():
        if not isinstance(name, str) or not _PURPOSE_RE.fullmatch(name):
            raise ModelRouterValidationError(
                "a purpose_overrides key is lowercase words joined by _"
            )
        entry = _closed(
            raw,
            # ``actor_ref`` is optional and is who chose this. It is in the
            # policy content rather than beside it because the content is what
            # a route decision hashes: "who had selected this model when this
            # Claim was produced" is then answerable from the decision alone,
            # which is the whole reason the selection is a version at all.
            allowed={"mode", "chain", "actor_ref"},
            required={"mode"},
            name=f"purpose_overrides.{name}",
        )
        mode = _string(entry["mode"], f"purpose_overrides.{name}.mode")
        if mode not in PURPOSE_OVERRIDE_MODES:
            raise ModelRouterValidationError(
                f"purpose_overrides.{name}.mode is tier or explicit"
            )
        actor = entry.get("actor_ref")
        chosen: dict[str, Any] = {"mode": mode}
        if mode == "tier":
            if "chain" in entry:
                raise ModelRouterValidationError(
                    f"purpose_overrides.{name} follows its tier and names no chain"
                )
        else:
            refs = _unique_refs(entry.get("chain"), f"purpose_overrides.{name}.chain")
            if not refs:
                raise ModelRouterValidationError(
                    f"purpose_overrides.{name} must name at least one profile"
                )
            chosen["chain"] = list(refs)
        if actor is not None:
            chosen["actor_ref"] = _string(actor, f"purpose_overrides.{name}.actor_ref")
        overrides[name] = chosen
    return {key: overrides[key] for key in sorted(overrides)}


def policy_chain(
    policy: Mapping[str, Any], *, tier: str | None, purpose: str | None = None
) -> dict[str, Any] | None:
    """The chain this pinned policy actually runs for one purpose, or ``None``.

    One reader, used by the router when it filters candidates and by the chain
    walker when it decides what to try next, because two readers of the same
    rule is how a selection ends up meaning one thing to the filter and another
    to the walk.  An explicit selection wins over the tier's chain; ``tier``
    mode is the same answer as no override at all, which is what makes
    "follow the tier" a real choice rather than the absence of one.
    """

    overrides = policy.get("purpose_overrides") or {}
    entry = overrides.get(purpose) if isinstance(purpose, str) else None
    if isinstance(entry, Mapping) and entry.get("mode") == "explicit":
        return {"mode": "explicit", "tier": tier, "chain": tuple(entry["chain"])}
    declared = (policy.get("fallback_chains") or {}).get("tiers", {})
    if tier is None or tier not in declared:
        return None
    return {"mode": "tier", "tier": tier, "chain": tuple(declared[tier])}


def live_links(
    chain: Sequence[str], profiles: Mapping[str, Mapping[str, Any]]
) -> list[str]:
    """The links of a chain this Core could actually route to right now."""

    return [
        profile_id for profile_id in chain
        if profile_id in profiles and profiles[profile_id].get("status") != "retired"
    ]


def resolve_chain(
    policy: Mapping[str, Any],
    *,
    tier: str | None,
    purpose: str | None,
    profiles: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any] | None:
    """The chain, after retirement has been taken into account.

    The owner's rule: when the gateway drops a model Dalton was using, the
    stage falls to the next link by itself.  Most of that is free -- a retired
    profile is refused as a candidate, so a chain whose *first* link is retired
    selects its second without anybody doing anything.

    The case that is not free is an explicit selection whose models have *all*
    gone.  Refusing the call would be silent-by-another-name: the owner chose
    two models a month ago, the gateway dropped both, and the stage stops.  So
    the selection is superseded by the tier's own chain -- which is what the
    stage would have run had nobody selected anything -- and the fact that it
    happened is carried in ``superseded_chain`` so the notice and the page can
    both say so.  The selection itself is untouched: it is content of an
    immutable policy version, and re-selecting is the owner's move.
    """

    resolved = policy_chain(policy, tier=tier, purpose=purpose)
    if resolved is None:
        return None
    if resolved["mode"] != "explicit" or live_links(resolved["chain"], profiles):
        return resolved
    declared = (policy.get("fallback_chains") or {}).get("tiers", {})
    fallback = tuple(declared.get(tier) or ()) if tier is not None else ()
    if not fallback or not live_links(fallback, profiles):
        return resolved
    return {
        "mode": "tier_after_retirement",
        "tier": tier,
        "chain": fallback,
        "superseded_chain": list(resolved["chain"]),
    }


def _policy_wire(data: Mapping[str, Any]) -> dict[str, Any]:
    keys = {
        "schema_version",
        "policy_version_ref",
        "id",
        "version",
        "created_at",
        "prior_version_ref",
        "filters",
        "ordered_preferences",
        "content_hash",
        # Optional and omitted when absent, so every policy version registered
        # before chains existed keeps its exact hash.
        "fallback_chains",
        # P14-M2: the owner's per-stage selection.  Same rule -- omitted when
        # absent, so no policy version written before selection existed moves.
        "purpose_overrides",
    }
    required = keys - {"content_hash", "fallback_chains", "purpose_overrides"}
    obj = _closed(data, allowed=keys, required=required, name="model routing policy")
    if obj["schema_version"] != SCHEMA_VERSION:
        raise ModelRouterValidationError("policy schema_version is unsupported")
    created_at = _string(obj["created_at"], "policy.created_at")
    _parse_time(created_at, "policy.created_at")
    filters = _closed(
        obj["filters"],
        allowed={
            "allowed_profile_ids",
            "allowed_providers",
            "allowed_families",
            "allowed_adapter_refs",
            "required_modalities",
            "family_independence_capabilities",
        },
        required={
            "allowed_profile_ids",
            "allowed_providers",
            "allowed_families",
            "allowed_adapter_refs",
            "required_modalities",
            "family_independence_capabilities",
        },
        name="policy.filters",
    )
    allowed_profile_ids = _unique_refs(filters["allowed_profile_ids"], "allowed_profile_ids")
    allowed_adapter_refs = _unique_refs(filters["allowed_adapter_refs"], "allowed_adapter_refs")
    preferences_raw = obj["ordered_preferences"]
    if not isinstance(preferences_raw, (list, tuple)) or not preferences_raw:
        raise ModelRouterValidationError("ordered_preferences must be a non-empty array")
    preferences: list[dict[str, str]] = []
    seen_fields: set[str] = set()
    for index, item in enumerate(preferences_raw):
        pref = _closed(
            item,
            allowed={"field", "direction"},
            required={"field", "direction"},
            name=f"ordered_preferences[{index}]",
        )
        field = _string(pref["field"], "preference.field")
        direction = _string(pref["direction"], "preference.direction")
        if field not in _PREFERENCE_FIELDS:
            raise ModelRouterValidationError(f"unsupported preference field: {field}")
        if direction not in {"asc", "desc"}:
            raise ModelRouterValidationError("preference direction must be asc or desc")
        if field in seen_fields:
            raise ModelRouterValidationError("preference fields must be unique")
        seen_fields.add(field)
        preferences.append({"field": field, "direction": direction})
    wire = {
        "schema_version": SCHEMA_VERSION,
        "policy_version_ref": _ref(obj["policy_version_ref"], "policy_version_ref"),
        "id": _ref(obj["id"], "policy.id"),
        "version": _positive_int(obj["version"], "policy.version"),
        "created_at": created_at,
        "prior_version_ref": _ref(
            obj["prior_version_ref"], "prior_version_ref", nullable=True
        ),
        "filters": {
            "allowed_profile_ids": list(allowed_profile_ids),
            "allowed_providers": list(
                _unique_tokens(filters["allowed_providers"], "allowed_providers")
            ),
            "allowed_families": list(
                _unique_tokens(filters["allowed_families"], "allowed_families")
            ),
            "allowed_adapter_refs": list(allowed_adapter_refs),
            "required_modalities": list(
                _unique_tokens(filters["required_modalities"], "required_modalities")
            ),
            "family_independence_capabilities": list(
                _unique_tokens(
                    filters["family_independence_capabilities"],
                    "family_independence_capabilities",
                )
            ),
        },
        "ordered_preferences": preferences,
    }
    chains = obj.get("fallback_chains")
    if chains is not None:
        wire["fallback_chains"] = _fallback_chains_wire(chains)
    overrides = obj.get("purpose_overrides")
    if overrides is not None:
        wire["purpose_overrides"] = _purpose_overrides_wire(overrides)
    digest = canonical_hash(wire)
    asserted = obj.get("content_hash")
    if asserted is not None and asserted != digest:
        raise ModelRouterValidationError("policy content_hash mismatch")
    wire["content_hash"] = digest
    return wire


class ModelRouter:
    """SQLite model catalog and deterministic selection service."""

    _authorized = authorized_flag()

    def __init__(
        self,
        path: str | Path = ":memory:",
        *,
        connection: sqlite3.Connection | None = None,
        clock: Callable[[], datetime] | None = None,
        read_only: bool = False,
    ) -> None:
        if read_only and connection is not None:
            raise ValueError("read_only requires a file, not a caller-owned connection")
        self.path = str(path)
        self.read_only = read_only
        self.clock = clock or _utc_now
        from .readonly_sqlite import connect_read_only
        self.connection = (connect_read_only(path) if read_only else
                           connection or sqlite3.connect(self.path, isolation_level=None))
        if not read_only and connection is None and self.path != ":memory:":
            os.chmod(self.path, 0o600)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA busy_timeout = 5000")
        if not read_only and connection is None and self.path != ":memory:":
            self._ensure_wal()
        self._authorization_flag = authorization_flag(
            self.connection, "dalton_model_router_authorized")
        if not read_only:
            self.connection.executescript(_SCHEMA_PATH.read_text(encoding="utf-8"))

    def _ensure_wal(self) -> None:
        """Keep concurrent route readers from blocking an immutable link commit."""

        row = self.connection.execute("PRAGMA journal_mode").fetchone()
        if row is not None and str(row[0]).lower() == "wal":
            return
        deadline = time.monotonic() + _WAL_CONVERSION_SECONDS
        while True:
            try:
                mode = self.connection.execute("PRAGMA journal_mode=WAL").fetchone()
                if mode is not None and str(mode[0]).lower() == "wal":
                    return
            except sqlite3.OperationalError:
                mode = self.connection.execute("PRAGMA journal_mode").fetchone()
                if mode is not None and str(mode[0]).lower() == "wal":
                    return
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.05)
                continue
            raise ModelRouterValidationError("model router requires WAL journal mode")

    def memory_snapshot(self) -> "ModelRouter":
        """Disposable writable snapshot; backup replaces the entire schema too.

        No migrations run after backup, so an old authority stays old and
        canonical operations fail closed on missing tables/columns.
        """
        snapshot = ModelRouter(clock=self.clock)
        try:
            self.connection.backup(snapshot.connection)
            snapshot.connection.execute("PRAGMA temp_store=MEMORY")
        except BaseException:
            snapshot.close()
            raise
        return snapshot

    @property
    def conn(self) -> sqlite3.Connection:
        return self.connection

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> "ModelRouter":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()

    def _now(self) -> datetime:
        value = self.clock()
        _timestamp(value)
        return value.astimezone(timezone.utc)

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Cursor]:
        if self.read_only:
            raise sqlite3.OperationalError("ModelRouter is read_only")
        if self.connection.in_transaction:
            raise RuntimeError("ModelRouter operation cannot be nested")
        self.connection.execute("BEGIN IMMEDIATE")
        self._authorized = True
        try:
            yield self.connection.cursor()
            self.connection.commit()
        except BaseException:
            self.connection.rollback()
            raise
        finally:
            self._authorized = False

    @staticmethod
    def _register_version(
        cur: sqlite3.Cursor,
        *,
        wire: Mapping[str, Any],
        table: str,
        version_ref_field: str,
        wire_entity_id_field: str,
        db_entity_id_field: str,
        hash_field: str,
        json_field: str,
        kind: str,
    ) -> dict[str, Any]:
        version_ref = wire[version_ref_field]
        entity_id = wire[wire_entity_id_field]
        digest = wire["content_hash"]
        existing = cur.execute(
            f"SELECT {hash_field}, {json_field} FROM {table} WHERE {version_ref_field}=?",
            (version_ref,),
        ).fetchone()
        if existing is not None:
            if existing[hash_field] == digest:
                return {"status": "duplicate", kind: json.loads(existing[json_field])}
            return {
                "status": "conflict",
                "reason": f"{version_ref_field} already has different content",
            }
        latest = cur.execute(
            f"SELECT {version_ref_field}, version FROM {table} "
            f"WHERE {db_entity_id_field}=? ORDER BY version DESC LIMIT 1",
            (entity_id,),
        ).fetchone()
        if latest is None:
            if wire["version"] != 1 or wire["prior_version_ref"] is not None:
                raise ModelRouterConflict(f"first {kind} version must be version 1 without prior")
        else:
            if wire["version"] != latest["version"] + 1:
                raise ModelRouterConflict(f"{kind} version must increase by exactly one")
            if wire["prior_version_ref"] != latest[version_ref_field]:
                raise ModelRouterConflict(f"{kind} prior_version_ref must reference latest version")
        return {"status": "fresh", kind: dict(wire)}

    def register_profile(self, profile: Mapping[str, Any]) -> dict[str, Any]:
        """Append one exact provider/model endpoint profile version."""
        wire = _profile_wire(profile)
        with self._transaction() as cur:
            result = self._register_version(
                cur,
                wire=wire,
                table="model_endpoint_profile_versions",
                version_ref_field="profile_version_ref",
                wire_entity_id_field="id",
                db_entity_id_field="profile_id",
                hash_field="profile_hash",
                json_field="profile_json",
                kind="profile",
            )
            if result["status"] != "fresh":
                return result
            cur.execute(
                "INSERT INTO model_endpoint_profile_versions "
                "(profile_version_ref, profile_id, version, prior_version_ref, provider, model, "
                "family, adapter_ref, credential_slot_ref, profile_hash, profile_json, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    wire["profile_version_ref"],
                    wire["id"],
                    wire["version"],
                    wire["prior_version_ref"],
                    wire["provider"],
                    wire["model"],
                    wire["family"],
                    wire["adapter_ref"],
                    wire["credential_slot_ref"],
                    wire["content_hash"],
                    canonical_json(wire),
                    wire["created_at"],
                ),
            )
            return result

    def declare_profile_metadata(
        self,
        *,
        declaration_ref: str,
        profile_id: str,
        version: int,
        prior_declaration_ref: str | None,
        provider: str,
        model: str,
        family: str,
        capabilities: Sequence[str],
        actor_ref: str,
        created_at: str,
    ) -> dict[str, Any]:
        """Append an owner declaration bound to one exact broker route."""

        body = {
            "schema_version": SCHEMA_VERSION,
            "declaration_ref": _ref(declaration_ref, "declaration_ref"),
            "profile_id": _ref(profile_id, "profile_id"),
            "version": _positive_int(version, "version"),
            "prior_declaration_ref": _ref(
                prior_declaration_ref, "prior_declaration_ref", nullable=True
            ),
            "provider": _token(provider, "provider"),
            "model": _token(model, "model"),
            "family": _token(family, "family"),
            "capabilities": list(_unique_tokens(
                capabilities, "capabilities", nonempty=True
            )),
            "actor_ref": _string(actor_ref, "actor_ref"),
            "created_at": _string(created_at, "created_at"),
        }
        _parse_time(body["created_at"], "created_at")
        record = _metadata_declaration_wire(
            {**body, "content_hash": canonical_hash(body)}
        )
        with self._transaction() as cur:
            existing = cur.execute(
                "SELECT declaration_json FROM model_profile_metadata_declarations "
                "WHERE declaration_ref=?", (record["declaration_ref"],),
            ).fetchone()
            if existing is not None:
                held = _metadata_declaration_wire(
                    json.loads(existing["declaration_json"])
                )
                semantic_keys = set(record) - {"created_at", "content_hash"}
                if any(held.get(key) != record.get(key) for key in semantic_keys):
                    raise ModelRouterConflict("declaration_ref already has different content")
                return {"status": "duplicate", "declaration": held}
            latest = cur.execute(
                "SELECT declaration_ref,version FROM model_profile_metadata_declarations "
                "WHERE profile_id=? ORDER BY version DESC LIMIT 1", (record["profile_id"],),
            ).fetchone()
            if latest is None:
                if record["version"] != 1 or record["prior_declaration_ref"] is not None:
                    raise ModelRouterConflict("first metadata declaration must be version 1")
            elif (record["version"] != latest["version"] + 1
                  or record["prior_declaration_ref"] != latest["declaration_ref"]):
                raise ModelRouterConflict("metadata declaration must extend latest version")
            cur.execute(
                "INSERT INTO model_profile_metadata_declarations "
                "(declaration_ref,profile_id,version,prior_declaration_ref,provider,model,"
                "family,capabilities_json,actor_ref,declaration_hash,declaration_json,created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (record["declaration_ref"], record["profile_id"], record["version"],
                 record["prior_declaration_ref"], record["provider"], record["model"],
                 record["family"], canonical_json(record["capabilities"]),
                 record["actor_ref"], record["content_hash"], canonical_json(record),
                 record["created_at"]),
            )
        return {"status": "fresh", "declaration": record}

    def latest_profile_metadata(self, profile_id: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT * FROM model_profile_metadata_declarations "
            "WHERE profile_id=? ORDER BY version DESC LIMIT 1",
            (_ref(profile_id, "profile_id"),),
        ).fetchone()
        if row is None:
            return None
        record = _metadata_declaration_wire(json.loads(row["declaration_json"]))
        indexed = {
            "declaration_ref": row["declaration_ref"], "profile_id": row["profile_id"],
            "version": row["version"], "prior_declaration_ref": row["prior_declaration_ref"],
            "provider": row["provider"], "model": row["model"], "family": row["family"],
            "capabilities_json": row["capabilities_json"], "actor_ref": row["actor_ref"],
            "declaration_hash": row["declaration_hash"], "created_at": row["created_at"],
        }
        expected = {
            "declaration_ref": record["declaration_ref"], "profile_id": record["profile_id"],
            "version": record["version"], "prior_declaration_ref": record["prior_declaration_ref"],
            "provider": record["provider"], "model": record["model"],
            "family": record["family"], "capabilities_json": canonical_json(record["capabilities"]),
            "actor_ref": record["actor_ref"], "declaration_hash": record["content_hash"],
            "created_at": record["created_at"],
        }
        if indexed != expected:
            raise ModelRouterConflict("metadata declaration index drifted")
        if record["version"] == 1:
            if record["prior_declaration_ref"] is not None:
                raise ModelRouterConflict("first metadata declaration has a prior")
        else:
            prior = self.connection.execute(
                "SELECT declaration_ref FROM model_profile_metadata_declarations "
                "WHERE profile_id=? AND version=?",
                (record["profile_id"], record["version"] - 1),
            ).fetchone()
            if prior is None or prior["declaration_ref"] != record["prior_declaration_ref"]:
                raise ModelRouterConflict("metadata declaration chain drifted")
        return record

    def register_policy(self, policy: Mapping[str, Any]) -> dict[str, Any]:
        """Append one routing-policy version with closed filters/preferences."""
        wire = _policy_wire(policy)
        with self._transaction() as cur:
            result = self._register_version(
                cur,
                wire=wire,
                table="model_routing_policy_versions",
                version_ref_field="policy_version_ref",
                wire_entity_id_field="id",
                db_entity_id_field="policy_id",
                hash_field="policy_hash",
                json_field="policy_json",
                kind="policy",
            )
            if result["status"] != "fresh":
                return result
            cur.execute(
                "INSERT INTO model_routing_policy_versions "
                "(policy_version_ref, policy_id, version, prior_version_ref, policy_hash, "
                "policy_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    wire["policy_version_ref"],
                    wire["id"],
                    wire["version"],
                    wire["prior_version_ref"],
                    wire["content_hash"],
                    canonical_json(wire),
                    wire["created_at"],
                ),
            )
            return result

    def get_profile(self, profile_version_ref: str) -> dict[str, Any]:
        ref = _ref(profile_version_ref, "profile_version_ref")
        row = self.connection.execute(
            "SELECT profile_json FROM model_endpoint_profile_versions WHERE profile_version_ref=?",
            (ref,),
        ).fetchone()
        if row is None:
            raise ModelProfileNotFound(profile_version_ref)
        return json.loads(row["profile_json"])

    def get_policy(self, policy_version_ref: str) -> dict[str, Any]:
        ref = _ref(policy_version_ref, "policy_version_ref")
        row = self.connection.execute(
            "SELECT policy_json FROM model_routing_policy_versions WHERE policy_version_ref=?",
            (ref,),
        ).fetchone()
        if row is None:
            raise RoutingPolicyNotFound(policy_version_ref)
        return json.loads(row["policy_json"])

    def get_decision(self, decision_ref: str) -> dict[str, Any]:
        ref = _ref(decision_ref, "decision_ref")
        row = self.connection.execute(
            "SELECT decision_json FROM model_route_decisions WHERE decision_id=?", (ref,)
        ).fetchone()
        if row is None:
            raise RouteDecisionNotFound(decision_ref)
        return json.loads(row["decision_json"])

    def list_decisions(
        self, *, work_order_id: str | None = None
    ) -> list[dict[str, Any]]:
        if work_order_id is None:
            rows = self.connection.execute(
                "SELECT decision_json FROM model_route_decisions ORDER BY decision_sequence"
            ).fetchall()
        else:
            work_order_id = _string(work_order_id, "work_order_id")
            rows = self.connection.execute(
                "SELECT decision_json FROM model_route_decisions WHERE work_order_id=? "
                "ORDER BY decision_sequence",
                (work_order_id,),
            ).fetchall()
        return [json.loads(row["decision_json"]) for row in rows]

    def latest_profiles(self) -> list[dict[str, Any]]:
        """The current version of every profile, retired ones included.

        The catalog reader needs the retired ones: "which models has this Core
        stopped offering, and against which broker catalog" is exactly the
        question that went unanswered while the two catalogs drifted.
        """

        return self._latest_profiles(self.connection)

    def record_chain_link(
        self,
        *,
        work_order_id: str,
        capability: str,
        attempt_number: int,
        purpose: str,
        tier: str,
        chain_position: int,
        profile_id: str,
        decision_id: str,
        policy_version_ref: str,
        served: bool,
        skip_reason: str | None = None,
    ) -> dict[str, Any]:
        """Append one immutable "this link was tried, and this happened" record.

        The route decision itself cannot carry it: its wire shape is validated
        key-for-key by the broker adapter, and a decision that grew a field
        would stop being admissible.  So the chain is recorded beside the
        decisions it is made of, pointing at them, and a replay reads the two
        together.
        """

        work_order_id = _string(work_order_id, "work_order_id")
        capability = _token(capability, "capability")
        attempt_number = _positive_int(attempt_number, "attempt_number")
        purpose = _token(purpose, "purpose")
        tier = _token(tier, "tier")
        chain_position = _positive_int(chain_position, "chain_position")
        profile_id = _ref(profile_id, "profile_id")
        decision_id = _ref(decision_id, "decision_id")
        policy_version_ref = _ref(policy_version_ref, "policy_version_ref")
        if not isinstance(served, bool):
            raise ModelRouterValidationError("served must be a boolean")
        if served and skip_reason is not None:
            raise ModelRouterValidationError("a served link has no skip reason")
        if not served:
            skip_reason = _token(skip_reason, "skip_reason")
        now = _timestamp(self._now())
        # The identity of a link is what it says, not when it was written.
        # Hashing the clock into the id meant a replay derived a *different*
        # id, missed the duplicate check, and fell through to the schema's
        # UNIQUE(work_order, capability, attempt, position) as a raw
        # sqlite3.IntegrityError -- so replaying a chain crashed the lane
        # instead of returning the decision it had already made.
        identity = {
            "schema_version": SCHEMA_VERSION,
            "work_order_ref": work_order_id,
            "capability": capability,
            "attempt_number": attempt_number,
            "purpose": purpose,
            "tier": tier,
            "chain_position": chain_position,
            "profile_id": profile_id,
            "decision_id": decision_id,
            "policy_version_ref": policy_version_ref,
            "served": served,
            "skip_reason": skip_reason,
        }
        link_id = f"route-chain-link:{canonical_hash(identity)[:32]}"
        link = {**identity, "id": link_id, "created_at": now}
        link["content_hash"] = canonical_hash(link)
        with self._transaction() as cur:
            existing = cur.execute(
                "SELECT link_json FROM model_route_chain_links WHERE link_id=?",
                (link_id,),
            ).fetchone()
            if existing is not None:
                return {"status": "duplicate", "link": json.loads(existing["link_json"])}
            # The same position of the same attempt, recorded differently, is a
            # conflict rather than a crash: the schema would refuse it, and a
            # caller deserves to be told which of the two it is.
            occupied = cur.execute(
                "SELECT link_json FROM model_route_chain_links WHERE work_order_id=? "
                "AND capability=? AND attempt_number=? AND chain_position=?",
                (work_order_id, capability, attempt_number, chain_position),
            ).fetchone()
            if occupied is not None:
                return {
                    "status": "conflict",
                    "reason": "this chain position is already recorded with other content",
                    "link": json.loads(occupied["link_json"]),
                }
            cur.execute(
                "INSERT INTO model_route_chain_links "
                "(link_id, work_order_id, capability, attempt_number, purpose, tier, "
                "chain_position, profile_id, decision_id, served, skip_reason, "
                "policy_version_ref, link_hash, link_json, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    link_id,
                    work_order_id,
                    capability,
                    attempt_number,
                    purpose,
                    tier,
                    chain_position,
                    profile_id,
                    decision_id,
                    int(served),
                    skip_reason,
                    policy_version_ref,
                    link["content_hash"],
                    canonical_json(link),
                    now,
                ),
            )
        return {"status": "fresh", "link": link}

    def chain_links(
        self, *, work_order_id: str | None = None
    ) -> list[dict[str, Any]]:
        """Every recorded chain link, oldest first."""

        if work_order_id is None:
            rows = self.connection.execute(
                "SELECT link_json FROM model_route_chain_links ORDER BY link_sequence"
            ).fetchall()
        else:
            rows = self.connection.execute(
                "SELECT link_json FROM model_route_chain_links WHERE work_order_id=? "
                "ORDER BY link_sequence",
                (_string(work_order_id, "work_order_id"),),
            ).fetchall()
        return [json.loads(row["link_json"]) for row in rows]

    def record_allow_decision(
        self,
        *,
        model_ref: str,
        profile_id: str,
        actor_ref: str,
        config_path: str,
        backup_path: str,
        diff: Mapping[str, Any],
        reload_instruction: str,
    ) -> dict[str, Any]:
        """P14-M2: append the record of one model being let through the broker.

        Content-addressed on what the decision *is* -- who let which model
        through which file, and what changed -- and not on the clock, so
        replaying the same governance call after a dropped reply returns the
        row that is already there instead of writing a second one.  The backup
        path is part of the record rather than a log line: the one question
        anybody asks afterwards is "what did this look like before", and the
        answer has to be findable from the record itself.
        """

        model_ref = _string(model_ref, "model_ref")
        profile_id = _ref(profile_id, "profile_id")
        actor_ref = _string(actor_ref, "actor_ref")
        config_path = _string(config_path, "config_path")
        backup_path = _string(backup_path, "backup_path")
        reload_instruction = _string(reload_instruction, "reload_instruction")
        if not isinstance(diff, Mapping):
            raise ModelRouterValidationError("diff must be an object")
        identity = {
            "model_ref": model_ref,
            "profile_id": profile_id,
            "actor_ref": actor_ref,
            "config_path": config_path,
            "backup_path": backup_path,
            "diff": json.loads(canonical_json(diff)),
        }
        decision_id = f"openclaw-allow-decision:{canonical_hash(identity)[:32]}"
        now = _timestamp(self._now())
        record = {
            "schema_version": SCHEMA_VERSION,
            "id": decision_id,
            "created_at": now,
            "reload_instruction": reload_instruction,
            **identity,
        }
        record["content_hash"] = canonical_hash(record)
        with self._transaction() as cur:
            existing = cur.execute(
                "SELECT decision_json FROM model_openclaw_allow_decisions "
                "WHERE decision_id=?",
                (decision_id,),
            ).fetchone()
            if existing is not None:
                return {
                    "status": "duplicate",
                    "decision": json.loads(existing["decision_json"]),
                }
            cur.execute(
                "INSERT INTO model_openclaw_allow_decisions "
                "(decision_id, model_ref, profile_id, actor_ref, config_path, "
                "backup_path, decision_hash, decision_json, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    decision_id,
                    model_ref,
                    profile_id,
                    actor_ref,
                    config_path,
                    backup_path,
                    record["content_hash"],
                    canonical_json(record),
                    now,
                ),
            )
        return {"status": "fresh", "decision": record}

    def record_fallback_notice(
        self,
        *,
        profile_id: str,
        purpose: str,
        tier: str,
        replacement_profile_id: str | None,
        reason: str,
        message: str,
        detail: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """P14-M2: say once that a stage lost the model it was pointed at.

        Keyed on (model, stage) and on nothing else, deliberately.  The lane
        that writes this runs every hour and will see the same retirement every
        hour for as long as the profile stays retired; a notice keyed on the
        moment would become an hourly alarm, and an alarm that repeats is an
        alarm that gets muted.  ``duplicate`` is therefore the normal answer
        after the first hour.
        """

        profile_id = _ref(profile_id, "profile_id")
        purpose = _string(purpose, "purpose")
        tier = _token(tier, "tier")
        reason = _token(reason, "reason")
        message = _string(message, "message")
        if replacement_profile_id is not None:
            replacement_profile_id = _ref(
                replacement_profile_id, "replacement_profile_id"
            )
        notice_id = ("model-fallback-notice:"
                     + canonical_hash({"profile_id": profile_id, "purpose": purpose})[:32])
        now = _timestamp(self._now())
        notice = {
            "schema_version": SCHEMA_VERSION,
            "id": notice_id,
            "profile_id": profile_id,
            "purpose": purpose,
            "tier": tier,
            "replacement_profile_id": replacement_profile_id,
            "reason": reason,
            "message": message,
            "detail": json.loads(canonical_json(dict(detail or {}))),
            "created_at": now,
        }
        notice["content_hash"] = canonical_hash(notice)
        with self._transaction() as cur:
            existing = cur.execute(
                "SELECT notice_json FROM model_fallback_notices WHERE notice_id=?",
                (notice_id,),
            ).fetchone()
            if existing is not None:
                return {"status": "duplicate",
                        "notice": json.loads(existing["notice_json"])}
            cur.execute(
                "INSERT INTO model_fallback_notices "
                "(notice_id, profile_id, purpose, tier, replacement_profile_id, "
                "reason, notice_hash, notice_json, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    notice_id,
                    profile_id,
                    purpose,
                    tier,
                    replacement_profile_id,
                    reason,
                    notice["content_hash"],
                    canonical_json(notice),
                    now,
                ),
            )
        return {"status": "fresh", "notice": notice}

    def acknowledge_fallback_notice(
        self, *, notice_id: str, actor_ref: str
    ) -> dict[str, Any]:
        """The owner has read one notice.  A second acknowledgement is a no-op."""

        notice_id = _ref(notice_id, "notice_id")
        actor_ref = _string(actor_ref, "actor_ref")
        now = _timestamp(self._now())
        with self._transaction() as cur:
            row = cur.execute(
                "SELECT notice_json FROM model_fallback_notices WHERE notice_id=?",
                (notice_id,),
            ).fetchone()
            if row is None:
                return {"status": "unknown",
                        "reason": f"there is no notice {notice_id}"}
            existing = cur.execute(
                "SELECT actor_ref, created_at FROM model_fallback_notice_acks "
                "WHERE notice_id=?",
                (notice_id,),
            ).fetchone()
            if existing is not None:
                return {
                    "status": "duplicate",
                    "notice": json.loads(row["notice_json"]),
                    "acknowledged_by": existing["actor_ref"],
                    "acknowledged_at": existing["created_at"],
                }
            cur.execute(
                "INSERT INTO model_fallback_notice_acks "
                "(notice_id, actor_ref, created_at) VALUES (?, ?, ?)",
                (notice_id, actor_ref, now),
            )
        return {
            "status": "acknowledged",
            "notice": json.loads(row["notice_json"]),
            "acknowledged_by": actor_ref,
            "acknowledged_at": now,
        }

    def fallback_notices(self, *, open_only: bool = False) -> list[dict[str, Any]]:
        """Every notice, newest first, each carrying its acknowledgement.

        A database opened read-only never runs the schema script, so a router
        written before these tables existed does not have them.  "No table" is
        answered as "no notices" rather than as an error, because the caller is
        the owner's page and a page that fails on an older installation is a
        page that says nothing about the models either.
        """
        try:
            rows = self.connection.execute(
                "SELECT n.notice_json, a.actor_ref AS ack_actor, "
                "a.created_at AS ack_at FROM model_fallback_notices n "
                "LEFT JOIN model_fallback_notice_acks a ON a.notice_id=n.notice_id "
                "ORDER BY n.notice_sequence DESC"
            ).fetchall()
        except sqlite3.OperationalError as exc:
            if "no such table" not in str(exc):
                raise
            return []
        notices: list[dict[str, Any]] = []
        for row in rows:
            if open_only and row["ack_actor"] is not None:
                continue
            notices.append({
                **json.loads(row["notice_json"]),
                "acknowledged_by": row["ack_actor"],
                "acknowledged_at": row["ack_at"],
            })
        return notices

    def allow_decisions(self, *, limit: int | None = None) -> list[dict[str, Any]]:
        """Every recorded allow decision, newest first."""

        sql = ("SELECT decision_json FROM model_openclaw_allow_decisions "
               "ORDER BY decision_sequence DESC")
        parameters: tuple[Any, ...] = ()
        if limit is not None:
            sql += " LIMIT ?"
            parameters = (_positive_int(limit, "limit"),)
        try:
            rows = self.connection.execute(sql, parameters).fetchall()
        except sqlite3.OperationalError as exc:
            # Same reasoning as fallback_notices: a router from before this
            # table existed has no decisions rather than an error.
            if "no such table" not in str(exc):
                raise
            return []
        return [json.loads(row["decision_json"]) for row in rows]

    @staticmethod
    def _work_wire(work_order: WorkOrder | Mapping[str, Any]) -> dict[str, Any]:
        if isinstance(work_order, WorkOrder):
            return work_order.to_dict()
        if not isinstance(work_order, Mapping):
            raise ModelRouterValidationError("work_order must be WorkOrder or mapping")
        try:
            return WorkOrder.from_dict(work_order).to_dict()
        except Exception as exc:
            raise ModelRouterValidationError(str(exc)) from exc

    @staticmethod
    def _latest_profiles(cur: sqlite3.Cursor) -> list[dict[str, Any]]:
        rows = cur.execute(
            "SELECT p.profile_json FROM model_endpoint_profile_versions p "
            "WHERE NOT EXISTS (SELECT 1 FROM model_endpoint_profile_versions newer "
            "WHERE newer.profile_id=p.profile_id AND newer.version>p.version) "
            "ORDER BY p.profile_id, p.profile_version_ref"
        ).fetchall()
        return [json.loads(row["profile_json"]) for row in rows]

    @staticmethod
    def _budget_values(budget: Mapping[str, Any]) -> tuple[dict[str, Any], list[str]]:
        parsed: dict[str, Any] = {}
        reasons: list[str] = []
        for field in _BUDGET_FIELDS:
            if field not in budget:
                reasons.append(f"work_order_budget_missing:{field}")
        if reasons:
            return parsed, reasons
        try:
            parsed["max_input_tokens"] = _positive_int(
                budget["max_input_tokens"], "budget.max_input_tokens"
            )
            parsed["max_output_tokens"] = _positive_int(
                budget["max_output_tokens"], "budget.max_output_tokens"
            )
            parsed["max_total_tokens"] = _positive_int(
                budget["max_total_tokens"], "budget.max_total_tokens"
            )
            parsed["max_cost_usd"] = _decimal(
                budget["max_cost_usd"], "budget.max_cost_usd", positive=True
            )
        except ModelRouterValidationError as exc:
            return {}, [f"work_order_budget_invalid:{exc}"]
        return parsed, []

    @staticmethod
    def _estimate_cost(
        profile: Mapping[str, Any], input_tokens: int, output_tokens: int
    ) -> Decimal:
        cost = profile["cost"]
        return (
            Decimal(str(cost["input_per_million_usd"])) * input_tokens
            + Decimal(str(cost["output_per_million_usd"])) * output_tokens
        ) / Decimal(1_000_000)

    @staticmethod
    def _preference_value(candidate: Mapping[str, Any], field: str) -> Any:
        profile = candidate["profile"]
        if field == "estimated_cost_usd":
            return candidate["estimated_cost_decimal"]
        if field.startswith("context."):
            return profile["context"][field.split(".", 1)[1]]
        if field.startswith("limits."):
            return profile["limits"][field.split(".", 1)[1]]
        if field.startswith("cost."):
            return Decimal(str(profile["cost"][field.split(".", 1)[1]]))
        if field == "profile_id":
            return profile["id"]
        return profile[field]

    @classmethod
    def _sort_candidates(
        cls, candidates: list[dict[str, Any]], preferences: Sequence[Mapping[str, Any]]
    ) -> list[dict[str, Any]]:
        # The implicit final tie-break is always the immutable profile version
        # reference, independent of insertion or SQLite row order.
        ordered = sorted(candidates, key=lambda item: item["profile"]["profile_version_ref"])
        for preference in reversed(preferences):
            ordered.sort(
                key=lambda item, field=preference["field"]: cls._preference_value(
                    item, field
                ),
                reverse=preference["direction"] == "desc",
            )
        return ordered

    @staticmethod
    def _assert_transition(
        cur: sqlite3.Cursor,
        *,
        decision_kind: str,
        previous_decision_ref: str | None,
        work_order_id: str,
        attempt_number: int,
        capability: str,
    ) -> tuple[dict[str, Any] | None, set[str]]:
        latest_row = cur.execute(
            "SELECT decision_id, decision_json FROM model_route_decisions "
            "WHERE work_order_id=? AND capability=? ORDER BY decision_sequence DESC LIMIT 1",
            (work_order_id, capability),
        ).fetchone()
        latest = json.loads(latest_row["decision_json"]) if latest_row else None
        if decision_kind == "initial":
            # Scheduler leases can expire before routing, so its first routed
            # attempt need not be attempt 1.
            if previous_decision_ref is not None:
                raise RouteTransitionError("initial route cannot reference a previous decision")
            if latest is not None:
                raise RouteTransitionError(
                    "route already exists; retry or switch must reference the latest decision"
                )
            return None, set()
        if previous_decision_ref is None:
            raise RouteTransitionError(f"{decision_kind} requires previous_decision_ref")
        if latest is None or latest["id"] != previous_decision_ref:
            raise RouteTransitionError("previous_decision_ref must be the latest route decision")
        if latest["work_order_ref"] != work_order_id or latest["capability"] != capability:
            raise RouteTransitionError("previous route belongs to different work/capability")
        # The same lease-expiry gap can occur between route decisions.  Route
        # lineage must move forward, but it must not invent missing decisions.
        if decision_kind == "retry" and attempt_number <= latest["attempt_number"]:
            raise RouteTransitionError("retry must advance the attempt number")
        if decision_kind == "switch" and attempt_number != latest["attempt_number"]:
            raise RouteTransitionError("switch must remain in the same attempt")
        excluded: set[str] = set()
        if decision_kind == "switch":
            # Walk the same-attempt chain so an explicit switch cannot silently
            # cycle A -> B -> A.
            current = latest
            while current is not None and current["attempt_number"] == attempt_number:
                selected = current.get("selected_profile_version_ref")
                if selected:
                    excluded.add(selected)
                prior = current.get("previous_decision_ref")
                if not prior:
                    break
                row = cur.execute(
                    "SELECT decision_json FROM model_route_decisions WHERE decision_id=?",
                    (prior,),
                ).fetchone()
                current = json.loads(row["decision_json"]) if row else None
        return latest, excluded

    def route(
        self,
        work_order: WorkOrder | Mapping[str, Any],
        *,
        attempt_number: int,
        capability: str,
        policy_version_ref: str,
        credential_slot_refs: Sequence[str],
        required_modalities: Sequence[str],
        required_context_tokens: int,
        estimated_input_tokens: int,
        estimated_output_tokens: int,
        idempotency_key: str,
        decision_kind: str = "initial",
        previous_decision_ref: str | None = None,
        excluded_profile_ids: Sequence[str] = (),
        required_profile_version_ref: str | None = None,
        producer_family: str | None = None,
        tier: str | None = None,
        purpose: str | None = None,
    ) -> dict[str, Any]:
        """Persist one deterministic route decision.

        A selected result and a fail-closed rejection are both immutable route
        decisions.  Repeating the same idempotency key returns ``duplicate``;
        reusing it with different request semantics returns ``conflict``.
        """
        wire = self._work_wire(work_order)
        attempt_number = _positive_int(attempt_number, "attempt_number")
        capability = _token(capability, "capability")
        if capability not in wire["requested_capabilities"]:
            raise ModelRouterValidationError(
                "capability must be declared by the WorkOrder"
            )
        policy_version_ref = _ref(
            policy_version_ref, "policy_version_ref"
        )  # type: ignore[assignment]
        idempotency_key = _string(idempotency_key, "idempotency_key")
        if decision_kind not in _DECISION_KINDS:
            raise ModelRouterValidationError("decision_kind is invalid")
        excluded_profile_ids = tuple(
            _string(item, "excluded_profile_ids") for item in excluded_profile_ids
        )
        if len(set(excluded_profile_ids)) != len(excluded_profile_ids):
            raise ModelRouterValidationError("excluded_profile_ids must be unique")
        required_profile_version_ref = _ref(
            required_profile_version_ref, "required_profile_version_ref", nullable=True)
        previous_decision_ref = _ref(
            previous_decision_ref, "previous_decision_ref", nullable=True
        )
        slots = tuple(_slot(value, "credential_slot_refs[]") for value in credential_slot_refs)
        if len(set(slots)) != len(slots):
            raise ModelRouterValidationError("credential_slot_refs must be unique")
        modalities = _unique_tokens(
            required_modalities, "required_modalities", nonempty=True
        )
        required_context_tokens = _positive_int(
            required_context_tokens, "required_context_tokens"
        )
        estimated_input_tokens = _positive_int(
            estimated_input_tokens, "estimated_input_tokens"
        )
        estimated_output_tokens = _positive_int(
            estimated_output_tokens, "estimated_output_tokens"
        )
        if required_context_tokens < estimated_input_tokens:
            raise ModelRouterValidationError(
                "required_context_tokens cannot be smaller than estimated_input_tokens"
            )
        if producer_family is not None:
            producer_family = _token(producer_family, "producer_family")
        if tier is not None:
            tier = _token(tier, "tier")
        if purpose is not None:
            if not _PURPOSE_RE.fullmatch(_string(purpose, "purpose")):
                raise ModelRouterValidationError(
                    "a purpose is lowercase words joined by _"
                )
        request = {
            "work_order_hash": canonical_hash(wire),
            "work_order_ref": wire["id"],
            "attempt_number": attempt_number,
            "capability": capability,
            "policy_version_ref": policy_version_ref,
            "credential_slot_refs": list(slots),
            "required_modalities": list(modalities),
            "required_context_tokens": required_context_tokens,
            "estimated_input_tokens": estimated_input_tokens,
            "estimated_output_tokens": estimated_output_tokens,
            "decision_kind": decision_kind,
            "previous_decision_ref": previous_decision_ref,
            "producer_family": producer_family,
        }
        if excluded_profile_ids:
            request["excluded_profile_ids"] = list(excluded_profile_ids)
        if required_profile_version_ref is not None:
            request["required_profile_version_ref"] = required_profile_version_ref
        if tier is not None:
            # Part of the request identity -- the same work routed under a
            # different tier is a different request -- but deliberately not part
            # of the decision wire, whose shape the broker adapter validates
            # exactly. Which link of which chain served is recorded next to the
            # decision, in model_route_chain_links.
            request["tier"] = tier
        now_dt = self._now()
        now = _timestamp(now_dt)
        with self._transaction() as cur:
            # The policy is read before the request identity is settled,
            # because whether the purpose belongs in that identity depends on
            # what the policy says about it -- see below.
            policy_row = cur.execute(
                "SELECT policy_json FROM model_routing_policy_versions "
                "WHERE policy_version_ref=?",
                (policy_version_ref,),
            ).fetchone()
            if policy_row is None:
                raise RoutingPolicyNotFound(str(policy_version_ref))
            policy = json.loads(policy_row["policy_json"])
            purpose_entry = (
                (policy.get("purpose_overrides") or {}).get(purpose)
                if purpose is not None else None
            )
            purpose_override = purpose_entry is not None
            purpose_explicit = (
                isinstance(purpose_entry, Mapping)
                and purpose_entry.get("mode") == "explicit"
            )
            if purpose_override:
                # P14-M2. The purpose joins the request identity only when this
                # policy version actually carries a selection for it, and then
                # for a reason: the same work under a different selection is a
                # different request, and without this the idempotency cache
                # would answer the new selection with the old model.
                #
                # Only *then*, though. Adding it unconditionally would change
                # the identity of every route request ever made, so replaying a
                # work order from before selection existed would hash
                # differently, come back ``conflict``, and stop a lane that had
                # done nothing wrong. A policy with no override for this purpose
                # routes exactly as it did before selection existed.
                request["purpose"] = purpose
            request_hash = canonical_hash(request)
            prior_idempotency = cur.execute(
                "SELECT request_hash, result_json FROM model_route_idempotency "
                "WHERE idempotency_key=?",
                (idempotency_key,),
            ).fetchone()
            if prior_idempotency is not None:
                if prior_idempotency["request_hash"] == request_hash:
                    result = json.loads(prior_idempotency["result_json"])
                    result["status"] = "duplicate"
                    return result
                return {
                    "status": "conflict",
                    "reason": "idempotency key already belongs to another route request",
                }
            _previous, switch_exclusions = self._assert_transition(
                cur,
                decision_kind=decision_kind,
                previous_decision_ref=previous_decision_ref,
                work_order_id=wire["id"],
                attempt_number=attempt_number,
                capability=capability,
            )
            budget, global_reasons = self._budget_values(wire["budget"])
            if budget:
                if estimated_input_tokens > budget["max_input_tokens"]:
                    global_reasons.append("work_order_budget_input_exceeded")
                if estimated_output_tokens > budget["max_output_tokens"]:
                    global_reasons.append("work_order_budget_output_exceeded")
                if (
                    estimated_input_tokens + estimated_output_tokens
                    > budget["max_total_tokens"]
                ):
                    global_reasons.append("work_order_budget_total_exceeded")
            filters = policy["filters"]
            if (
                capability in filters["family_independence_capabilities"]
                and producer_family is None
            ):
                global_reasons.append("producer_family_required")
            chain_positions: dict[str, int] | None = None
            live_chain: list[str] = []
            latest_profiles = self._latest_profiles(cur)
            by_id = {profile["id"]: profile for profile in latest_profiles}
            if tier is not None or purpose_explicit:
                resolved = resolve_chain(
                    policy, tier=tier, purpose=purpose, profiles=by_id
                )
                if resolved is None:
                    global_reasons.append("tier_not_declared_by_policy")
                else:
                    chain_positions = {
                        profile_id: index
                        for index, profile_id in enumerate(resolved["chain"])
                    }
                    live_chain = live_links(resolved["chain"], by_id)
            required_modality_set = set(modalities) | set(filters["required_modalities"])
            supplied_slots = set(slots)
            candidates: list[dict[str, Any]] = []
            snapshot: list[dict[str, Any]] = []
            for profile in latest_profiles:
                reasons = list(global_reasons)
                if (required_profile_version_ref is not None
                        and profile["profile_version_ref"] != required_profile_version_ref):
                    reasons.append("not_exact_provider_retry_profile_version")
                if profile["id"] in excluded_profile_ids:
                    reasons.append("excluded_by_provider_retry_history")
                if profile["profile_version_ref"] in switch_exclusions:
                    reasons.append("already_tried_in_switch_chain")
                if profile.get("status") == "retired":
                    reasons.append("profile_retired")
                if chain_positions is not None and profile["id"] not in chain_positions:
                    reasons.append("profile_not_in_tier_chain")
                if profile.get("unpriced"):
                    # P14-M2. A model with no published rate card is registered
                    # at a declared ceiling rather than at its real price, so it
                    # is allowed exactly one place: the end of a chain, where
                    # the choice is between an over-reserved answer and no
                    # answer at all.
                    #
                    # "The end" means the end of what can still be reached.
                    # Measured over the declared chain instead, an unpriced
                    # model sitting in front of a retired one would be refused
                    # for not being last while being the only thing left.
                    if chain_positions is None:
                        reasons.append("unpriced_model_requires_chain")
                    elif not live_chain or profile["id"] != live_chain[-1]:
                        reasons.append("unpriced_model_not_last_link")
                if capability not in profile["capabilities"]:
                    reasons.append("capability_not_supported")
                if not required_modality_set.issubset(set(profile["modalities"])):
                    reasons.append("modality_not_supported")
                if (
                    filters["allowed_profile_ids"]
                    and profile["id"] not in filters["allowed_profile_ids"]
                    and not (purpose_explicit and chain_positions is not None
                             and profile["id"] in chain_positions)
                ):
                    reasons.append("profile_not_allowed")
                if (
                    filters["allowed_providers"]
                    and profile["provider"] not in filters["allowed_providers"]
                ):
                    reasons.append("provider_not_allowed")
                if (
                    filters["allowed_families"]
                    and profile["family"] not in filters["allowed_families"]
                ):
                    reasons.append("family_not_allowed")
                if (
                    filters["allowed_adapter_refs"]
                    and profile["adapter_ref"] not in filters["allowed_adapter_refs"]
                ):
                    reasons.append("adapter_not_allowed")
                availability = profile["availability"]
                if availability["state"] != "available":
                    reasons.append("profile_not_available")
                if _parse_time(availability["checked_at"], "checked_at") > now_dt:
                    reasons.append("availability_check_in_future")
                if _parse_time(availability["valid_until"], "valid_until") <= now_dt:
                    reasons.append("availability_expired")
                if profile["credential_slot_ref"] not in supplied_slots:
                    reasons.append("credential_slot_unavailable")
                if required_context_tokens > profile["context"]["max_context_tokens"]:
                    reasons.append("context_window_insufficient")
                if estimated_output_tokens > profile["context"]["max_output_tokens"]:
                    reasons.append("model_output_limit_insufficient")
                limits = profile["limits"]
                if estimated_input_tokens > limits["max_input_tokens"]:
                    reasons.append("profile_input_limit_exceeded")
                if estimated_output_tokens > limits["max_output_tokens"]:
                    reasons.append("profile_output_limit_exceeded")
                if estimated_input_tokens + estimated_output_tokens > limits["max_total_tokens"]:
                    reasons.append("profile_total_limit_exceeded")
                estimate = self._estimate_cost(
                    profile, estimated_input_tokens, estimated_output_tokens
                )
                if estimate > Decimal(str(limits["max_cost_usd"])):
                    reasons.append("profile_cost_limit_exceeded")
                if budget and estimate > budget["max_cost_usd"]:
                    reasons.append("work_order_cost_budget_exceeded")
                if producer_family is not None and not independent_families(
                    profile["family"], producer_family
                ):
                    reasons.append("model_family_not_independent")
                reasons = sorted(set(reasons))
                item = {
                    "profile_version_ref": profile["profile_version_ref"],
                    "profile_hash": profile["content_hash"],
                    "eligible": not reasons,
                    "rejection_reasons": reasons,
                    "estimated_cost_usd": _money_string(estimate),
                }
                snapshot.append(item)
                if not reasons:
                    candidates.append(
                        {
                            "profile": profile,
                            "estimated_cost_decimal": estimate,
                            "snapshot": item,
                        }
                    )
            snapshot.sort(key=lambda item: item["profile_version_ref"])
            snapshot_hash = canonical_hash(snapshot)
            selected = None
            if candidates:
                ordered = self._sort_candidates(
                    candidates, policy["ordered_preferences"]
                )
                if chain_positions is not None:
                    # Chain order first, the policy's own preferences only to
                    # break a tie within one link. A chain whose order were
                    # decided by cheapest-first would not be a chain: the point
                    # of naming gpt-6-astra before claude-fable-5-1 is that the
                    # first one is wanted even though it is not the cheaper.
                    # sorted() is stable, so the preference order survives.
                    ordered.sort(
                        key=lambda item: chain_positions[item["profile"]["id"]]
                    )
                selected = ordered[0]["profile"]
            outcome = "selected" if selected is not None else "rejected"
            rejected_reasons = sorted(
                set(global_reasons)
                | (
                    {reason for item in snapshot for reason in item["rejection_reasons"]}
                    if selected is None
                    else set()
                )
            )
            if not snapshot:
                rejected_reasons.append("model_directory_empty")
            decision_id = f"route-decision:{uuid.uuid4().hex}"
            decision = {
                "schema_version": SCHEMA_VERSION,
                "id": decision_id,
                "created_at": now,
                "decision_kind": decision_kind,
                "outcome": outcome,
                "work_order_ref": wire["id"],
                "work_order_hash": request["work_order_hash"],
                "attempt_number": attempt_number,
                "capability": capability,
                "policy_version_ref": policy_version_ref,
                "policy_hash": policy["content_hash"],
                "candidate_snapshot_hash": snapshot_hash,
                "candidate_snapshot": snapshot,
                "constraints": {
                    "credential_slot_refs": list(slots),
                    "required_modalities": list(modalities),
                    "required_context_tokens": required_context_tokens,
                    "estimated_input_tokens": estimated_input_tokens,
                    "estimated_output_tokens": estimated_output_tokens,
                    "producer_family": producer_family,
                },
                "selected_profile_version_ref": (
                    selected["profile_version_ref"] if selected else None
                ),
                "selected_profile_hash": selected["content_hash"] if selected else None,
                "selected_endpoint": (
                    {
                        "provider": selected["provider"],
                        "model": selected["model"],
                        "family": selected["family"],
                        "adapter_ref": selected["adapter_ref"],
                        "credential_slot_ref": selected["credential_slot_ref"],
                    }
                    if selected
                    else None
                ),
                "previous_decision_ref": previous_decision_ref,
                "rejection_reasons": rejected_reasons,
                "request_hash": request_hash,
            }
            decision["content_hash"] = canonical_hash(decision)
            cur.execute(
                "INSERT INTO model_route_decisions "
                "(decision_id, decision_kind, outcome, work_order_id, work_order_hash, "
                "attempt_number, capability, policy_version_ref, policy_hash, "
                "candidate_snapshot_hash, selected_profile_version_ref, previous_decision_ref, "
                "request_hash, decision_hash, decision_json, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    decision_id,
                    decision_kind,
                    outcome,
                    wire["id"],
                    request["work_order_hash"],
                    attempt_number,
                    capability,
                    policy_version_ref,
                    policy["content_hash"],
                    snapshot_hash,
                    decision["selected_profile_version_ref"],
                    previous_decision_ref,
                    request_hash,
                    decision["content_hash"],
                    canonical_json(decision),
                    now,
                ),
            )
            result = {"status": "fresh", "decision": decision}
            cur.execute(
                "INSERT INTO model_route_idempotency "
                "(idempotency_key, request_hash, decision_id, result_json, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    idempotency_key,
                    request_hash,
                    decision_id,
                    canonical_json(result),
                    now,
                ),
            )
            return result

    def switch(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        kwargs["decision_kind"] = "switch"
        return self.route(*args, **kwargs)

    def retry(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        kwargs["decision_kind"] = "retry"
        return self.route(*args, **kwargs)


DeterministicModelRouter = ModelRouter
