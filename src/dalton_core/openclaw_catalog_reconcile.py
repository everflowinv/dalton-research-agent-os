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
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .model_deployment import ADAPTER_REF, openclaw_broker_profiles
from .model_router import canonical_json


_BROKER_PLUGIN_ID = "dalton-openclaw-model-broker"
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
            output[model_ref] = {
                "provider": provider,
                "model": model_id,
                "model_ref": model_ref,
                "context_window": context_window,
                "max_output_tokens": max_tokens,
                "input_cost": _nonnegative_number(cost.get("input"), f"{model_ref}.cost.input"),
                "output_cost": _nonnegative_number(cost.get("output"), f"{model_ref}.cost.output"),
            }
    return output


def _broker_profiles(config: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    plugins = _mapping(config.get("plugins", {}), "plugins")
    entries = _mapping(plugins.get("entries", {}), "plugins.entries")
    plugin = _mapping(entries.get(_BROKER_PLUGIN_ID, {}), f"plugin {_BROKER_PLUGIN_ID}")
    plugin_config = _mapping(plugin.get("config", {}), f"plugin {_BROKER_PLUGIN_ID}.config")
    output: dict[str, dict[str, Any]] = {}
    route_owners: dict[str, str] = {}
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
        if model_ref in route_owners:
            raise OpenClawCatalogError(
                f"broker model {model_ref} is assigned to multiple profiles"
            )
        route_owners[model_ref] = profile_id
        output[profile_id] = {
            "id": profile_id,
            "provider": provider,
            "model": model,
            "model_ref": model_ref,
            "max_tokens": profile.get("maxTokens"),
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
    brokers = _broker_profiles(config)
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
) -> list[dict[str, Any]]:
    """Build the runnable verifier catalog from explicitly brokered models.

    Existing curated routes keep their immutable profile definitions.  A new
    broker profile gets a conservative verify-only definition from the public
    provider catalog and a content-bound version reference.  Orphan or changed
    routes are rejected instead of being guessed into a paid run.
    """

    if availability_ttl.total_seconds() <= 0:
        raise OpenClawCatalogError("availability_ttl must be positive")
    providers = _provider_models(config)
    brokers = _broker_profiles(config)
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
        static_route = static.get(profile_id)
        if static_route is not None:
            if static_route["model_ref"] != model_ref:
                raise OpenClawCatalogError(
                    f"broker profile {profile_id} changed route; add a new profile id"
                )
            profile = copy.deepcopy(static_route["profile"])
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
            profile["cost"] = {
                "currency": "USD",
                "input_per_million_usd": provider_model["input_cost"],
                "output_per_million_usd": provider_model["output_cost"],
            }
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
        }
        digest = hashlib.sha256(canonical_json(public_snapshot).encode("utf-8")).hexdigest()[:16]
        slug = profile_id.removeprefix("profile:")
        output.append({
            "schema_version": "0.1",
            "profile_version_ref": f"model-profile-version:dynamic-{slug}-{digest}:1",
            "id": profile_id,
            "version": 1,
            "created_at": created,
            "prior_version_ref": None,
            "provider": provider_model["provider"],
            "model": provider_model["model"],
            "family": f"unclassified:{provider_model['provider']}",
            "adapter_ref": ADAPTER_REF,
            "credential_slot_ref": f"credential-slot:openclaw:{provider_model['provider']}",
            "capabilities": ["verify"],
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
                "input_per_million_usd": provider_model["input_cost"],
                "output_per_million_usd": provider_model["output_cost"],
            },
            "limits": {
                "max_input_tokens": max_input,
                "max_output_tokens": max_output,
                "max_total_tokens": context_window,
                "max_cost_usd": 250.0,
            },
        })
    return output


__all__ = [
    "OpenClawCatalogError",
    "load_openclaw_config",
    "openclaw_broker_profiles_from_config",
    "reconcile_openclaw_model_catalog",
]
