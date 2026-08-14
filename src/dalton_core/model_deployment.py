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

from .model_router import ModelRouter


ADAPTER_REF = "adapter:openclaw-model-broker:0.1"
POLICY_REF = "model-routing-policy-version:dalton-openclaw:1"


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
)


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("checked_at must include a timezone")
    return value.astimezone(timezone.utc)


def openclaw_profiles(
    *, checked_at: datetime, availability_ttl: timedelta = timedelta(hours=24)
) -> list[dict[str, Any]]:
    """Return immutable profile version 1 wires for the six probed routes."""

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
                f"model-profile:{endpoint['name']}" for endpoint in _ENDPOINTS
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
    """Install the six endpoint profiles and one deterministic policy."""

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


__all__ = [
    "ADAPTER_REF",
    "POLICY_REF",
    "install_openclaw_catalog",
    "openclaw_policy",
    "openclaw_profiles",
]
