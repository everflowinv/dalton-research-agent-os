"""Closed transport limits shared by model-producing control planes."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


DEFAULT_BROKER_MAX_FRAME_BYTES = 1_048_576
LEGACY_BROKER_MAX_FRAME_BYTES = 262_144
MAX_BROKER_FRAME_BYTES = 1_048_576
MIN_BROKER_FRAME_BYTES = 1_024
BROKER_FRAME_POLICY_VERSION = "broker-frame-policy-0.1"


class ModelTransportConfigError(ValueError):
    pass


def resolve_broker_max_frame_bytes(value: Mapping[str, Any]) -> int:
    """Return the encoded request/response frame byte bound."""

    configured = value.get(
        "broker_max_frame_bytes", DEFAULT_BROKER_MAX_FRAME_BYTES
    )
    if (
        isinstance(configured, bool)
        or not isinstance(configured, int)
        or not MIN_BROKER_FRAME_BYTES <= configured <= MAX_BROKER_FRAME_BYTES
    ):
        raise ModelTransportConfigError(
            "broker_max_frame_bytes must be an integer from 1024 through 1048576"
        )
    return configured


def broker_frame_execution_binding(value: Mapping[str, Any]) -> dict[str, Any]:
    """Freeze the effective byte-frame policy into model execution identity."""

    return {
        "schema_version": BROKER_FRAME_POLICY_VERSION,
        "broker_max_frame_bytes": resolve_broker_max_frame_bytes(value),
    }


__all__ = [
    "BROKER_FRAME_POLICY_VERSION",
    "DEFAULT_BROKER_MAX_FRAME_BYTES",
    "LEGACY_BROKER_MAX_FRAME_BYTES",
    "MAX_BROKER_FRAME_BYTES",
    "MIN_BROKER_FRAME_BYTES",
    "ModelTransportConfigError",
    "broker_frame_execution_binding",
    "resolve_broker_max_frame_bytes",
]
