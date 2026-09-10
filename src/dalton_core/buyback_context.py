"""W4: what a buyback is worth, computed against the things it has to be read against.

"The company bought 4,321,766 shares at $201.51" is not information.  The
owner's instruction is explicit about what turns it into information, and it is
four comparisons, none of which is in the filing:

* **against the price paid.**  A buyback at $201.51 when the shares are $189.10
  destroyed value on the day, whatever the press release said.  The price
  authority (yfinance) has the close; the filing has the average paid.
* **against the pace and what is left.**  $3.2bn of authorisation with $284m a
  month going out is eleven months of programme; the same authorisation with
  $30m a month going out is a programme in name only.
* **against the trend.**  Quarter on quarter, because a company that bought
  $1.1bn last quarter and $300m this one has changed its mind about something
  and has not said so.
* **against the size of the company and the cash it makes.**  As a percentage
  of market capitalisation and of trailing free cash flow, because a buyback
  funded out of debt while free cash flow falls is a different act from one
  funded out of a cash pile.

And one more the owner named separately: **does it actually shrink the
company?**  A buyback that exactly offsets share-based compensation returns
nothing to a holder; it moves cash from the shareholders to the employees and
leaves the share count where it was.  The statements have the share count, so
this is checkable rather than assertable.

Every one of these is arithmetic.  None of them is a judgement, and this module
makes none: it computes, names the accession or the price version behind each
input, and says ``unavailable`` with a reason wherever an input is missing.  A
missing input is the common case on a young Core and the reason has to be in
the prompt, because "buyback as a share of free cash flow: unavailable, this
company has no cash-flow statement in this Core" and a silently absent line are
very different things to a reader deciding what to do next.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping, Sequence
from decimal import Decimal, InvalidOperation
from types import MappingProxyType
from typing import Any

SCHEMA_VERSION = "0.1"

#: A unit word from a filing's own header, and what to multiply by. Frozen, and
#: a unit this does not know is a refusal to scale rather than a guess: getting
#: this wrong is a factor of a thousand.
SCALE_FACTORS: Mapping[str, Decimal] = MappingProxyType({
    "thousands": Decimal(10) ** 3,
    "millions": Decimal(10) ** 6,
    "billions": Decimal(10) ** 9,
})

#: The XBRL concepts that make up free cash flow, named rather than searched
#: for. Two of them, because free cash flow is not a filed line: it is
#: operating cash flow less capital expenditure, and a system that let a model
#: decide which lines those were would have a different definition every
#: quarter.
OPERATING_CASH_FLOW_CONCEPTS: tuple[str, ...] = (
    "NetCashProvidedByUsedInOperatingActivities",
    "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations",
)
CAPEX_CONCEPTS: tuple[str, ...] = (
    "PaymentsToAcquirePropertyPlantAndEquipment",
    "PaymentsToAcquireProductiveAssets",
)
#: The share count, in the order it should be preferred. Diluted first: a
#: buyback that offsets dilution has to be measured against the count that
#: dilution moves, and the basic count would hide exactly the effect the
#: comparison exists to find.
SHARE_COUNT_CONCEPTS: tuple[str, ...] = (
    "WeightedAverageNumberOfDilutedSharesOutstanding",
    "WeightedAverageNumberOfSharesOutstandingBasic",
    "CommonStockSharesOutstanding",
)

#: How many quarters back the free-cash-flow window reaches. Four, because a
#: buyback is compared with a year's cash generation and a single quarter's is
#: seasonal in most businesses.
TRAILING_QUARTERS = 4
MAX_TREND_QUARTERS = 6

#: The owner's reading of a buyback, written as things to weigh. Printed to the
#: brain beside the figures; none of them decides anything.
CONSIDERATIONS: tuple[str, ...] = (
    "A buyback is not good news by itself. Read it against the price paid: "
    "shares bought above what you think they are worth destroyed value, and "
    "the press release will not say so.",
    "Read the pace against what is left authorised. An authorisation is a "
    "permission with no obligation attached; companies announce them and do "
    "not use them, and a programme running at a tenth of its authorisation is "
    "a different fact from one running at full tilt.",
    "Read this quarter against the last few. A company that has quietly "
    "stopped buying has changed its mind about something and has not said so.",
    "Ask whether it shrinks the company. A buyback that offsets share-based "
    "compensation returns nothing to a holder; it moves cash from the "
    "shareholders to the employees and leaves the count where it was.",
    "Ask where the cash came from. Buying shares out of free cash flow and "
    "buying them out of new borrowing are different decisions with the same "
    "press release.",
    "US disclosure is late by construction. The Item 2 table you are reading "
    "covers a quarter that ended weeks ago, and an 8-K authorisation says "
    "nothing about what has actually been bought. Only Hong Kong discloses "
    "daily. Do not read this as news about today.",
)


class BuybackContextError(ValueError):
    """The event handed in is not a buyback disclosure this can read."""


def _decimal(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return Decimal(value)
    if not isinstance(value, str):
        return None
    text = value.strip().replace(",", "")
    if not text:
        return None
    try:
        parsed = Decimal(text)
    except InvalidOperation:
        return None
    return parsed if parsed.is_finite() else None


def _plain(value: Decimal | None, places: int) -> str | None:
    if value is None:
        return None
    return format(value.quantize(Decimal(1).scaleb(-places)), "f")


def scaled(value: Any, unit: Any) -> Decimal | None:
    """A filed figure in its own units, or ``None`` when the unit is unstated.

    Refusing to scale an unlabelled column is the whole point. A remaining
    authorisation of "3,244" is $3.2 billion if the header said millions and
    $3,244 if it did not; taking the first reading on the strength of it being
    the likely one would be a factor of a million invented by a parser.
    """

    number = _decimal(value)
    if number is None:
        return None
    if unit is None:
        return None
    factor = SCALE_FACTORS.get(str(unit))
    return None if factor is None else number * factor


# ---------------------------------------------------------------------------
# what the statements say
# ---------------------------------------------------------------------------


def _concept_series(
    connection: sqlite3.Connection, company_ref: str, concepts: Sequence[str]
) -> list[dict[str, Any]]:
    """Filed quarterly values for the first of these concepts that exists.

    The concepts are tried in order and the first one with rows wins, because
    they are alternatives for the same line rather than lines to be added
    together -- a filer reports operating cash flow under one tag or the other
    and adding both would double it.
    """

    try:
        for concept in concepts:
            rows = connection.execute(
                "SELECT l.period_end AS period_end, l.period_start AS period_start, "
                "l.value AS value, l.unit AS unit, l.label AS label, "
                "f.accession AS accession, f.filed AS filed, f.form AS form "
                "FROM coverage_mission_statement_lines l "
                "JOIN coverage_mission_statement_filings f USING(ingest_id) "
                "WHERE f.company_ref=? AND l.concept=? AND l.is_breakdown=0 "
                "AND l.dimension_axis IS NULL AND l.value IS NOT NULL "
                "ORDER BY l.period_end DESC, f.filed DESC",
                (company_ref, concept),
            ).fetchall()
            if rows:
                return [{"concept": concept, **dict(row)} for row in rows]
    except sqlite3.OperationalError as exc:
        if "no such table" not in str(exc):
            raise
        return []
    return []


def _newest_per_period(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """One value per period end: the most recently filed statement of it.

    A restated quarter appears twice, in the original filing and in the one
    that restated it, and the later filing is the company's current answer.
    """

    seen: dict[str, dict[str, Any]] = {}
    for row in rows:
        key = str(row["period_end"])
        current = seen.get(key)
        if current is None or str(row["filed"]) > str(current["filed"]):
            seen[key] = dict(row)
    return sorted(seen.values(), key=lambda row: str(row["period_end"]), reverse=True)


def statement_figures(
    connection: sqlite3.Connection,
    company_ref: str,
    *,
    quarters: int = TRAILING_QUARTERS,
) -> dict[str, Any]:
    """Trailing free cash flow and the share count, or why neither is here.

    Free cash flow is computed rather than looked up: operating cash flow less
    capital expenditure, over the same set of period ends, and the periods that
    have both are the ones that count. A quarter with one and not the other is
    dropped and said out loud rather than treated as though capex were zero.
    """

    operating = _newest_per_period(
        _concept_series(connection, company_ref, OPERATING_CASH_FLOW_CONCEPTS)
    )
    capex = _newest_per_period(
        _concept_series(connection, company_ref, CAPEX_CONCEPTS)
    )
    shares = _newest_per_period(
        _concept_series(connection, company_ref, SHARE_COUNT_CONCEPTS)
    )
    result: dict[str, Any] = {
        "free_cash_flow": None,
        "free_cash_flow_status": "unavailable",
        "free_cash_flow_reason": None,
        "free_cash_flow_periods": [],
        "share_counts": [
            {"period_end": row["period_end"], "value": row["value"],
             "concept": row["concept"], "accession": row["accession"]}
            for row in shares[:MAX_TREND_QUARTERS]
        ],
        "share_count_status": "read" if shares else "unavailable",
        "share_count_reason": (
            None if shares else
            "no share-count line is in this Core's statement ledger for this "
            f"company; looked for {list(SHARE_COUNT_CONCEPTS)}"
        ),
        "refs": [],
    }
    if not operating or not capex:
        missing = []
        if not operating:
            missing.append("operating cash flow")
        if not capex:
            missing.append("capital expenditure")
        result["free_cash_flow_reason"] = (
            f"this Core has no {' and no '.join(missing)} line for this company, "
            "so free cash flow cannot be computed; it is not zero and it is not "
            "assumed"
        )
        result["refs"] = [row["accession"] for row in shares[:MAX_TREND_QUARTERS]]
        return result
    capex_by_period = {str(row["period_end"]): row for row in capex}
    total = Decimal(0)
    used: list[dict[str, Any]] = []
    for row in operating:
        partner = capex_by_period.get(str(row["period_end"]))
        if partner is None:
            continue
        cash = _decimal(row["value"])
        spend = _decimal(partner["value"])
        if cash is None or spend is None:
            continue
        # Capex is filed as a positive outflow under a "Payments to..." tag, so
        # it is subtracted whichever sign the filer used; abs() rather than a
        # sign test because a filer who wrote it negative meant the same thing.
        period = cash - abs(spend)
        total += period
        used.append({
            "period_end": row["period_end"], "operating_cash_flow": row["value"],
            "capex": partner["value"], "free_cash_flow": _plain(period, 0),
            "accession": row["accession"], "capex_accession": partner["accession"],
        })
        if len(used) >= quarters:
            break
    if not used:
        result["free_cash_flow_reason"] = (
            "operating cash flow and capital expenditure are both held but for no "
            "period in common, so no quarter's free cash flow can be computed"
        )
        return result
    result["free_cash_flow"] = _plain(total, 0)
    result["free_cash_flow_status"] = "read"
    result["free_cash_flow_periods"] = used
    result["refs"] = list(dict.fromkeys(
        [row["accession"] for row in used]
        + [row["capex_accession"] for row in used]
        + [row["accession"] for row in shares[:MAX_TREND_QUARTERS]]
    ))
    return result


# ---------------------------------------------------------------------------
# the context
# ---------------------------------------------------------------------------


def _spend(row: Mapping[str, Any]) -> Decimal | None:
    shares = _decimal(row.get("shares_purchased"))
    price = _decimal(row.get("average_price_paid"))
    if shares is None or price is None:
        return None
    return shares * price


def _quarter_totals(
    events: Sequence[Mapping[str, Any]]
) -> list[dict[str, Any]]:
    """Each filing's issuer-purchases rows, added up, newest first.

    Grouped by accession rather than by a parsed quarter: the accession is the
    filing, the filing covers one quarter, and deriving a quarter label from
    three month names would be a second way of saying the same thing that could
    disagree with the first.
    """

    by_accession: dict[str, dict[str, Any]] = {}
    for event in events:
        payload = event.get("payload") or {}
        if payload.get("disclosure_kind") != "issuer_purchases_table":
            continue
        accession = payload.get("accession")
        if not accession:
            continue
        bucket = by_accession.setdefault(accession, {
            "accession": accession,
            "form": payload.get("form"),
            "filing_date": payload.get("filing_date"),
            "shares": Decimal(0),
            "spend": Decimal(0),
            "spend_is_a_floor": False,
            "months": [],
            "remaining_authorisation": payload.get("remaining_authorisation"),
            "remaining_authorisation_unit": payload.get("remaining_authorisation_unit"),
            "refs": [],
        })
        shares = _decimal(payload.get("shares_purchased")) or Decimal(0)
        spend = _spend(payload)
        bucket["shares"] += shares
        if spend is None:
            bucket["spend_is_a_floor"] = True
        else:
            bucket["spend"] += spend
        bucket["months"].append(payload.get("period_label"))
        if event.get("id"):
            bucket["refs"].append(event["id"])
        # The latest month of the filing carries the authorisation that was
        # left at the end of the quarter, which is the number that matters;
        # the label sorts within a quarter because the months are the same
        # three names in the same order in every filing.
        if payload.get("remaining_authorisation") is not None:
            bucket["remaining_authorisation"] = payload["remaining_authorisation"]
            bucket["remaining_authorisation_unit"] = payload.get(
                "remaining_authorisation_unit"
            )
    rows = sorted(
        by_accession.values(),
        key=lambda row: (str(row["filing_date"] or ""), row["accession"]),
        reverse=True,
    )
    return [
        {
            **row,
            "shares": _plain(row["shares"], 0),
            "spend": _plain(row["spend"], 2),
            "months": [month for month in row["months"] if month],
        }
        for row in rows[:MAX_TREND_QUARTERS]
    ]


def build_buyback_context(
    event: Mapping[str, Any],
    *,
    company_events: Sequence[Mapping[str, Any]] = (),
    price: Mapping[str, Any] | None = None,
    market_cap: Mapping[str, Any] | None = None,
    statements: Mapping[str, Any] | None = None,
    transcript_claims: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Everything derivable about one buyback disclosure, with its refs.

    Every input is handed in rather than fetched, so the whole computation is a
    pure function of one ledger slice and one price version and can be replayed
    from them. ``price`` is ``MarketPriceSeriesAuthority.latest_close`` and
    ``market_cap`` is its ``latest_observation("market_cap")``; both may be
    ``None``, and where they are the comparison says so.
    """

    if event.get("kind") != "buyback_disclosure":
        raise BuybackContextError(
            f"buyback context is for buyback_disclosure events, not {event.get('kind')!r}"
        )
    payload = dict(event.get("payload") or {})
    quarters = _quarter_totals([event, *company_events])
    refs = [ref for ref in (event.get("id"), payload.get("invocation_ref")) if ref]
    context: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "kind": "buyback_disclosure",
        "disclosure_kind": payload.get("disclosure_kind"),
        "subject": payload,
        "quarters": quarters,
        "considerations": list(CONSIDERATIONS),
    }
    context["price_comparison"] = _price_comparison(payload, price)
    context["pace"] = _pace(quarters)
    context["trend"] = _trend(quarters)
    context["size"] = _size(quarters, market_cap, statements)
    context["dilution_offset"] = _dilution_offset(quarters, statements)
    context["transcript_claims"] = (
        list((transcript_claims or {}).get("claims") or ())
    )
    context["transcript_claims_status"] = (
        (transcript_claims or {}).get("status") or "not_asked"
    )
    for block in ("price_comparison", "pace", "trend", "size", "dilution_offset"):
        refs.extend(context[block].get("refs") or ())
    refs.extend(row["ref"] for row in context["transcript_claims"] if row.get("ref"))
    for quarter in quarters:
        refs.extend(quarter["refs"])
    context["refs"] = list(dict.fromkeys(str(ref) for ref in refs if ref))
    return context


def _price_comparison(
    payload: Mapping[str, Any], price: Mapping[str, Any] | None
) -> dict[str, Any]:
    """What was paid against what the shares cost now."""

    paid = _decimal(payload.get("average_price_paid"))
    if paid is None or paid == 0:
        return {"status": "unavailable",
                "reason": "this disclosure names no average price paid "
                          "(an authorisation announces no purchases)",
                "refs": []}
    if price is None or _decimal(price.get("close")) is None:
        return {"status": "unavailable",
                "reason": "this Core holds no price series for this company, so "
                          "what was paid cannot be compared with what the shares "
                          "cost now",
                "average_price_paid": _plain(paid, 4), "refs": []}
    close = _decimal(price["close"])
    premium = (paid - close) / close * 100
    return {
        "status": "read",
        "average_price_paid": _plain(paid, 4),
        "close": price.get("close"),
        "close_as_of": price.get("as_of"),
        "paid_versus_close_percent": _plain(premium, 2),
        "direction": "above" if premium > 0 else ("below" if premium < 0 else "level"),
        "refs": [ref for ref in (price.get("version_ref"), price.get("invocation_ref"))
                 if ref],
    }


def _pace(quarters: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """How long the authorisation lasts at the rate the company is going."""

    if not quarters:
        return {"status": "unavailable", "reason": "no issuer-purchases rows", "refs": []}
    newest = quarters[0]
    spend = _decimal(newest["spend"])
    remaining = scaled(
        newest.get("remaining_authorisation"),
        newest.get("remaining_authorisation_unit"),
    )
    if spend is None or spend == 0:
        return {"status": "unavailable",
                "reason": "the latest quarter reports no spend to run a pace off",
                "refs": newest["refs"]}
    if remaining is None:
        return {
            "status": "unavailable",
            "reason": (
                "the filing states a remaining authorisation with no units, or "
                "none at all; an unscaled authorisation is not divided by "
                "anything here, because guessing the unit is a factor of a "
                "thousand"
                if newest.get("remaining_authorisation") is not None else
                "the filing states no remaining authorisation"
            ),
            "quarterly_spend": newest["spend"],
            "refs": newest["refs"],
        }
    quarters_left = remaining / spend
    return {
        "status": "read",
        "quarterly_spend": newest["spend"],
        "spend_is_a_floor": newest["spend_is_a_floor"],
        "remaining_authorisation": _plain(remaining, 0),
        "quarters_of_authorisation_left": _plain(quarters_left, 2),
        "months_of_authorisation_left": _plain(quarters_left * 3, 1),
        "accession": newest["accession"],
        "refs": newest["refs"],
    }


def _trend(quarters: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """This quarter's spend against the one before it, and the run behind them."""

    if len(quarters) < 2:
        return {
            "status": "unavailable",
            "reason": (
                "this Core holds one quarter of issuer-purchases rows, so there is "
                "nothing to compare it with; the trend becomes readable after the "
                "next 10-Q"
            ),
            "series": [
                {"accession": row["accession"], "filing_date": row["filing_date"],
                 "shares": row["shares"], "spend": row["spend"]}
                for row in quarters
            ],
            "refs": [ref for row in quarters for ref in row["refs"]],
        }
    newest, prior = quarters[0], quarters[1]
    current = _decimal(newest["spend"])
    previous = _decimal(prior["spend"])
    change = (
        _plain((current - previous) / previous * 100, 2)
        if current is not None and previous not in (None, Decimal(0)) else None
    )
    return {
        "status": "read",
        "quarter_on_quarter_percent": change,
        "series": [
            {"accession": row["accession"], "filing_date": row["filing_date"],
             "shares": row["shares"], "spend": row["spend"],
             "months": row["months"]}
            for row in quarters
        ],
        "refs": [ref for row in quarters for ref in row["refs"]],
    }


def _size(
    quarters: Sequence[Mapping[str, Any]],
    market_cap: Mapping[str, Any] | None,
    statements: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """The quarter's buyback as a share of the company and of the cash it makes."""

    if not quarters:
        return {"status": "unavailable", "reason": "no issuer-purchases rows", "refs": []}
    spend = _decimal(quarters[0]["spend"])
    result: dict[str, Any] = {
        "status": "read", "quarterly_spend": quarters[0]["spend"],
        "percent_of_market_cap": None, "percent_of_market_cap_reason": None,
        "percent_of_trailing_fcf": None, "percent_of_trailing_fcf_reason": None,
        "trailing_fcf": None,
        "refs": list(quarters[0]["refs"]),
    }
    cap = _decimal((market_cap or {}).get("value"))
    if spend is None or spend == 0:
        result["status"] = "unavailable"
        result["reason"] = "the latest quarter reports no spend"
        return result
    if cap is None or cap == 0:
        result["percent_of_market_cap_reason"] = (
            "this Core holds no market capitalisation observation for this company"
        )
    else:
        result["percent_of_market_cap"] = _plain(spend / cap * 100, 3)
        result["market_cap"] = (market_cap or {}).get("value")
        result["market_cap_as_of"] = (market_cap or {}).get("as_of")
        if (market_cap or {}).get("version_ref"):
            result["refs"].append(market_cap["version_ref"])
    figures = statements or {}
    fcf = _decimal(figures.get("free_cash_flow"))
    if fcf is None or fcf == 0:
        result["percent_of_trailing_fcf_reason"] = (
            figures.get("free_cash_flow_reason")
            or "no trailing free cash flow was computed for this company"
        )
    else:
        result["trailing_fcf"] = figures.get("free_cash_flow")
        result["percent_of_trailing_fcf"] = _plain(spend / fcf * 100, 2)
        result["trailing_fcf_quarters"] = len(figures.get("free_cash_flow_periods") or ())
        result["refs"].extend(figures.get("refs") or ())
    return result


def _dilution_offset(
    quarters: Sequence[Mapping[str, Any]],
    statements: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Did the share count actually fall, or did the buyback just hold it still?

    The comparison the owner asked for and the one a press release never makes.
    Two filed share counts a quarter apart, and the shares the company says it
    bought in between: if the count barely moved, the buyback went to offsetting
    issuance and a holder's claim on the business is where it was.
    """

    figures = statements or {}
    counts = list(figures.get("share_counts") or ())
    if len(counts) < 2:
        return {
            "status": "unavailable",
            "reason": (
                figures.get("share_count_reason")
                or "fewer than two filed share counts are held for this company, so "
                   "the change in count cannot be measured"
            ),
            "refs": [],
        }
    if not quarters:
        return {"status": "unavailable", "reason": "no issuer-purchases rows", "refs": []}
    newest, prior = counts[0], counts[1]
    current = _decimal(newest["value"])
    previous = _decimal(prior["value"])
    bought = _decimal(quarters[0]["shares"])
    if current is None or previous is None or bought is None or bought == 0:
        return {"status": "unavailable",
                "reason": "one of the two share counts or the shares bought is not "
                          "a number this reader can use",
                "refs": []}
    fell_by = previous - current
    return {
        "status": "read",
        "share_count_from": prior["value"],
        "share_count_from_period": prior["period_end"],
        "share_count_to": newest["value"],
        "share_count_to_period": newest["period_end"],
        "concept": newest["concept"],
        "share_count_fell_by": _plain(fell_by, 0),
        "shares_repurchased": quarters[0]["shares"],
        # The share of the buyback that actually reached the count. Under 100%
        # means issuance ate the rest; at or under zero means the count rose in
        # a quarter the company was buying, which is the case worth saying out
        # loud.
        "percent_of_buyback_that_reached_the_count": _plain(fell_by / bought * 100, 1),
        "offsets_issuance_only": fell_by <= 0,
        # Said rather than assumed: the two counts need not be exactly one
        # quarter apart, and a reader comparing them has to know that.
        "note": (
            "the two counts are the two most recent filed periods and the "
            "repurchase figure is the latest filing's; if the periods do not line "
            "up the ratio is indicative and not exact"
        ),
        "refs": [row["accession"] for row in (newest, prior) if row.get("accession")],
    }


def prompt_block(context: Mapping[str, Any]) -> list[str]:
    """The buyback context as the lines the judgement prompt prints."""

    payload = context["subject"]
    lines = [
        "",
        "## Derived buyback context (computed from filings and the price authority; "
        "no model read this)",
    ]
    if context["disclosure_kind"] == "authorisation":
        lines.append(
            f"authorisation: {payload.get('authorisation_change')} of "
            f"{payload.get('authorised_amount')} "
            f"{payload.get('authorised_amount_unit') or 'units unstated'} "
            f"{payload.get('currency') or ''} announced {payload.get('announced_date')} "
            f"in {payload.get('accession')}"
        )
        lines.append(
            "an authorisation is a permission, not a purchase: nothing has been "
            "bought because of it"
        )
    else:
        lines.append(
            f"month: {payload.get('period_label')} -- bought "
            f"{payload.get('shares_purchased')} shares at "
            f"{payload.get('average_price_paid')}, of which "
            f"{payload.get('shares_purchased_under_plans')} under the announced "
            f"programme; {payload.get('remaining_authorisation')} "
            f"{payload.get('remaining_authorisation_unit') or 'units unstated'} left "
            f"({payload.get('form')} {payload.get('accession')})"
        )
    price = context["price_comparison"]
    if price["status"] == "read":
        lines.append(
            f"price paid vs the last close: paid {price['average_price_paid']}, "
            f"close {price['close']} on {price['close_as_of']} -- "
            f"{price['paid_versus_close_percent']}% {price['direction']} the close"
        )
    else:
        lines.append(f"price paid vs the last close: unavailable -- {price['reason']}")
    pace = context["pace"]
    if pace["status"] == "read":
        lines.append(
            f"pace: {pace['quarterly_spend']} spent in the latest quarter against "
            f"{pace['remaining_authorisation']} authorised and unspent -- "
            f"{pace['months_of_authorisation_left']} months of programme left at "
            f"this rate ({pace['accession']})"
        )
    else:
        lines.append(f"pace: unavailable -- {pace['reason']}")
    trend = context["trend"]
    if trend["status"] == "read":
        lines.append(
            f"quarter on quarter: {trend['quarter_on_quarter_percent']}% change in "
            "spend"
        )
        for row in trend["series"]:
            lines.append(
                f"  {row['filing_date']} {row['accession']}: {row['shares']} shares, "
                f"{row['spend']} spent"
            )
    else:
        lines.append(f"quarter on quarter: unavailable -- {trend['reason']}")
    size = context["size"]
    if size["status"] == "read":
        lines.append(
            "size: "
            + (f"{size['percent_of_market_cap']}% of market capitalisation"
               if size["percent_of_market_cap"] else
               f"share of market cap unavailable ({size['percent_of_market_cap_reason']})")
            + "; "
            + (f"{size['percent_of_trailing_fcf']}% of trailing free cash flow "
               f"({size['trailing_fcf']} over "
               f"{size.get('trailing_fcf_quarters')} quarters)"
               if size["percent_of_trailing_fcf"] else
               f"share of free cash flow unavailable "
               f"({size['percent_of_trailing_fcf_reason']})")
        )
    else:
        lines.append(f"size: unavailable -- {size.get('reason')}")
    offset = context["dilution_offset"]
    if offset["status"] == "read":
        lines.append(
            f"does it shrink the company: share count went from "
            f"{offset['share_count_from']} ({offset['share_count_from_period']}) to "
            f"{offset['share_count_to']} ({offset['share_count_to_period']}), a fall "
            f"of {offset['share_count_fell_by']} against "
            f"{offset['shares_repurchased']} shares bought -- "
            f"{offset['percent_of_buyback_that_reached_the_count']}% of the buyback "
            f"reached the count"
        )
        if offset["offsets_issuance_only"]:
            lines.append(
                "  the count did not fall: on this evidence the buyback offset "
                "issuance rather than returning anything to a holder"
            )
    else:
        lines.append(f"does it shrink the company: unavailable -- {offset['reason']}")
    claims = context["transcript_claims"]
    lines.append(
        "what management has said about buybacks (transcript Claims, "
        f"status {context['transcript_claims_status']}):"
    )
    if claims:
        for row in claims:
            lines.append(f"  [{row.get('aspect')}] {row['ref']}: {row['statement']}")
    else:
        lines.append("  (none held; management may simply never have been asked)")
    lines.append("")
    lines.append("How to weigh this (considerations, not rules; you decide):")
    lines.extend(f"- {line}" for line in context["considerations"])
    return lines


__all__ = [
    "CAPEX_CONCEPTS",
    "CONSIDERATIONS",
    "MAX_TREND_QUARTERS",
    "OPERATING_CASH_FLOW_CONCEPTS",
    "SCALE_FACTORS",
    "SCHEMA_VERSION",
    "SHARE_COUNT_CONCEPTS",
    "TRAILING_QUARTERS",
    "BuybackContextError",
    "build_buyback_context",
    "prompt_block",
    "scaled",
    "statement_figures",
]
