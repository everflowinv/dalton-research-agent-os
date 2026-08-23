"""Operator-owned OpenClaw endpoint catalog for Dalton Core.

This module records exact, already-probed provider/model routes in the Core
router.  It contains logical credential-slot references only; OpenClaw keeps
all provider credentials and performs the actual completion through the
external broker adapter.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .model_router import ModelRouter, ModelRouterConflict, RoutingPolicyNotFound, canonical_json
from .store import content_hash


ADAPTER_REF = "adapter:openclaw-model-broker:0.1"
POLICY_REF = "model-routing-policy-version:dalton-openclaw:1"
LEGACY_BROKER_POLICY_REF = "model-routing-policy-version:dalton-openclaw:2"
BROKER_POLICY_REF = "model-routing-policy-version:dalton-openclaw:3"
ASSESSMENT_PROFILE_ID = "profile:gpt-5-6-sol"
ASSESSMENT_POLICY_REF = "model-routing-policy-version:dalton-openclaw-assessment:1"
VERIFIER_PROFILE_ID = "profile:gemini-3-7-flash"
VERIFIER_POLICY_REF = "model-routing-policy-version:dalton-openclaw-verifier:1"


_LEGACY_ENDPOINT_NAMES = (
    "deepseek-v4-flash",
    "gpt-5-6-sol",
    "gpt-5-6-terra",
    "gpt-5-6-luna",
    "claude-fable-5",
    "claude-opus-5",
)


_ENDPOINTS: tuple[dict[str, Any], ...] = (
    {
        "name": "deepseek-v4-flash",
        "provider": "deepseek",
        "model": "deepseek-v4-flash",
        "family": "deepseek-v4",
        "credential_slot_ref": "credential-slot:openclaw:deepseek",
        "capabilities": ["research", "verify", "code", "summarize", "extract"],
        "max_context_tokens": 1_000_000,
        "max_output_tokens": 128_000,
        "max_input_tokens": 800_000,
        "input_cost": 0.22,
        "output_cost": 0.66,
    },
    {
        "name": "gpt-5-6-sol",
        "provider": "openai",
        "model": "gpt-5.6-sol",
        "family": "openai-gpt-5.6",
        "credential_slot_ref": "credential-slot:openclaw:openai",
        "capabilities": ["research", "research-hard", "verify", "adjudicate", "code"],
        "max_context_tokens": 258_400,
        "max_output_tokens": 128_000,
        "max_input_tokens": 200_000,
        "input_cost": 5.0,
        "output_cost": 30.0,
    },
    {
        "name": "gpt-5-6-terra",
        "provider": "openai",
        "model": "gpt-5.6-terra",
        "family": "openai-gpt-5.6",
        "credential_slot_ref": "credential-slot:openclaw:openai",
        "capabilities": ["research", "verify", "code", "summarize", "extract"],
        "max_context_tokens": 258_400,
        "max_output_tokens": 128_000,
        "max_input_tokens": 200_000,
        "input_cost": 2.0,
        "output_cost": 12.0,
    },
    {
        "name": "gpt-5-6-luna",
        "provider": "openai",
        "model": "gpt-5.6-luna",
        "family": "openai-gpt-5.6",
        "credential_slot_ref": "credential-slot:openclaw:openai",
        "capabilities": ["summarize", "extract", "format"],
        "max_context_tokens": 258_400,
        "max_output_tokens": 128_000,
        "max_input_tokens": 200_000,
        "input_cost": 0.2,
        "output_cost": 1.2,
    },
    {
        "name": "claude-fable-5",
        "provider": "claude-cli-gateway",
        "model": "claude-fable-5",
        "family": "anthropic-claude-5",
        "credential_slot_ref": "credential-slot:openclaw:claude-cli",
        "capabilities": ["research", "research-hard", "verify", "adjudicate", "code"],
        "max_context_tokens": 1_000_000,
        "max_output_tokens": 128_000,
        "max_input_tokens": 800_000,
        "input_cost": 10.0,
        "output_cost": 50.0,
    },
    {
        "name": "claude-opus-5",
        "provider": "claude-cli-gateway",
        "model": "claude-opus-5",
        "family": "anthropic-claude-5",
        "credential_slot_ref": "credential-slot:openclaw:claude-cli",
        "capabilities": ["research", "research-hard", "verify", "adjudicate", "code"],
        "max_context_tokens": 1_000_000,
        "max_output_tokens": 64_000,
        "max_input_tokens": 800_000,
        "input_cost": 5.0,
        "output_cost": 25.0,
    },
    {
        "name": "claude-sonnet-5",
        "provider": "claude-cli-gateway",
        "model": "claude-sonnet-5",
        "family": "anthropic-claude-5",
        "credential_slot_ref": "credential-slot:openclaw:claude-cli",
        "capabilities": ["research", "verify", "adjudicate", "code", "summarize"],
        "max_context_tokens": 1_000_000,
        "max_output_tokens": 64_000,
        "max_input_tokens": 800_000,
        "input_cost": 2.0,
        "output_cost": 10.0,
    },
    {
        "name": "gemini-3-7-flash",
        "provider": "google",
        "model": "gemini-3.7-flash",
        "family": "google-gemini-3",
        "credential_slot_ref": "credential-slot:openclaw:google",
        "capabilities": ["research", "verify", "code", "summarize", "extract"],
        "max_context_tokens": 1_048_576,
        "max_output_tokens": 65_536,
        "max_input_tokens": 983_040,
        "input_cost": 0.75,
        "output_cost": 3.75,
    },
    {
        "name": "gemini-flash-latest",
        "provider": "google",
        "model": "gemini-flash-latest",
        "family": "google-gemini-flash",
        "credential_slot_ref": "credential-slot:openclaw:google",
        "capabilities": ["research", "verify", "code", "summarize", "extract"],
        "max_context_tokens": 1_048_576,
        "max_output_tokens": 65_536,
        "max_input_tokens": 983_040,
        "input_cost": 1.5,
        "output_cost": 9.0,
    },
    {
        "name": "gemini-3-1-pro-preview",
        "provider": "google",
        "model": "gemini-3.1-pro-preview",
        "family": "google-gemini-3",
        "credential_slot_ref": "credential-slot:openclaw:google",
        "capabilities": ["research", "research-hard", "verify", "adjudicate", "code"],
        "max_context_tokens": 1_048_576,
        "max_output_tokens": 65_536,
        "max_input_tokens": 983_040,
        "input_cost": 2.0,
        "output_cost": 12.0,
    },
    {
        "name": "gemini-3-5-flash-lite",
        "provider": "google",
        "model": "gemini-3.5-flash-lite",
        "family": "google-gemini-3",
        "credential_slot_ref": "credential-slot:openclaw:google",
        "capabilities": ["research", "verify", "summarize", "extract", "format"],
        "max_context_tokens": 1_048_576,
        "max_output_tokens": 65_536,
        "max_input_tokens": 983_040,
        "input_cost": 0.3,
        "output_cost": 2.5,
    },
    {
        "name": "qwen3-8-max",
        "provider": "qwen",
        "model": "qwen3.8-max",
        "family": "qwen-3.8",
        "credential_slot_ref": "credential-slot:openclaw:qwen",
        "capabilities": ["research", "research-hard", "verify", "adjudicate", "code"],
        "max_context_tokens": 983_616,
        "max_output_tokens": 131_072,
        "max_input_tokens": 852_544,
        "input_cost": 1.65,
        "output_cost": 4.95,
    },
    {
        "name": "qwen-deepseek-v4-flash-0731",
        "provider": "qwen",
        "model": "deepseek-v4-flash-0731",
        "family": "deepseek-v4",
        "credential_slot_ref": "credential-slot:openclaw:qwen",
        "capabilities": ["research", "verify", "code", "summarize", "extract"],
        "max_context_tokens": 1_000_000,
        "max_output_tokens": 393_216,
        "max_input_tokens": 606_784,
        "input_cost": 0.14,
        "output_cost": 0.28,
    },
    {
        "name": "qwen-deepseek-v4-pro",
        "provider": "qwen",
        "model": "deepseek-v4-pro",
        "family": "deepseek-v4",
        "credential_slot_ref": "credential-slot:openclaw:qwen",
        "capabilities": ["research", "research-hard", "verify", "adjudicate", "code"],
        "max_context_tokens": 1_000_000,
        "max_output_tokens": 393_216,
        "max_input_tokens": 606_784,
        "input_cost": 0.435,
        "output_cost": 0.87,
    },
    {
        "name": "glm-5-2",
        "provider": "qwen",
        "model": "glm-5.2",
        "family": "zhipu-glm-5.2",
        "credential_slot_ref": "credential-slot:openclaw:qwen",
        "capabilities": ["research", "verify", "code", "summarize", "extract"],
        "max_context_tokens": 1_000_000,
        "max_output_tokens": 131_072,
        "max_input_tokens": 868_928,
        "input_cost": 1.1,
        "output_cost": 3.851,
    },
    {
        "name": "gpt-5-5",
        "provider": "openai",
        "model": "gpt-5.5",
        "family": "openai-gpt-5.5",
        "credential_slot_ref": "credential-slot:openclaw:openai",
        "capabilities": ["research", "research-hard", "verify", "adjudicate", "code"],
        "max_context_tokens": 1_000_000,
        "max_output_tokens": 128_000,
        "max_input_tokens": 872_000,
        "input_cost": 5.0,
        "output_cost": 30.0,
    },
    {
        "name": "deepseek-v4-pro",
        "provider": "deepseek",
        "model": "deepseek-v4-pro",
        "family": "deepseek-v4",
        "credential_slot_ref": "credential-slot:openclaw:deepseek",
        "capabilities": ["research", "research-hard", "verify", "adjudicate", "code"],
        "max_context_tokens": 1_000_000,
        "max_output_tokens": 384_000,
        "max_input_tokens": 616_000,
        "input_cost": 0.66,
        "output_cost": 1.98,
    },
    {
        "name": "grok-4-6",
        "provider": "xai",
        "model": "grok-4.6",
        "family": "xai-grok-4",
        "credential_slot_ref": "credential-slot:openclaw:xai",
        "capabilities": ["research", "research-hard", "verify", "adjudicate", "code"],
        "max_context_tokens": 500_000,
        "max_output_tokens": 64_000,
        "max_input_tokens": 436_000,
        "input_cost": 2.0,
        "output_cost": 6.0,
    },
    {
        "name": "grok-build-0-1",
        "provider": "xai",
        "model": "grok-build-0.1",
        "family": "xai-grok-build",
        "credential_slot_ref": "credential-slot:openclaw:xai",
        "capabilities": ["research", "verify", "code", "summarize", "extract"],
        "max_context_tokens": 256_000,
        "max_output_tokens": 64_000,
        "max_input_tokens": 192_000,
        "input_cost": 1.0,
        "output_cost": 2.0,
    },
    {
        "name": "grok-4-3",
        "provider": "xai",
        "model": "grok-4.3",
        "family": "xai-grok-4",
        "credential_slot_ref": "credential-slot:openclaw:xai",
        "capabilities": ["research", "verify", "code", "summarize", "extract"],
        "max_context_tokens": 1_000_000,
        "max_output_tokens": 64_000,
        "max_input_tokens": 936_000,
        "input_cost": 1.25,
        "output_cost": 2.5,
    },
    {
        "name": "grok-4-20-beta-reasoning",
        "provider": "xai",
        "model": "grok-4.20-beta-latest-reasoning",
        "family": "xai-grok-4",
        "credential_slot_ref": "credential-slot:openclaw:xai",
        "capabilities": ["research", "research-hard", "verify", "adjudicate", "code"],
        "max_context_tokens": 1_000_000,
        "max_output_tokens": 30_000,
        "max_input_tokens": 970_000,
        "input_cost": 1.25,
        "output_cost": 2.5,
    },
    {
        "name": "grok-4-20-beta-non-reasoning",
        "provider": "xai",
        "model": "grok-4.20-beta-latest-non-reasoning",
        "family": "xai-grok-4",
        "credential_slot_ref": "credential-slot:openclaw:xai",
        "capabilities": ["research", "verify", "summarize", "extract", "format"],
        "max_context_tokens": 1_000_000,
        "max_output_tokens": 30_000,
        "max_input_tokens": 970_000,
        "input_cost": 1.25,
        "output_cost": 2.5,
    },
    {
        "name": "openrouter-ox-alpha",
        "provider": "openrouter",
        "model": "stealth/ox-alpha",
        "family": "openrouter-ox-alpha",
        "credential_slot_ref": "credential-slot:openclaw:openrouter",
        "capabilities": ["research", "verify", "code", "summarize", "extract"],
        "max_context_tokens": 1_048_576,
        "max_output_tokens": 131_072,
        "max_input_tokens": 917_504,
        "input_cost": 0.0,
        "output_cost": 0.0,
    },
)


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("checked_at must include a timezone")
    return value.astimezone(timezone.utc)


def openclaw_profiles(
    *, checked_at: datetime, availability_ttl: timedelta = timedelta(hours=24)
) -> list[dict[str, Any]]:
    """Return immutable profile version 1 wires for configured OpenClaw routes."""

    checked = _utc(checked_at)
    if availability_ttl.total_seconds() <= 0:
        raise ValueError("availability_ttl must be positive")
    created = checked.isoformat(timespec="microseconds")
    valid_until = (checked + availability_ttl).isoformat(timespec="microseconds")
    profiles: list[dict[str, Any]] = []
    for endpoint in _ENDPOINTS:
        maximum_total = endpoint["max_input_tokens"] + endpoint["max_output_tokens"]
        profiles.append(
            {
                "schema_version": "0.1",
                "profile_version_ref": f"model-profile-version:{endpoint['name']}:1",
                "id": f"model-profile:{endpoint['name']}",
                "version": 1,
                "created_at": created,
                "prior_version_ref": None,
                "provider": endpoint["provider"],
                "model": endpoint["model"],
                "family": endpoint["family"],
                "adapter_ref": ADAPTER_REF,
                "credential_slot_ref": endpoint["credential_slot_ref"],
                "capabilities": list(endpoint["capabilities"]),
                "modalities": ["text"],
                "context": {
                    "max_context_tokens": endpoint["max_context_tokens"],
                    "max_output_tokens": endpoint["max_output_tokens"],
                },
                "availability": {
                    "state": "available",
                    "checked_at": created,
                    "valid_until": valid_until,
                },
                "cost": {
                    "currency": "USD",
                    "input_per_million_usd": endpoint["input_cost"],
                    "output_per_million_usd": endpoint["output_cost"],
                },
                "limits": {
                    "max_input_tokens": endpoint["max_input_tokens"],
                    "max_output_tokens": endpoint["max_output_tokens"],
                    "max_total_tokens": maximum_total,
                    "max_cost_usd": 250.0,
                },
            }
        )
    return profiles


def openclaw_policy(*, created_at: datetime) -> dict[str, Any]:
    """Return the exact allowlisted policy for the OpenClaw broker."""

    created = _utc(created_at).isoformat(timespec="microseconds")
    return {
        "schema_version": "0.1",
        "policy_version_ref": POLICY_REF,
        "id": "model-routing-policy:dalton-openclaw",
        "version": 1,
        "created_at": created,
        "prior_version_ref": None,
        "filters": {
            "allowed_profile_ids": [
                f"model-profile:{name}" for name in _LEGACY_ENDPOINT_NAMES
            ],
            "allowed_providers": [],
            "allowed_families": [],
            "allowed_adapter_refs": [ADAPTER_REF],
            "required_modalities": ["text"],
            "family_independence_capabilities": ["verify", "adjudicate"],
        },
        "ordered_preferences": [
            {"field": "estimated_cost_usd", "direction": "asc"},
            {"field": "profile_version_ref", "direction": "asc"},
        ],
    }


def install_openclaw_catalog(
    router_path: str | Path,
    *,
    checked_at: datetime,
    availability_ttl: timedelta = timedelta(hours=24),
) -> dict[str, Any]:
    """Install the endpoint profiles and one deterministic policy."""

    path = Path(router_path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with ModelRouter(path) as router:
        policy = router.register_policy(openclaw_policy(created_at=checked_at))
        profiles = [
            router.register_profile(profile)
            for profile in openclaw_profiles(
                checked_at=checked_at, availability_ttl=availability_ttl
            )
        ]
    return {
        "policy": policy,
        "profiles": profiles,
        "router_path": str(path),
    }


def openclaw_broker_profiles(
    *, checked_at: datetime, availability_ttl: timedelta = timedelta(days=7)
) -> list[dict[str, Any]]:
    """Return broker-protocol-compatible profiles without rewriting v1 history."""

    profiles = openclaw_profiles(
        checked_at=checked_at, availability_ttl=availability_ttl
    )
    for profile, endpoint in zip(profiles, _ENDPOINTS, strict=True):
        profile["profile_version_ref"] = (
            f"model-profile-version:broker-{endpoint['name']}:1"
        )
        profile["id"] = f"profile:{endpoint['name']}"
    return profiles


def openclaw_broker_policy(*, created_at: datetime) -> dict[str, Any]:
    """Version 3 routes to every configured profile accepted by the UDS broker."""

    policy = openclaw_policy(created_at=created_at)
    policy["policy_version_ref"] = BROKER_POLICY_REF
    policy["version"] = 3
    policy["prior_version_ref"] = LEGACY_BROKER_POLICY_REF
    policy["filters"]["allowed_profile_ids"] = [
        f"profile:{endpoint['name']}" for endpoint in _ENDPOINTS
    ]
    return policy


def _legacy_openclaw_broker_policy(*, created_at: datetime) -> dict[str, Any]:
    """Recreate immutable policy v2 so fresh catalogs can advance to v3."""

    policy = openclaw_policy(created_at=created_at)
    policy["policy_version_ref"] = LEGACY_BROKER_POLICY_REF
    policy["version"] = 2
    policy["prior_version_ref"] = POLICY_REF
    policy["filters"]["allowed_profile_ids"] = [
        f"profile:{name}" for name in _LEGACY_ENDPOINT_NAMES
    ]
    return policy


def openclaw_verifier_policy(*, created_at: datetime) -> dict[str, Any]:
    """Phase-pin independent verification to one exact broker profile.

    The verifier selection must be provable, not an emergent result of a
    cost-sorted shared policy.  This immutable policy chain allows exactly the
    Owner-selected ``profile:gemini-3-7-flash``; any producer from the same
    model family stays fail-closed through the router's family-independence
    filter.
    """

    created = _utc(created_at).isoformat(timespec="microseconds")
    return {
        "schema_version": "0.1",
        "policy_version_ref": VERIFIER_POLICY_REF,
        "id": "model-routing-policy:dalton-openclaw-verifier",
        "version": 1,
        "created_at": created,
        "prior_version_ref": None,
        "filters": {
            "allowed_profile_ids": [VERIFIER_PROFILE_ID],
            "allowed_providers": [],
            "allowed_families": [],
            "allowed_adapter_refs": [ADAPTER_REF],
            "required_modalities": ["text"],
            "family_independence_capabilities": ["verify", "adjudicate"],
        },
        "ordered_preferences": [
            {"field": "estimated_cost_usd", "direction": "asc"},
            {"field": "profile_version_ref", "direction": "asc"},
        ],
    }


def openclaw_assessment_policy(*, created_at: datetime) -> dict[str, Any]:
    """Phase-pin thesis-impact assessment to GPT-5.6 Sol."""

    created = _utc(created_at).isoformat(timespec="microseconds")
    return {
        "schema_version": "0.1",
        "policy_version_ref": ASSESSMENT_POLICY_REF,
        "id": "model-routing-policy:dalton-openclaw-assessment",
        "version": 1,
        "created_at": created,
        "prior_version_ref": None,
        "filters": {
            "allowed_profile_ids": [ASSESSMENT_PROFILE_ID],
            "allowed_providers": [],
            "allowed_families": [],
            "allowed_adapter_refs": [ADAPTER_REF],
            "required_modalities": ["text"],
            "family_independence_capabilities": ["verify", "adjudicate"],
        },
        "ordered_preferences": [
            {"field": "estimated_cost_usd", "direction": "asc"},
            {"field": "profile_version_ref", "direction": "asc"},
        ],
    }


def upgrade_openclaw_broker_catalog(
    router_path: str | Path,
    *,
    checked_at: datetime,
    availability_ttl: timedelta = timedelta(days=7),
    openclaw_config_path: str | Path | None = None,
) -> dict[str, Any]:
    """Append broker profiles plus shared and phase-pinned policies."""

    with ModelRouter(Path(router_path)) as router:
        legacy_policy = _register_policy_once(
            router,
            _legacy_openclaw_broker_policy(created_at=checked_at)
        )
        policy = _register_policy_once(
            router, openclaw_broker_policy(created_at=checked_at)
        )
        assessment_policy = _register_policy_once(
            router,
            openclaw_assessment_policy(created_at=checked_at)
        )
        verifier_policy = _register_policy_once(
            router,
            openclaw_verifier_policy(created_at=checked_at)
        )
        desired_profiles = openclaw_broker_profiles(
            checked_at=checked_at, availability_ttl=availability_ttl
        )
        if openclaw_config_path is not None:
            desired_profiles = [
                profile
                for profile in desired_profiles
                if profile["id"] not in {ASSESSMENT_PROFILE_ID, VERIFIER_PROFILE_ID}
            ]
        profiles = [
            _register_live_profile(router, profile, checked_at=checked_at)
            for profile in desired_profiles
        ]
        if openclaw_config_path is not None:
            from .openclaw_catalog_reconcile import (
                load_openclaw_config,
                openclaw_broker_profiles_from_config,
            )

            live_phase_profiles = openclaw_broker_profiles_from_config(
                load_openclaw_config(openclaw_config_path),
                checked_at=checked_at,
                availability_ttl=availability_ttl,
                profile_ids=(ASSESSMENT_PROFILE_ID, VERIFIER_PROFILE_ID),
            )
            profiles.extend(
                _register_live_profile(router, profile, checked_at=checked_at)
                for profile in live_phase_profiles
            )
    return {
        "legacy_policy": legacy_policy,
        "policy": policy,
        "assessment_policy": assessment_policy,
        "verifier_policy": verifier_policy,
        "profiles": profiles,
        "router_path": str(router_path),
    }


def _register_policy_once(
    router: ModelRouter, desired: dict[str, Any]
) -> dict[str, Any]:
    """Reuse an immutable policy ref only when its semantics are unchanged."""

    try:
        existing = router.get_policy(desired["policy_version_ref"])
    except RoutingPolicyNotFound:
        return router.register_policy(desired)
    ignored = {"created_at", "content_hash"}
    existing_semantics = {
        key: value for key, value in existing.items() if key not in ignored
    }
    desired_semantics = {
        key: value for key, value in desired.items() if key not in ignored
    }
    if canonical_json(existing_semantics) != canonical_json(desired_semantics):
        raise ModelRouterConflict(
            "existing immutable routing policy differs from deployment contract"
        )
    return {"status": "duplicate", "policy": existing}


def _profile_semantics(profile: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in profile.items()
        if key
        not in {
            "schema_version",
            "profile_version_ref",
            "version",
            "created_at",
            "prior_version_ref",
            "availability",
            "content_hash",
        }
    }


def _register_live_profile(
    router: ModelRouter,
    desired: dict[str, Any],
    *,
    checked_at: datetime,
) -> dict[str, Any]:
    """Append only when public route economics/capacity changed or expired."""

    row = router.connection.execute(
        "SELECT profile_json FROM model_endpoint_profile_versions "
        "WHERE profile_id=? ORDER BY version DESC LIMIT 1",
        (desired["id"],),
    ).fetchone()
    if row is None:
        return router.register_profile(desired)
    import json

    latest = json.loads(row["profile_json"])
    valid_until = datetime.fromisoformat(latest["availability"]["valid_until"])
    if (
        canonical_json(_profile_semantics(latest))
        == canonical_json(_profile_semantics(desired))
        and valid_until > _utc(checked_at)
    ):
        return {"status": "duplicate", "profile": latest}
    version = int(latest["version"]) + 1
    semantics_hash = content_hash(_profile_semantics(desired))[:16]
    wire = dict(desired)
    wire.update({
        "profile_version_ref": (
            f"model-profile-version:broker-{desired['id'].removeprefix('profile:')}"
            f"-{semantics_hash}:{version}"
        ),
        "version": version,
        "prior_version_ref": latest["profile_version_ref"],
    })
    return router.register_profile(wire)


__all__ = [
    "ADAPTER_REF",
    "POLICY_REF",
    "LEGACY_BROKER_POLICY_REF",
    "BROKER_POLICY_REF",
    "ASSESSMENT_PROFILE_ID",
    "ASSESSMENT_POLICY_REF",
    "VERIFIER_PROFILE_ID",
    "VERIFIER_POLICY_REF",
    "install_openclaw_catalog",
    "openclaw_policy",
    "openclaw_profiles",
    "openclaw_broker_profiles",
    "openclaw_broker_policy",
    "openclaw_assessment_policy",
    "openclaw_verifier_policy",
    "upgrade_openclaw_broker_catalog",
]
