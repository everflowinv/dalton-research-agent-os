"""P14-M2: which models the gateway has, which it lets through, which we hold.

The owner's instruction was one sentence: the list of models Dalton can use
should follow the providers OpenClaw exposes, and a new model should register
itself as available rather than waiting for somebody to remember.

Three sets are needed to say where a model actually is, and they are three
because there are three gates, not one:

``in_openclaw_not_allowed``
    the provider catalog offers it and the broker plugin does not list it in
    ``llm.allowedModels``.  Dalton cannot reach it; the gateway could.  This is
    the set the cockpit's 「放行」 button acts on, and letting one through is a
    write to ``openclaw.json`` and therefore the owner's decision, never a
    lane's.

``allowed_not_in_dalton``
    the broker allows it and this Core holds no live profile for it.  Normally
    empty within the hour: the catalog lane registers what the broker offers.
    A model that stays here has no ``config.profiles`` entry, which is the one
    thing the broker's own catalog does not derive from ``allowedModels``.

``dalton_not_in_openclaw``
    this Core holds a live profile for a model the provider catalog no longer
    offers.  Not deleted -- a route decision from June names the profile
    version and a version chain with a hole in it cannot be replayed -- but
    retired by the existing catalog sync, which appends a version saying so.

Nothing here writes.  It reads two subtrees of a configuration file --
``models.providers`` and the broker plugin's own entry -- and touches no
credential, key, header or provider setting on the way past.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

from .model_router import ModelRouter
from .openclaw_catalog_reconcile import (
    OpenClawCatalogError,
    _broker_profiles,
    _provider_models,
)

BROKER_PLUGIN_ID = "dalton-openclaw-model-broker"
_PROFILE_ID_RE = re.compile(r"^profile:[A-Za-z0-9][A-Za-z0-9._:/+-]*$")


class ModelDiscoveryError(OpenClawCatalogError):
    """The public model portion of an OpenClaw config cannot be read."""


def _broker_entry(config: Mapping[str, Any]) -> Mapping[str, Any]:
    plugins = config.get("plugins")
    entries = (plugins or {}).get("entries") if isinstance(plugins, Mapping) else None
    entry = (entries or {}).get(BROKER_PLUGIN_ID) if isinstance(entries, Mapping) else None
    if entry is None:
        return {}
    if not isinstance(entry, Mapping):
        raise ModelDiscoveryError("the broker plugin entry must be an object")
    return entry


def allowed_model_refs(config: Mapping[str, Any]) -> tuple[str, ...]:
    """The models the broker plugin will pass through, in declaration order.

    Order is kept because ``allowedModels`` is a list the owner reads, and a
    patch that reorders it would show up in a diff as a change nobody made.
    """

    entry = _broker_entry(config)
    llm = entry.get("llm") or {}
    if not isinstance(llm, Mapping):
        raise ModelDiscoveryError("the broker plugin's llm settings must be an object")
    allowed = llm.get("allowedModels", [])
    if not isinstance(allowed, list):
        raise ModelDiscoveryError("allowedModels must be an array")
    refs: list[str] = []
    for item in allowed:
        if not isinstance(item, str) or "/" not in item:
            raise ModelDiscoveryError("an allowed model is provider/model")
        if item not in refs:
            refs.append(item)
    return tuple(refs)


def provider_models(config: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """Every model ``models.providers`` offers, keyed ``provider/model``."""

    return _provider_models(config)


def broker_profiles(config: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """Every profile the broker plugin declares, keyed by profile id."""

    return _broker_profiles(config)


def _held_profiles(router: ModelRouter) -> dict[str, dict[str, Any]]:
    """This Core's ``profile:`` catalog at its latest version, by id.

    Scoped to the ``profile:`` namespace exactly as the catalog sync is: the
    six pre-broker ``model-profile:`` ids were never described by any broker
    catalog, so measuring them against one would assert something it does not
    say.
    """

    return {
        profile["id"]: profile
        for profile in router.latest_profiles()
        if _PROFILE_ID_RE.fullmatch(str(profile.get("id", "")))
    }


def discover_models(
    config: Mapping[str, Any],
    *,
    router: ModelRouter | None = None,
    checked_at: datetime | None = None,
) -> dict[str, Any]:
    """The three diff sets, by name, plus what each side actually holds.

    ``router`` is optional so the two halves can be answered separately: the
    OpenClaw half needs no database, and a report that only wants "what does
    the gateway offer that the broker does not pass" should not have to open
    one.
    """

    providers = provider_models(config)
    allowed = allowed_model_refs(config)
    profiles = broker_profiles(config)
    profile_refs = {item["model_ref"] for item in profiles.values()}
    allowed_set = set(allowed)

    # Gate one: the gateway offers it, the broker does not pass it.
    in_openclaw_not_allowed = sorted(set(providers) - allowed_set)
    # Allowed but with no profile object: the broker itself cannot serve it,
    # because a Dalton call names a profile id and there is none to name.
    allowed_without_profile = sorted(allowed_set - profile_refs)
    unpriced = sorted(ref for ref, item in providers.items() if item["unpriced"])

    result: dict[str, Any] = {
        "schema_version": "0.1",
        "provider_model_refs": sorted(providers),
        "allowed_model_refs": list(allowed),
        "broker_profile_ids": sorted(profiles),
        "in_openclaw_not_allowed": in_openclaw_not_allowed,
        "allowed_without_broker_profile": allowed_without_profile,
        "unpriced_model_refs": unpriced,
        "allowed_not_in_dalton": [],
        "dalton_not_in_openclaw": [],
        "live_profile_ids": [],
        "retired_profile_ids": [],
        "dalton_read": router is not None,
    }
    if checked_at is not None:
        result["checked_at"] = checked_at.isoformat(timespec="microseconds")
    if router is None:
        return result

    held = _held_profiles(router)
    live = {
        profile_id: profile
        for profile_id, profile in held.items()
        if profile.get("status") != "retired"
    }
    live_refs = {
        profile_id: f"{profile['provider']}/{profile['model']}"
        for profile_id, profile in live.items()
    }
    # Gate two: the broker passes it and this Core has no live profile for it.
    # Measured by profile id, because that is what a Dalton call names; a model
    # the broker allows without a profile object is named in its own set above
    # and cannot be registered here whatever the lane does.
    servable = {
        profile_id: item["model_ref"]
        for profile_id, item in profiles.items()
        if item["model_ref"] in allowed_set
    }
    result["allowed_not_in_dalton"] = sorted(set(servable) - set(live))
    # Gate three: this Core holds a live profile for a model the provider
    # catalog no longer offers. The catalog sync retires these; it never
    # deletes one.
    result["dalton_not_in_openclaw"] = sorted(
        profile_id for profile_id, ref in live_refs.items() if ref not in providers
    )
    result["live_profile_ids"] = sorted(live)
    result["retired_profile_ids"] = sorted(set(held) - set(live))
    result["unpriced_profile_ids"] = sorted(
        profile_id for profile_id, profile in live.items() if profile.get("unpriced")
    )
    result["in_sync"] = not (
        result["allowed_not_in_dalton"] or result["dalton_not_in_openclaw"]
    )
    return result


def summarise(discovery: Mapping[str, Any]) -> dict[str, Any]:
    """Counts only -- for a tick summary, which must never carry a model list."""

    keys: Sequence[str] = (
        "in_openclaw_not_allowed",
        "allowed_not_in_dalton",
        "dalton_not_in_openclaw",
        "allowed_without_broker_profile",
        "unpriced_model_refs",
    )
    return {key: len(discovery.get(key) or []) for key in keys}


__all__ = [
    "BROKER_PLUGIN_ID",
    "ModelDiscoveryError",
    "allowed_model_refs",
    "broker_profiles",
    "discover_models",
    "provider_models",
    "summarise",
]
