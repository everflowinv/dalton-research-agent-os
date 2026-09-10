"""Reconcile Dalton verifier candidates with a live OpenClaw model catalog.

Only public routing metadata is copied from the OpenClaw configuration.  API
keys, headers, environment variables, and all other provider configuration are
ignored by construction.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
import math
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .model_deployment import ADAPTER_REF, openclaw_broker_profiles
from .model_router import (
    RETIRED_REASON_NOT_IN_BROKER,
    ModelRouter,
    canonical_hash,
    canonical_json,
)


_BROKER_PLUGIN_ID = "dalton-openclaw-model-broker"
# P14-M2: what a model with no published rate card is charged at.
#
# Not zero, and not a guess at the real price either -- a *declared ceiling*,
# chosen to sit above every price the live catalog publishes, including the
# tiered top of the dearest model (gpt-6-astra above 272k tokens: 20 in / 75
# out). A reservation made at this number over-reserves, which is the direction
# an unknown has to fail in: the day budget refuses a little early rather than
# spending a lot without noticing. Settlement uses the same number, so an
# unpriced call shows up in the day's spend as at least what it could have
# cost, and the model still may only be a chain's last link.
UNPRICED_CEILING_INPUT_PER_MILLION_USD = 25.0
UNPRICED_CEILING_OUTPUT_PER_MILLION_USD = 100.0
_PROFILE_ID_RE = re.compile(r"^profile:[A-Za-z0-9][A-Za-z0-9._:/+-]*$")
_TOKEN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/+-]*$")


class OpenClawCatalogError(ValueError):
    """The public model-routing portion of an OpenClaw config is invalid."""


def _mapping(value: Any, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise OpenClawCatalogError(f"{name} must be an object")
    return value


def _sequence(value: Any, name: str) -> Sequence[Any]:
    if not isinstance(value, list):
        raise OpenClawCatalogError(f"{name} must be an array")
    return value


def _positive_int(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise OpenClawCatalogError(f"{name} must be a positive integer")
    return value


def _nonnegative_number(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        raise OpenClawCatalogError(f"{name} must be a non-negative number")
    return float(value)


def _wire_time(value: datetime) -> str:
    if value.tzinfo is None:
        raise OpenClawCatalogError("checked_at must include a timezone")
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")


def load_openclaw_config(path: str | Path) -> dict[str, Any]:
    """Load one config with duplicate-key rejection and no secret projection."""

    def reject_duplicates(items: list[tuple[str, Any]]) -> dict[str, Any]:
        output: dict[str, Any] = {}
        for key, value in items:
            if key in output:
                raise OpenClawCatalogError(f"duplicate JSON key: {key}")
            output[key] = value
        return output

    try:
        loaded = json.loads(
            Path(path).expanduser().read_text(encoding="utf-8"),
            object_pairs_hook=reject_duplicates,
        )
    except (OSError, json.JSONDecodeError) as exc:
        raise OpenClawCatalogError(f"cannot load OpenClaw config: {exc}") from exc
    return dict(_mapping(loaded, "OpenClaw config"))


def _provider_models(config: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    models = _mapping(config.get("models", {}), "models")
    providers = _mapping(models.get("providers", {}), "models.providers")
    output: dict[str, dict[str, Any]] = {}
    for provider, raw_provider in providers.items():
        if not isinstance(provider, str) or not _TOKEN_RE.fullmatch(provider):
            raise OpenClawCatalogError("provider id must be a canonical token")
        provider_obj = _mapping(raw_provider, f"provider {provider}")
        for raw_model in _sequence(provider_obj.get("models", []), f"provider {provider}.models"):
            model = _mapping(raw_model, f"provider {provider} model")
            model_id = model.get("id")
            if not isinstance(model_id, str) or not _TOKEN_RE.fullmatch(model_id):
                raise OpenClawCatalogError(f"provider {provider} has an invalid model id")
            model_ref = f"{provider}/{model_id}"
            if model_ref in output:
                raise OpenClawCatalogError(f"duplicate provider model: {model_ref}")
            context_window = _positive_int(
                model.get("contextWindow"), f"{model_ref}.contextWindow"
            )
            max_tokens = _positive_int(model.get("maxTokens"), f"{model_ref}.maxTokens")
            cost = _mapping(model.get("cost", {}), f"{model_ref}.cost")
            # P14-M2: a model the gateway offers with no published rate card is
            # read, marked, and registered anyway.  Refusing to parse it would
            # make the whole catalog unreadable because of one entry -- and the
            # entry we would be hiding is exactly the one worth seeing.  What
            # the mark buys is a routing rule: an unpriced model may only ever
            # be a chain's last link, because a call whose cost cannot be
            # estimated cannot be admitted against a day budget honestly.
            unpriced = cost.get("input") is None or cost.get("output") is None
            output[model_ref] = {
                "provider": provider,
                "model": model_id,
                "model_ref": model_ref,
                "context_window": context_window,
                "max_output_tokens": max_tokens,
                "unpriced": unpriced,
                # ``None``, never zero. A zero rate card is not "we do not know
                # what this costs" -- it is "this is free", which the router
                # would sort first and the day ledger would settle at nothing.
                # The caller decides what to do with not-knowing; it must not
                # be able to mistake it for knowing.
                "input_cost": (
                    None if unpriced
                    else _nonnegative_number(cost.get("input"), f"{model_ref}.cost.input")
                ),
                "output_cost": (
                    None if unpriced
                    else _nonnegative_number(cost.get("output"), f"{model_ref}.cost.output")
                ),
            }
    return output


def _provider_controls_valid(value: Any, checked_at: datetime | None = None) -> bool:
    if not isinstance(value, Mapping) or set(value) != {"mode", "rateCard"}:
        return False
    if value["mode"] not in {
        "openai-responses-input-count-v1",
        "google-generative-ai-count-tokens-v1",
    }:
        return False
    rate = value["rateCard"]
    if not isinstance(rate, Mapping) or set(rate) != {
        "inputPerMillionUsd", "outputPerMillionUsd", "validUntil"
    }:
        return False
    for key in ("inputPerMillionUsd", "outputPerMillionUsd"):
        if isinstance(rate[key], bool):
            return False
        try:
            number = float(rate[key])
        except (TypeError, ValueError, OverflowError):
            return False
        if not math.isfinite(number) or number <= 0:
            return False
    try:
        expires = datetime.fromisoformat(rate["validUntil"].replace("Z", "+00:00"))
    except (TypeError, ValueError, AttributeError):
        return False
    if expires.tzinfo is None:
        return False
    return checked_at is None or expires.astimezone(timezone.utc) > checked_at


def _broker_profiles(
    config: Mapping[str, Any], *, checked_at: datetime | None = None
) -> dict[str, dict[str, Any]]:
    plugins = _mapping(config.get("plugins", {}), "plugins")
    entries = _mapping(plugins.get("entries", {}), "plugins.entries")
    plugin = _mapping(entries.get(_BROKER_PLUGIN_ID, {}), f"plugin {_BROKER_PLUGIN_ID}")
    plugin_config = _mapping(plugin.get("config", {}), f"plugin {_BROKER_PLUGIN_ID}.config")
    output: dict[str, dict[str, Any]] = {}
    for raw_profile in _sequence(plugin_config.get("profiles", []), "broker profiles"):
        profile = _mapping(raw_profile, "broker profile")
        profile_id = profile.get("id")
        model_ref = profile.get("model")
        if not isinstance(profile_id, str) or not _PROFILE_ID_RE.fullmatch(profile_id):
            raise OpenClawCatalogError("broker profile id is invalid")
        if not isinstance(model_ref, str) or "/" not in model_ref:
            raise OpenClawCatalogError(f"broker profile {profile_id} has an invalid model")
        provider, model = model_ref.split("/", 1)
        if not _TOKEN_RE.fullmatch(provider) or not _TOKEN_RE.fullmatch(model):
            raise OpenClawCatalogError(f"broker profile {profile_id} has an invalid model")
        if profile_id in output:
            raise OpenClawCatalogError(f"duplicate broker profile id: {profile_id}")
        provider_controls = profile.get("providerControls")
        controls_declared = _provider_controls_valid(provider_controls, checked_at)
        output[profile_id] = {
            "id": profile_id,
            "provider": provider,
            "model": model,
            "model_ref": model_ref,
            "max_tokens": profile.get("maxTokens"),
            # Public broker-side admission. The broker still verifies the host
            # runtime's matching advertised transport before provider use.
            "provider_controls": controls_declared,
        }
    return output


def _static_routes(checked_at: datetime) -> dict[str, dict[str, Any]]:
    return {
        profile["id"]: {
            "profile": profile,
            "model_ref": f"{profile['provider']}/{profile['model']}",
        }
        for profile in openclaw_broker_profiles(
            checked_at=checked_at, availability_ttl=timedelta(days=7)
        )
    }


def reconcile_openclaw_model_catalog(
    config: Mapping[str, Any],
    *,
    checked_at: datetime,
    calibrated_profile_ids: Sequence[str] = (),
) -> dict[str, Any]:
    """Return a secret-free drift report and the profiles needing a smoke test."""

    providers = _provider_models(config)
    brokers = _broker_profiles(config, checked_at=checked_at)
    static = _static_routes(checked_at)
    configured_refs = set(providers)
    broker_refs = {item["model_ref"] for item in brokers.values()}
    static_ids = set(static)
    broker_ids = set(brokers)
    changed_ids = sorted(
        profile_id
        for profile_id in static_ids & broker_ids
        if static[profile_id]["model_ref"] != brokers[profile_id]["model_ref"]
    )
    new_ids = sorted(broker_ids - static_ids)
    missing_ids = sorted(static_ids - broker_ids)
    calibrated = set(calibrated_profile_ids)
    smoke_required = sorted((set(new_ids) | set(changed_ids)) - calibrated)
    return {
        "schema_version": "0.1",
        "checked_at": _wire_time(checked_at),
        "provider_model_count": len(providers),
        "broker_profile_count": len(brokers),
        "static_profile_count": len(static),
        "provider_model_refs": sorted(configured_refs),
        "broker_profile_ids": sorted(broker_ids),
        "missing_broker_model_refs": sorted(configured_refs - broker_refs),
        "orphan_broker_model_refs": sorted(broker_refs - configured_refs),
        "new_broker_profile_ids": new_ids,
        "changed_broker_profile_ids": changed_ids,
        "missing_static_profile_ids": missing_ids,
        "smoke_required_profile_ids": smoke_required,
        "catalog_in_sync": not any(
            (
                configured_refs - broker_refs,
                broker_refs - configured_refs,
                new_ids,
                changed_ids,
                missing_ids,
            )
        ),
    }


def openclaw_broker_profiles_from_config(
    config: Mapping[str, Any],
    *,
    checked_at: datetime,
    availability_ttl: timedelta = timedelta(days=7),
    profile_ids: Sequence[str] | None = None,
    metadata_declarations: Mapping[str, Mapping[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Project explicitly brokered models into Dalton's current catalog.

    Route, price and capacity come from the current public broker catalog.
    Curated profile IDs supply initial role metadata; changed and unknown
    routes need an exact-route Dalton declaration to establish lineage.
    The synchronizer appends versions when this projection changes, retaining
    immutable history. Models absent from the provider catalog are refused.
    """

    if availability_ttl.total_seconds() <= 0:
        raise OpenClawCatalogError("availability_ttl must be positive")
    providers = _provider_models(config)
    brokers = _broker_profiles(config, checked_at=checked_at)
    if profile_ids is not None:
        selected = set(profile_ids)
        if not selected or any(
            not isinstance(profile_id, str)
            or not _PROFILE_ID_RE.fullmatch(profile_id)
            for profile_id in selected
        ):
            raise OpenClawCatalogError("profile_ids must be canonical profile ids")
        missing = selected - set(brokers)
        if missing:
            raise OpenClawCatalogError(
                f"requested broker profiles are unavailable: {sorted(missing)}"
            )
        brokers = {
            profile_id: broker
            for profile_id, broker in brokers.items()
            if profile_id in selected
        }
    static = _static_routes(checked_at)
    created = _wire_time(checked_at)
    valid_until = _wire_time(checked_at + availability_ttl)
    output: list[dict[str, Any]] = []
    for profile_id, broker in brokers.items():
        model_ref = broker["model_ref"]
        provider_model = providers.get(model_ref)
        if provider_model is None:
            raise OpenClawCatalogError(
                f"broker profile {profile_id} references unknown model {model_ref}"
            )
        declaration = (metadata_declarations or {}).get(profile_id)
        if declaration is not None and (
            declaration.get("provider") != provider_model["provider"]
            or declaration.get("model") != provider_model["model"]
        ):
            declaration = None
        static_route = static.get(profile_id)
        if static_route is not None:
            profile = copy.deepcopy(static_route["profile"])
            route_changed = static_route["model_ref"] != model_ref
            profile["provider"] = provider_model["provider"]
            profile["model"] = provider_model["model"]
            profile["credential_slot_ref"] = (
                f"credential-slot:openclaw:{provider_model['provider']}"
            )
            if route_changed:
                profile["family"] = f"unclassified:{provider_model['provider']}"
            if declaration is not None:
                profile["family"] = declaration["family"]
                profile["capabilities"] = list(declaration["capabilities"])
            if broker["provider_controls"]:
                profile["capabilities"] = list(dict.fromkeys(
                    [*profile["capabilities"], "provider-controlled-verify"]
                ))
            else:
                profile["capabilities"] = [
                    item for item in profile["capabilities"]
                    if item != "provider-controlled-verify"
                ]
            context_window = provider_model["context_window"]
            max_output = provider_model["max_output_tokens"]
            broker_max = broker["max_tokens"]
            if broker_max is not None:
                max_output = min(
                    max_output,
                    _positive_int(broker_max, f"{profile_id}.maxTokens"),
                )
            profile["context"] = {
                "max_context_tokens": context_window,
                "max_output_tokens": max_output,
            }
            if provider_model["unpriced"]:
                profile["cost"] = {
                    "currency": "USD",
                    "input_per_million_usd": UNPRICED_CEILING_INPUT_PER_MILLION_USD,
                    "output_per_million_usd": UNPRICED_CEILING_OUTPUT_PER_MILLION_USD,
                }
                profile["unpriced"] = True
            else:
                profile["cost"] = {
                    "currency": "USD",
                    "input_per_million_usd": provider_model["input_cost"],
                    "output_per_million_usd": provider_model["output_cost"],
                }
                profile.pop("unpriced", None)
            profile["limits"] = {
                "max_input_tokens": max(1, context_window - max_output),
                "max_output_tokens": max_output,
                "max_total_tokens": context_window,
                "max_cost_usd": 250.0,
            }
            profile["created_at"] = created
            profile["availability"] = {
                "state": "available",
                "checked_at": created,
                "valid_until": valid_until,
            }
            output.append(profile)
            continue

        context_window = provider_model["context_window"]
        max_output = provider_model["max_output_tokens"]
        broker_max = broker["max_tokens"]
        if broker_max is not None:
            max_output = min(max_output, _positive_int(broker_max, f"{profile_id}.maxTokens"))
        max_input = max(1, context_window - max_output)
        public_snapshot = {
            "profile_id": profile_id,
            "model_ref": model_ref,
            "context_window": context_window,
            "max_output_tokens": max_output,
            "input_cost": provider_model["input_cost"],
            "output_cost": provider_model["output_cost"],
            "unpriced": provider_model["unpriced"],
        }
        digest = hashlib.sha256(canonical_json(public_snapshot).encode("utf-8")).hexdigest()[:16]
        slug = profile_id.removeprefix("profile:")
        dynamic: dict[str, Any] = {
            "schema_version": "0.1",
            "profile_version_ref": f"model-profile-version:dynamic-{slug}-{digest}:1",
            "id": profile_id,
            "version": 1,
            "created_at": created,
            "prior_version_ref": None,
            "provider": provider_model["provider"],
            "model": provider_model["model"],
            "family": ((declaration or {}).get("family")
                       or f"unclassified:{provider_model['provider']}"),
            "adapter_ref": ADAPTER_REF,
            "credential_slot_ref": f"credential-slot:openclaw:{provider_model['provider']}",
            # An unknown catalog entry is visible and priceable, but it is not
            # silently certified for hard research or independent verification.
            "capabilities": list((declaration or {}).get("capabilities") or ["research"]),
            "modalities": ["text"],
            "context": {
                "max_context_tokens": context_window,
                "max_output_tokens": max_output,
            },
            "availability": {
                "state": "available",
                "checked_at": created,
                "valid_until": valid_until,
            },
            "cost": {
                "currency": "USD",
                "input_per_million_usd": (
                    UNPRICED_CEILING_INPUT_PER_MILLION_USD
                    if provider_model["unpriced"]
                    else provider_model["input_cost"]
                ),
                "output_per_million_usd": (
                    UNPRICED_CEILING_OUTPUT_PER_MILLION_USD
                    if provider_model["unpriced"]
                    else provider_model["output_cost"]
                ),
            },
            "limits": {
                "max_input_tokens": max_input,
                "max_output_tokens": max_output,
                "max_total_tokens": context_window,
                "max_cost_usd": 250.0,
            },
        }
        if broker["provider_controls"]:
            dynamic["capabilities"] = list(dict.fromkeys(
                [*dynamic["capabilities"], "provider-controlled-verify"]
            ))
        if provider_model["unpriced"]:
            dynamic["unpriced"] = True
        output.append(dynamic)
    return output


def broker_catalog_hash(config: Mapping[str, Any]) -> str:
    """The digest of exactly what the broker offers, and nothing else.

    A retirement is an assertion about the world -- "the broker stopped
    offering this" -- and an assertion that carries no evidence cannot be
    argued with a year later.  So the retired version records the hash of the
    broker catalog that proved it: profile ids, model references, and whether
    each profile publicly declares the controls required by verifier calls.
    Secrets and rate-card values remain outside this digest.
    """

    brokers = _broker_profiles(config)
    return canonical_hash(
        [
            {"id": profile_id, "model": brokers[profile_id]["model_ref"],
             "provider_controls": brokers[profile_id]["provider_controls"]}
            for profile_id in sorted(brokers)
        ]
    )


def _retired_version(
    latest: Mapping[str, Any], *, checked_at: datetime, catalog_hash: str
) -> dict[str, Any]:
    """The next version of a profile, saying it is no longer offered."""

    created = _wire_time(checked_at)
    retired = {
        key: copy.deepcopy(value)
        for key, value in latest.items()
        if key not in {"content_hash", "status", "retirement"}
    }
    version = int(latest["version"]) + 1
    root, separator, tail = str(latest["profile_version_ref"]).rpartition(":")
    if separator and tail.isdigit():
        version_ref = f"{root}:{version}"
    else:
        slug = str(latest["id"]).removeprefix("profile:")
        version_ref = f"model-profile-version:retired-{slug}-{catalog_hash[:16]}:{version}"
    retired.update({
        "version": version,
        "prior_version_ref": latest["profile_version_ref"],
        "profile_version_ref": version_ref,
        "created_at": created,
        # Belt and braces: routing refuses a retired profile outright, and a
        # reader that only knows about availability still sees it is gone.
        "availability": {
            "state": "unavailable",
            "checked_at": created,
            "valid_until": _wire_time(checked_at + timedelta(days=7)),
        },
        "status": "retired",
        "retirement": {
            "reason": RETIRED_REASON_NOT_IN_BROKER,
            "retired_at": created,
            "broker_catalog_hash": catalog_hash,
        },
    })
    return retired


def _revived_version(
    latest: Mapping[str, Any], desired: Mapping[str, Any], *, checked_at: datetime
) -> dict[str, Any]:
    """The next version of a retired profile, saying it is offered again."""

    revived = {
        key: copy.deepcopy(value)
        for key, value in desired.items()
        if key not in {"content_hash", "status", "retirement"}
    }
    version = int(latest["version"]) + 1
    root, separator, tail = str(latest["profile_version_ref"]).rpartition(":")
    revived.update({
        "version": version,
        "prior_version_ref": latest["profile_version_ref"],
        "profile_version_ref": (
            f"{root}:{version}" if separator and tail.isdigit()
            else f"{desired['profile_version_ref']}-revived-{version}"
        ),
        "created_at": _wire_time(checked_at),
    })
    return revived


def _router_broker_profiles(router: ModelRouter) -> dict[str, dict[str, Any]]:
    """Every ``profile:`` the router holds, at its latest version.

    Scoped to the broker id namespace on purpose.  The six ``model-profile:``
    ids are the pre-broker catalog: nothing routes to them, no broker catalog
    has ever described them, and retiring them against a broker catalog would
    be asserting something that catalog does not say.
    """

    return {
        profile["id"]: profile
        for profile in router.latest_profiles()
        if _PROFILE_ID_RE.fullmatch(str(profile.get("id", "")))
    }


def _profile_semantics(profile: Mapping[str, Any]) -> dict[str, Any]:
    """Public route meaning, excluding version and observation timestamps."""

    return {
        key: copy.deepcopy(value)
        for key, value in profile.items()
        if key not in {
            "schema_version", "profile_version_ref", "version", "created_at",
            "prior_version_ref", "availability", "content_hash", "status", "retirement",
        }
    }


def _metadata_declarations(router: ModelRouter) -> dict[str, dict[str, Any]]:
    exists = router.connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' "
        "AND name='model_profile_metadata_declarations'"
    ).fetchone()
    if exists is None:
        return {}
    rows = router.connection.execute(
        "SELECT DISTINCT profile_id FROM model_profile_metadata_declarations"
    ).fetchall()
    records = [router.latest_profile_metadata(row["profile_id"]) for row in rows]
    return {record["profile_id"]: record for record in records}


def _changed_version(
    latest: Mapping[str, Any], desired: Mapping[str, Any], *, checked_at: datetime
) -> dict[str, Any]:
    version = int(latest["version"]) + 1
    digest = canonical_hash(_profile_semantics(desired))[:16]
    wire = copy.deepcopy(dict(desired))
    wire.update({
        "profile_version_ref": (
            f"model-profile-version:broker-{desired['id'].removeprefix('profile:')}"
            f"-{digest}:{version}"
        ),
        "version": version,
        "prior_version_ref": latest["profile_version_ref"],
        "created_at": _wire_time(checked_at),
    })
    wire.pop("content_hash", None)
    return wire


def _availability_expired(profile: Mapping[str, Any], checked_at: datetime) -> bool:
    # Profiles come through ModelRouter validation, including timezone-aware
    # availability bounds. Keep the prior observation immutable when renewing.
    valid_until = datetime.fromisoformat(
        profile["availability"]["valid_until"].replace("Z", "+00:00")
    )
    return valid_until <= checked_at


def catalog_sync_status(
    router: ModelRouter,
    config: Mapping[str, Any],
    *,
    checked_at: datetime,
) -> dict[str, Any]:
    """Read-only: are the two catalogs the same set, and if not, which way?

    ``catalog_in_sync`` is true when every model the broker offers has a live
    profile here and every profile here that is not retired is offered by the
    broker.  Retired profiles are deliberately outside both halves: they are
    the record of a model that used to be offered, and holding them against the
    live catalog would make sync unreachable by construction.
    """

    brokers = _broker_profiles(config)
    held = _router_broker_profiles(router)
    desired = {
        profile["id"]: profile
        for profile in openclaw_broker_profiles_from_config(
            config, checked_at=checked_at,
            metadata_declarations=_metadata_declarations(router),
        )
    }
    live = {
        profile_id
        for profile_id, profile in held.items()
        if profile.get("status") != "retired"
    }
    retired = sorted(set(held) - live)
    missing_here = sorted(set(brokers) - set(held))
    retired_but_offered = sorted(set(brokers) & set(retired))
    not_offered = sorted(live - set(brokers))
    drifted = sorted(
        profile_id for profile_id in live & set(desired)
        if canonical_json(_profile_semantics(held[profile_id]))
        != canonical_json(_profile_semantics(desired[profile_id]))
    )
    return {
        "schema_version": "0.1",
        "checked_at": _wire_time(checked_at),
        "broker_catalog_hash": broker_catalog_hash(config),
        "broker_profile_ids": sorted(brokers),
        "live_profile_ids": sorted(live),
        "retired_profile_ids": retired,
        # The two diff sets, by name, that a report or a cockpit panel shows.
        "missing_static_profile_ids": missing_here + retired_but_offered,
        "not_in_broker_profile_ids": not_offered,
        "drifted_profile_ids": drifted,
        "expired_availability_profile_ids": sorted(
            profile_id for profile_id in live & set(desired)
            if _availability_expired(held[profile_id], checked_at)
        ),
        "catalog_in_sync": (
            not missing_here and not retired_but_offered and not not_offered and not drifted
        ),
    }


def sync_openclaw_model_catalog(
    router: ModelRouter,
    config: Mapping[str, Any],
    *,
    checked_at: datetime,
    availability_ttl: timedelta = timedelta(days=7),
) -> dict[str, Any]:
    """Make the router's catalog agree with the broker's, append-only.

    Three moves, all of which append a version and none of which deletes one:

    * a broker profile with nothing here gets registered (this is P13l, kept);
    * a profile here the broker no longer offers gets a *retired* version --
      the old versions stay exactly as they were, so a route decision from
      months ago still resolves its profile and the version chain still reads
      end to end;
    * a retired profile the broker offers again gets a live version back.

    Idempotent: run it twice and the second run does nothing, because all three
    moves are conditioned on a difference that no longer exists.
    """

    catalog_hash = broker_catalog_hash(config)
    desired = {
        profile["id"]: profile
        for profile in openclaw_broker_profiles_from_config(
            config, checked_at=checked_at, availability_ttl=availability_ttl,
            metadata_declarations=_metadata_declarations(router),
        )
    }
    held = _router_broker_profiles(router)
    added: list[str] = []
    revived: list[str] = []
    updated: list[str] = []
    refreshed: list[str] = []
    for profile_id in sorted(desired):
        current = held.get(profile_id)
        if current is None:
            router.register_profile(desired[profile_id])
            added.append(profile_id)
        elif current.get("status") == "retired":
            router.register_profile(
                _revived_version(current, desired[profile_id], checked_at=checked_at)
            )
            revived.append(profile_id)
        elif canonical_json(_profile_semantics(current)) != canonical_json(
            _profile_semantics(desired[profile_id])
        ):
            router.register_profile(
                _changed_version(current, desired[profile_id], checked_at=checked_at)
            )
            updated.append(profile_id)
        elif _availability_expired(current, checked_at):
            router.register_profile(
                _changed_version(current, desired[profile_id], checked_at=checked_at)
            )
            refreshed.append(profile_id)
    retired: list[str] = []
    for profile_id in sorted(held):
        current = held[profile_id]
        if profile_id in desired or current.get("status") == "retired":
            continue
        router.register_profile(
            _retired_version(current, checked_at=checked_at, catalog_hash=catalog_hash)
        )
        retired.append(profile_id)
    status = catalog_sync_status(router, config, checked_at=checked_at)
    return {
        **status,
        "added_profile_ids": added,
        "retired_profile_ids_this_run": retired,
        "revived_profile_ids": revived,
        "updated_profile_ids": updated,
        "refreshed_profile_ids": refreshed,
        "changed": bool(added or retired or revived or updated or refreshed),
    }


__all__ = [
    "OpenClawCatalogError",
    "UNPRICED_CEILING_INPUT_PER_MILLION_USD",
    "UNPRICED_CEILING_OUTPUT_PER_MILLION_USD",
    "broker_catalog_hash",
    "catalog_sync_status",
    "load_openclaw_config",
    "openclaw_broker_profiles_from_config",
    "reconcile_openclaw_model_catalog",
    "sync_openclaw_model_catalog",
]
