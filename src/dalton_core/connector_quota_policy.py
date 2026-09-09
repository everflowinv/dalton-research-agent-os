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
        # S2: one Guidepoint expert-transcript search per unit.
        #
        # Deliberately the smallest ceiling of any search source, and not
        # because the proxy is slow. Guidepoint's licence permits research
        # reading and explicitly forbids bulk extraction; a lane that can run
        # hundreds of searches a day is one whose traffic pattern stops looking
        # like research. The mission plan is five issuers times two specs plus
        # four industry queries -- fourteen for a complete sweep -- and
        # the plan's cadence repeats a spec weekly, so steady state is a
        # handful a day. Twenty-five leaves room for one full re-sweep plus
        # retries in a single day and nothing that resembles a crawl. Raising
        # it is a governance decision, not a constant edit.
        ("guidepoint", "search_library"): MappingProxyType(
            {
                "quota_unit": "search",
                "daily_unit_limit": 25,
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
        # C1: one company's dated corporate events per unit.
        #
        # An earnings date is announced once and then does not move, so the
        # calendar lane asks once a day per covered company and the five
        # covered companies need five of these. Fifty leaves room for a
        # business day's worth of retries and for the coverage universe to
        # grow, without ever making this the reason Yahoo starts refusing.
        #
        # One physical call: ``Ticker.calendar`` is a single quoteSummary
        # request against the same two hosts the price operation uses.
        ("yfinance", "calendar"): MappingProxyType(
            {
                "quota_unit": "search",
                "daily_unit_limit": 50,
                "max_physical_calls_per_unit": 1,
            }
        ),
        # S1: the two local feeds. There is no upstream to be polite to and
        # nothing to pay -- these are file reads on this machine -- so the
        # ceilings are generous. They are declared anyway, because a lane
        # without a quota is a lane whose runaway loop nobody notices, and
        # because admission refuses a route with no governed policy rather
        # than inventing an unlimited one.
        ("sales-notes", "list_notes"): MappingProxyType(
            {
                "quota_unit": "search",
                # One enumeration per window per tick, and a tick walks a few
                # windows; a few hundred a day is a bug, not a workload.
                "daily_unit_limit": 500,
                "max_physical_calls_per_unit": 1,
            }
        ),
        ("sales-notes", "get_note"): MappingProxyType(
            {
                "quota_unit": "document",
                # About twelve notes arrive per run and twenty-four a day. A
                # thousand covers a full backfill of the whole archive in one
                # day and still bounds a loop.
                "daily_unit_limit": 1_000,
                "max_physical_calls_per_unit": 1,
            }
        ),
        ("company-wiki", "list_documents"): MappingProxyType(
            {
                "quota_unit": "search",
                "daily_unit_limit": 500,
                "max_physical_calls_per_unit": 1,
            }
        ),
        ("company-wiki", "get_document"): MappingProxyType(
            {
                "quota_unit": "document",
                # The whole corpus is about a thousand documents, so this is
                # "read everything once" and no more.
                "daily_unit_limit": 1_000,
                "max_physical_calls_per_unit": 1,
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
        # the 2026-08 incident was about. Three pages of one hundred cover the
        # 204-row A+H universe, which is why the per-unit ceiling is three and
        # not a round number: the adapter also caps the library's own
        # three-attempt retry loop to one attempt per page, so three pages is
        # three GETs and the ceiling is the truth rather than a hope. Four
        # units a day is deliberately below anything that could look like
        # probing.
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
