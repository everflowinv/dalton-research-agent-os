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
        ("sec", "list_filings"): MappingProxyType(
            {
                "quota_unit": "search",
                # P10x: raised from 50. Free, and one index read per issuer per
                # form is cheap; data.sec.gov's own rate limit is the real bound.
                "daily_unit_limit": 200,
                "max_physical_calls_per_unit": 1,
            }
        ),
        # P11a: one company's price window per unit.
        #
        # Deliberately modest. Yahoo is an unofficial free source that has not
        # agreed to serve us: there is no published rate limit to stay under
        # and no support channel when a request starts being refused, so the
        # ceiling is politeness rather than arithmetic. Five covered companies
        # ticking once a day need five of these; two hundred leaves room for
        # backfills and retries without ever looking like a scraper.
        ("yfinance", "daily_prices"): MappingProxyType(
            {
                "quota_unit": "search",
                "daily_unit_limit": 200,
                # One ``download`` plus one metadata read for the share count
                # and market capitalisation, which Yahoo serves separately.
                "max_physical_calls_per_unit": 2,
            }
        ),
        # Estimates move slowly -- an analyst revises a target a handful of
        # times a quarter -- so this is smaller again. Reading it more often
        # would spend the source's goodwill on numbers that did not change.
        ("yfinance", "analyst_estimates"): MappingProxyType(
            {
                "quota_unit": "search",
                "daily_unit_limit": 50,
                "max_physical_calls_per_unit": 4,
            }
        ),
        # S4: China / Hong Kong fundamentals. Conservative throughout, and for
        # a reason with a date on it: on 2026-08-21 the OpenClaw skill pressed
        # 东方财富's price-history cluster a dozen times in a row and the
        # neighbouring endpoints -- which had been healthy all along -- were
        # cut off too, for minutes, with no error that said why. The skill's
        # standing rule is 「不要批量探测东财」. These ceilings are that rule
        # expressed as arithmetic.
        #
        # One company's statement history per unit. Behind the single library
        # call are one report-date listing plus one fetch per five periods, so
        # a decade of quarters is about nine physical calls; the Hong Kong
        # route is a summary call plus one table call. A statement set changes
        # four times a year, so twenty companies a day is generous.
        ("cn-hk-findata", "financial_statements"): MappingProxyType(
            {
                "quota_unit": "document",
                "daily_unit_limit": 20,
                "max_physical_calls_per_unit": 12,
            }
        ),
        # One company's holder picture per unit: the top-ten table for one
        # report date, plus the holder-count history, which the vendor pages
        # 500 rows at a time and which is short for any one issuer.
        ("cn-hk-findata", "shareholders"): MappingProxyType(
            {
                "quota_unit": "document",
                "daily_unit_limit": 20,
                "max_physical_calls_per_unit": 6,
            }
        ),
        # The most expensive of the six and the smallest allowance because of
        # it: the vendor publishes one market-wide buyback table and offers no
        # per-issuer route, so answering "did this company buy back stock"
        # means reading every page of every company's answer and throwing away
        # all but one. Four a day, and a lane that wants five companies should
        # read the table once and filter it five times rather than ask again.
        ("cn-hk-findata", "buybacks"): MappingProxyType(
            {
                "quota_unit": "document",
                "daily_unit_limit": 4,
                "max_physical_calls_per_unit": 40,
            }
        ),
        # One exchange-day per unit, straight from the exchange rather than a
        # vendor. Two exchanges times one trading day, with room to backfill a
        # short window, is what 40 buys.
        ("cn-hk-findata", "margin_balance"): MappingProxyType(
            {
                "quota_unit": "search",
                "daily_unit_limit": 40,
                "max_physical_calls_per_unit": 1,
            }
        ),
        # A daily snapshot. Reading it more than a handful of times a day
        # spends the source's patience on a number that moves once.
        ("cn-hk-findata", "northbound_flow"): MappingProxyType(
            {
                "quota_unit": "search",
                "daily_unit_limit": 8,
                "max_physical_calls_per_unit": 1,
            }
        ),
        # The one operation that must touch 东财's quote cluster -- the host
        # the 2026-08 incident was about. Two pages cover the whole A+H
        # universe, and four units a day is deliberately below anything that
        # could look like probing.
        ("cn-hk-findata", "ah_premium"): MappingProxyType(
            {
                "quota_unit": "search",
                "daily_unit_limit": 4,
                "max_physical_calls_per_unit": 3,
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
