"""Owner-approved connector call ceilings.

These specs are governance inputs, not counters.  ``ConnectorStore`` remains
the authority that reserves, measures, settles, and blocks calls against the
exact calendar window.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Any


DAILY_RESET_TIMEZONE = "Asia/Shanghai"
DAILY_WINDOW_SECONDS = 86_400

_DAILY_CALL_LIMITS = MappingProxyType(
    {
        ("alphaengine", "search_library"): 50,
        ("alphaengine", "get_document"): 80,
        ("gemini-web-search", "search_web"): 1_000,
    }
)


def governed_daily_call_quota(connector_slug: str, operation: str) -> dict[str, Any]:
    """Return a copy of one exact owner-approved daily call policy input."""

    key = (connector_slug, operation)
    try:
        call_limit = _DAILY_CALL_LIMITS[key]
    except KeyError as exc:
        raise ValueError(
            f"no governed daily call quota for {connector_slug}/{operation}"
        ) from exc
    return {
        "connector_slug": connector_slug,
        "operation": operation,
        "call_limit": call_limit,
        "window_seconds": DAILY_WINDOW_SECONDS,
        "reset_timezone": DAILY_RESET_TIMEZONE,
    }


def governed_daily_call_quotas() -> list[dict[str, Any]]:
    """Return the complete deterministic quota inventory."""

    return [
        governed_daily_call_quota(connector_slug, operation)
        for connector_slug, operation in sorted(_DAILY_CALL_LIMITS)
    ]


def apply_call_quota_to_limits(
    quota: Mapping[str, Any],
    *,
    max_response_bytes: int,
    max_records: int,
    max_cost_micros_per_call: int = 0,
) -> dict[str, int]:
    """Expand a call ceiling into conservative ConnectorStore metrics."""

    call_limit = int(quota["call_limit"])
    values = {
        "max_response_bytes": max_response_bytes,
        "max_records": max_records,
        "max_cost_micros_per_call": max_cost_micros_per_call,
    }
    if call_limit < 1 or any(
        isinstance(value, bool) or not isinstance(value, int) or value < 0
        for value in values.values()
    ):
        raise ValueError("quota metric bounds must be non-negative integers")
    return {
        "calls": call_limit,
        "bytes": call_limit * max_response_bytes,
        "records": call_limit * max_records,
        "cost_micros": call_limit * max_cost_micros_per_call,
    }


__all__ = [
    "DAILY_RESET_TIMEZONE",
    "DAILY_WINDOW_SECONDS",
    "apply_call_quota_to_limits",
    "governed_daily_call_quota",
    "governed_daily_call_quotas",
]
