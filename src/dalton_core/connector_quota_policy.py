"""Owner-approved connector daily quota ceilings.

These specs are governance inputs, not counters.  ``ConnectorStore`` remains
the authority that reserves, measures, settles, and blocks quota units against
the exact calendar window.  AlphaEngine document acquisition is governed per
logical document even though every page remains a separately metered physical
call.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Any


DAILY_RESET_TIMEZONE = "Asia/Shanghai"
DAILY_WINDOW_SECONDS = 86_400

_DAILY_QUOTAS = MappingProxyType(
    {
        ("alphaengine", "search_library"): MappingProxyType(
            {
                "quota_unit": "search",
                "daily_unit_limit": 50,
                "max_physical_calls_per_unit": 1,
            }
        ),
        ("alphaengine", "get_document"): MappingProxyType(
            {
                "quota_unit": "document",
                "daily_unit_limit": 80,
                "max_physical_calls_per_unit": 20,
            }
        ),
        ("gemini-web-search", "search_web"): MappingProxyType(
            {
                "quota_unit": "search",
                "daily_unit_limit": 1_000,
                "max_physical_calls_per_unit": 1,
            }
        ),
    }
)


def governed_daily_quota(connector_slug: str, operation: str) -> dict[str, Any]:
    """Return a copy of one exact owner-approved daily quota policy input."""

    key = (connector_slug, operation)
    try:
        quota = _DAILY_QUOTAS[key]
    except KeyError as exc:
        raise ValueError(
            f"no governed daily quota for {connector_slug}/{operation}"
        ) from exc
    return {
        "connector_slug": connector_slug,
        "operation": operation,
        **dict(quota),
        "window_seconds": DAILY_WINDOW_SECONDS,
        "reset_timezone": DAILY_RESET_TIMEZONE,
    }


def governed_daily_quotas() -> list[dict[str, Any]]:
    """Return the complete deterministic quota inventory."""

    return [
        governed_daily_quota(connector_slug, operation)
        for connector_slug, operation in sorted(_DAILY_QUOTAS)
    ]


def apply_governed_quota_to_limits(
    quota: Mapping[str, Any],
    *,
    max_response_bytes: int,
    max_records: int,
    max_cost_micros_per_call: int = 0,
) -> dict[str, int]:
    """Expand a governed unit ceiling into ConnectorStore metric limits.

    The ``records`` meter carries logical document units for AlphaEngine
    ``get_document``.  Its ``calls`` and ``bytes`` limits are separate internal
    safety ceilings for at most 20 physical pages per document; they are not a
    statement that AlphaEngine charges once per page.
    """

    daily_unit_limit = int(quota["daily_unit_limit"])
    calls_per_unit = int(quota["max_physical_calls_per_unit"])
    quota_unit = quota["quota_unit"]
    values = {
        "daily_unit_limit": daily_unit_limit,
        "max_physical_calls_per_unit": calls_per_unit,
        "max_response_bytes": max_response_bytes,
        "max_records": max_records,
        "max_cost_micros_per_call": max_cost_micros_per_call,
    }
    if quota_unit not in {"search", "document"} or any(
        isinstance(value, bool) or not isinstance(value, int) or value < 0
        for value in values.values()
    ) or daily_unit_limit < 1 or calls_per_unit < 1:
        raise ValueError("quota metric bounds must be non-negative integers")
    physical_call_limit = daily_unit_limit * calls_per_unit
    return {
        "calls": physical_call_limit,
        "bytes": physical_call_limit * max_response_bytes,
        "records": (
            daily_unit_limit
            if quota_unit == "document"
            else physical_call_limit * max_records
        ),
        "cost_micros": physical_call_limit * max_cost_micros_per_call,
    }


__all__ = [
    "DAILY_RESET_TIMEZONE",
    "DAILY_WINDOW_SECONDS",
    "apply_governed_quota_to_limits",
    "governed_daily_quota",
    "governed_daily_quotas",
]
