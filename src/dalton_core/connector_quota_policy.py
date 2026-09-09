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
        # P9d-4b: one cited page per unit; the mission plan cap and the
        # per-host profile chain bound this further.
        ("web-fetch", "fetch_get"): MappingProxyType(
            {
                "quota_unit": "document",
                # P10x: raised from 200. These are free public HTTPS reads and
                # the allowance is per host now, so this is a politeness bound
                # on one site rather than a shared budget sources compete for.
                "daily_unit_limit": 1_000,
                "max_physical_calls_per_unit": 1,
            }
        ),
        # P10p: one issuer's filing index per unit. A "search" unit rather than
        # a new word for it: one query in, a list of filings out, which is the
        # same shape the other search quotas already describe.
        #
        # Small on purpose -- an annual report changes once a year, so the
        # mission needs a handful of these a day, not a stream. data.sec.gov is
        # free but rate limited, and this ceiling is what stands between a retry
        # loop and being throttled off the source the whole SEC lane depends on.
        # S3: the crowd sources, all at fifty units a day.
        #
        # Fifty is not a measurement. None of these three publishes a rate
        # limit, and two of them are read through a host tool that would be
        # throttled or logged out long before any number here mattered. Fifty
        # is a bound on what a bug can cost: five companies read once a day is
        # five units, so this is ten times what the lane is for, and a runaway
        # retry loop stops at breakfast rather than at the point where an
        # account is flagged.
        #
        # It is deliberately the same number for all seven operations. A
        # different figure for each would imply a measurement behind each one,
        # and there is not.
        #
        # `max_physical_calls_per_unit` differs because paging does: one
        # logical read of a timeline or a review library is several HTTP calls,
        # and one post or one ranking is exactly one.
        ("xueqiu-posts", "search_posts"): MappingProxyType(
            {
                "quota_unit": "search",
                "daily_unit_limit": 50,
                "max_physical_calls_per_unit": 5,
            }
        ),
        ("xueqiu-posts", "get_post"): MappingProxyType(
            {
                "quota_unit": "document",
                "daily_unit_limit": 50,
                "max_physical_calls_per_unit": 1,
            }
        ),
        ("xueqiu-posts", "hot_rank"): MappingProxyType(
            {
                "quota_unit": "search",
                "daily_unit_limit": 50,
                "max_physical_calls_per_unit": 1,
            }
        ),
        ("x-xreach-crowd", "user_timeline"): MappingProxyType(
            {
                "quota_unit": "search",
                "daily_unit_limit": 50,
                "max_physical_calls_per_unit": 5,
            }
        ),
        ("x-xreach-crowd", "search"): MappingProxyType(
            {
                "quota_unit": "search",
                "daily_unit_limit": 50,
                "max_physical_calls_per_unit": 5,
            }
        ),
        ("x-xreach-crowd", "thread"): MappingProxyType(
            {
                "quota_unit": "document",
                "daily_unit_limit": 50,
                "max_physical_calls_per_unit": 5,
            }
        ),
        # Free, unauthenticated and paged thirty rows at a time, so one
        # employer's library is up to twenty page reads. The politeness bound
        # is the point: nothing here is worth being blocked for.
        ("employee-reviews", "blind_reviews"): MappingProxyType(
            {
                "quota_unit": "document",
                "daily_unit_limit": 50,
                "max_physical_calls_per_unit": 20,
            }
        ),
        ("sec", "list_filings"): MappingProxyType(
            {
                "quota_unit": "search",
                # P10x: raised from 50. Free, and one index read per issuer per
                # form is cheap; data.sec.gov's own rate limit is the real bound.
                "daily_unit_limit": 200,
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
