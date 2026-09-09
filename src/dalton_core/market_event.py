"""P11d: the day a price did something the rest of the universe did not.

A three percent move is not news.  Five IT services names down three percent
on a Fed morning is one fact about rates and zero facts about any of them, and
an event ledger that recorded it five times would spend five model calls
saying "sector-wide, no company news" -- which is the correct answer, arrived
at expensively, five times.

So a move is read against two comparators and neither is optional in the way
it looks:

- **the equal-weight universe basket**, computed from the same day's bars of
  the other covered companies.  Equal-weight rather than cap-weight because
  the question is "did this one behave differently from its peers", and a
  cap-weighted basket of five names is mostly IBM.
- **a benchmark**, when a price series for one exists.  It usually will not,
  because nothing fetches SPY today.  Its absence is *recorded on the event*
  rather than treated as a zero return: an excess-versus-market of "+4.2%"
  computed against a market return nobody measured is a fabricated number
  wearing a real one's clothes.

Two hard exclusions, both learned from P11a:

**A provisional bar never triggers.**  A bar read before its own trading day
settled holds the last trade of an afternoon in the shape of a close.  Firing
on it would spend a model call on a move that had not happened yet, and worse,
the event's payload hash would be the afternoon's number -- so when the real
close arrived it would be a *second* event about the same day.

**A day with no previous bar never triggers.**  The first bar of a series has
no return; a series whose history was backfilled in one run has one date after
another with nothing between them, and dividing by a price from three years
ago produces a four-hundred-percent "move".
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from decimal import Decimal, InvalidOperation, localcontext
from typing import Any

from .market_price import bar_is_provisional
from .research_event import day_start

# The arithmetic is decimal and exact for the same reason the prices are
# stored as text: a threshold comparison that flips on the last bit of a float
# is a threshold nobody can reason about.
_PRECISION = 28
_QUANTUM = Decimal("0.0001")


class MarketEventError(ValueError):
    """The detector was given something it cannot read."""


def _decimal(value: Any, name: str) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError) as exc:
        raise MarketEventError(f"{name} is not a decimal: {value!r}") from exc
    if not parsed.is_finite():
        raise MarketEventError(f"{name} must be finite")
    return parsed


def _percent(current: Decimal, previous: Decimal) -> Decimal | None:
    if previous <= 0:
        return None
    with localcontext() as ctx:
        ctx.prec = _PRECISION
        return ((current / previous) - 1) * 100


def daily_return(series: Mapping[str, Any] | None, as_of: str) -> dict[str, Any] | None:
    """One trading day's return for one series, or nothing and why.

    Returns ``None`` when the day is absent, is the first bar of the series,
    or is provisional -- the three cases where a number could be produced and
    should not be.
    """

    if not series or not series.get("bars"):
        return None
    bars = list(series["bars"])
    index = next((i for i, bar in enumerate(bars) if bar["date"] == as_of), None)
    if index is None or index == 0:
        return None
    bar, previous = bars[index], bars[index - 1]
    if bar_is_provisional(bar):
        return None
    move = _percent(_decimal(bar["close"], "close"), _decimal(previous["close"], "close"))
    if move is None:
        return None
    return {
        "as_of": as_of,
        "close": str(bar["close"]),
        "previous_close": str(previous["close"]),
        "return_percent": move,
        "version_ref": series.get("version_ref"),
        "invocation_ref": bar.get("invocation_ref"),
    }


def basket_return(
    returns: Mapping[str, Mapping[str, Any]], *, exclude: str
) -> tuple[Decimal | None, int]:
    """The equal-weight return of every covered company except this one.

    Excluding the subject is the point: a five-name basket that includes the
    name being measured dilutes its own move by a fifth, so a company that
    moved five percent against four flat peers looks like it moved four
    percent against a basket that moved one.
    """

    members = [
        row["return_percent"] for ref, row in returns.items()
        if ref != exclude and row is not None
    ]
    if not members:
        return None, 0
    with localcontext() as ctx:
        ctx.prec = _PRECISION
        total = sum(members, Decimal(0))
        return total / Decimal(len(members)), len(members)


def _quantise(value: Decimal | None) -> str | None:
    return None if value is None else str(value.quantize(_QUANTUM))


def detect_abnormal_moves(
    *,
    series_by_company: Mapping[str, Mapping[str, Any] | None],
    as_of: str,
    thresholds: Mapping[str, Any],
    benchmark_series: Mapping[str, Any] | None = None,
    benchmark_ref: str | None = None,
) -> list[dict[str, Any]]:
    """Every company whose day cleared a threshold, as ResearchEvent bodies.

    Deterministic and side-effect free: it reads price versions and returns
    event bodies.  Recording them is the lane's job, which is what lets the
    same function be a read-only smoke test over a copy of the live Core.
    """

    absolute = _decimal(thresholds["absolute_move_percent"], "absolute_move_percent")
    basket_gap = _decimal(thresholds["excess_vs_basket_percent"], "excess_vs_basket_percent")
    benchmark_gap = _decimal(
        thresholds["excess_vs_benchmark_percent"], "excess_vs_benchmark_percent"
    )
    min_members = int(thresholds.get("min_basket_members") or 0)
    threshold_ref = str(thresholds.get("threshold_ref") or "abnormal-move:unversioned")

    returns: dict[str, dict[str, Any]] = {}
    for company_ref, series in series_by_company.items():
        row = daily_return(series, as_of)
        if row is not None:
            returns[company_ref] = row
    market = daily_return(benchmark_series, as_of) if benchmark_series else None

    events: list[dict[str, Any]] = []
    for company_ref in sorted(returns):
        row = returns[company_ref]
        move = row["return_percent"]
        basket, members = basket_return(returns, exclude=company_ref)
        excess_basket = None
        if basket is not None and members >= min_members:
            excess_basket = move - basket
        excess_benchmark = None if market is None else move - market["return_percent"]

        triggers = []
        if abs(move) >= absolute:
            triggers.append("absolute")
        if excess_basket is not None and abs(excess_basket) >= basket_gap:
            triggers.append("excess_vs_basket")
        if excess_benchmark is not None and abs(excess_benchmark) >= benchmark_gap:
            triggers.append("excess_vs_benchmark")
        if not triggers:
            continue
        source_refs = [row["version_ref"] or "market-price-series:unknown"]
        if row["invocation_ref"]:
            source_refs.append(row["invocation_ref"])
        events.append({
            "company_ref": company_ref,
            "kind": "price_move",
            "evidence_tier": "market_price",
            "occurred_at": day_start(as_of),
            "source_refs": source_refs,
            "payload": {
                "as_of": as_of,
                "close": row["close"],
                "previous_close": row["previous_close"],
                "return_percent": _quantise(move),
                "direction": "up" if move > 0 else ("down" if move < 0 else "flat"),
                "basket_return_percent": _quantise(basket) if members >= min_members else None,
                "excess_vs_basket_percent": _quantise(excess_basket),
                "basket_members": members,
                # Named even when there is no series for it, because "we had no
                # market comparator that day" is a fact about the event.
                "benchmark_ref": benchmark_ref,
                "benchmark_return_percent": None if market is None
                else _quantise(market["return_percent"]),
                "excess_vs_benchmark_percent": _quantise(excess_benchmark),
                "trigger": ",".join(triggers),
                "threshold_percent": str(absolute),
                "price_version_ref": row["version_ref"],
                "invocation_ref": row["invocation_ref"],
            },
        })
    return events


# The fund is fundamental long-biased and a coverage thesis is a reason to own
# the name (blueprint v1.0 §1, the owner's own description of the team).  So a
# thesis with no stance recorded against it is read as long, and the assumption
# is a named constant rather than an inference from prose: guessing a direction
# out of an ``implied_expectation`` sentence is exactly the kind of reading
# this codebase refuses to do in a detector.  A per-thesis override lives in
# the tracking policy, so a short thesis is a line of policy and not a code
# change.
DEFAULT_THESIS_STANCE = "long"
THESIS_STANCES: tuple[str, ...] = ("long", "short")


def cumulative_return(
    series: Mapping[str, Any] | None, *, from_date: str, as_of: str
) -> Decimal | None:
    """The move from the close on ``from_date`` to the close on ``as_of``.

    ``None`` when either end is missing or when the far end is provisional --
    the same exclusion the daily detector makes, for the same reason: a window
    that ends on an afternoon's last trade would fire, and then fire again on a
    different number when the day settled.
    """

    if not series or not series.get("bars"):
        return None
    bars = list(series["bars"])
    end = next((i for i, row in enumerate(bars) if row["date"] == as_of), None)
    start = next((i for i, row in enumerate(bars) if row["date"] == from_date), None)
    if end is None or start is None or start >= end:
        return None
    if bar_is_provisional(bars[end]):
        return None
    return _percent(
        _decimal(bars[end]["close"], "close"), _decimal(bars[start]["close"], "close")
    )


def detect_price_divergences(
    *,
    series_by_company: Mapping[str, Mapping[str, Any] | None],
    dates: Sequence[str],
    thresholds: Mapping[str, Any],
    stances: Mapping[str, Mapping[str, str]],
    suppress: Mapping[str, bool] | None = None,
) -> list[dict[str, Any]]:
    """Companies whose window ran against what their thesis implies.

    ``dates`` is the settled trading days everyone shares, oldest first; the
    window is the whole of it.  ``stances`` maps a company to one thesis and
    the direction that thesis implies, resolved by the caller so this stays a
    price function.  ``suppress`` marks companies that already have a
    divergence event inside this window -- without it a divergence that
    persists for a fortnight would produce a fresh event every day, each with
    a different end date and therefore a different hash, and the idempotency
    rule would not catch a single one of them.
    """

    if len(dates) < 2:
        return []
    threshold = _decimal(
        thresholds["divergence_vs_basket_percent"], "divergence_vs_basket_percent"
    )
    window_days = int(thresholds.get("window_trading_days") or len(dates))
    min_members = int(thresholds.get("min_basket_members") or 0)
    from_date, as_of = dates[0], dates[-1]
    suppress = dict(suppress or {})

    cumulative: dict[str, Decimal] = {}
    for company_ref, series in series_by_company.items():
        value = cumulative_return(series, from_date=from_date, as_of=as_of)
        if value is not None:
            cumulative[company_ref] = value

    events: list[dict[str, Any]] = []
    for company_ref in sorted(cumulative):
        stance = stances.get(company_ref)
        if stance is None or suppress.get(company_ref):
            continue
        direction = stance.get("stance", DEFAULT_THESIS_STANCE)
        if direction not in THESIS_STANCES:
            continue
        members = [value for ref, value in cumulative.items() if ref != company_ref]
        if len(members) < min_members:
            continue
        with localcontext() as ctx:
            ctx.prec = _PRECISION
            basket = sum(members, Decimal(0)) / Decimal(len(members))
        excess = cumulative[company_ref] - basket
        # Against the thesis: a long thesis wants the excess positive, a short
        # thesis wants it negative. The divergence is how far the wrong way it
        # went, so a name that is merely flat produces nothing.
        against = -excess if direction == "long" else excess
        if against < threshold:
            continue
        events.append({
            "company_ref": company_ref,
            "kind": "price_divergence",
            "evidence_tier": "market_price",
            "occurred_at": day_start(as_of),
            "source_refs": [
                (series_by_company[company_ref] or {}).get("version_ref")
                or "market-price-series:unknown",
                stance["thesis_ref"],
            ],
            "payload": {
                "window_days": window_days,
                "from_date": from_date,
                "as_of": as_of,
                "cumulative_return_percent": _quantise(cumulative[company_ref]),
                "basket_return_percent": _quantise(basket),
                "excess_vs_basket_percent": _quantise(excess),
                "basket_members": len(members),
                "thesis_ref": stance["thesis_ref"],
                "thesis_stance": direction,
                "divergence_percent": _quantise(against),
                "threshold_percent": str(threshold),
                "price_version_ref": (series_by_company[company_ref] or {}).get("version_ref"),
            },
        })
    return events


def latest_settled_date(series_by_company: Mapping[str, Mapping[str, Any] | None]) -> str | None:
    """The newest trading day every-ish company has a settled bar for.

    The newest date *any* company has would make the basket one name wide on
    the day one company's fetch ran first; the newest date they all have is
    the last day a comparison is honest about.
    """

    dates: list[set[str]] = []
    for series in series_by_company.values():
        if not series or not series.get("bars"):
            continue
        dates.append({
            bar["date"] for bar in series["bars"] if not bar_is_provisional(bar)
        })
    if not dates:
        return None
    shared = set.intersection(*dates) if len(dates) > 1 else dates[0]
    return max(shared) if shared else None


def recent_settled_dates(
    series_by_company: Mapping[str, Mapping[str, Any] | None], *, limit: int
) -> list[str]:
    """The last ``limit`` trading days everyone has a settled bar for, oldest first."""

    dates: list[set[str]] = []
    for series in series_by_company.values():
        if not series or not series.get("bars"):
            continue
        dates.append({
            bar["date"] for bar in series["bars"] if not bar_is_provisional(bar)
        })
    if not dates:
        return []
    shared = set.intersection(*dates) if len(dates) > 1 else dates[0]
    return sorted(shared)[-max(1, int(limit)):]


__all__ = [
    "DEFAULT_THESIS_STANCE",
    "THESIS_STANCES",
    "MarketEventError",
    "basket_return",
    "cumulative_return",
    "daily_return",
    "detect_abnormal_moves",
    "detect_price_divergences",
    "latest_settled_date",
    "recent_settled_dates",
]
