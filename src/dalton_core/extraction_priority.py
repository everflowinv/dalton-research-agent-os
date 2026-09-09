"""P10x: what the extraction lane reads next, and how much of it fits in a tick.

Two decisions used to be made badly for the same reason -- nobody had written
down what the lane is optimising.

**Order.**  ``mission_stage.review_sort_key`` sorts by company first and kind
second, so the top-priority company's news is read before the second company's
earnings call.  That is the right rule for *acquisition* -- a P0 company's
checklist is closed before a P2 company's -- and the wrong one for *reading*,
because the value of a document is mostly its kind: a filing beats a transcript
beats a broker note beats a sales note beats a news page, whoever it is about.
So reading orders by evidence value first, and uses company priority only to
break ties inside one kind.

Between two documents of the same kind the tie is broken by **coverage
thinness**: the company that holds fewest Claims of that tier is read first.
This is the rule that would have prevented the live shape it was written for --
Cognizant holding a hundred and forty-nine sell-side Claims while Accenture
holds five, because Cognizant's queries happened to return more notes first.
Thinness makes the lane spend its next window where the evidence is thinnest
rather than where it is already thickest.

**Batch size.**  ``max_windows_per_tick`` is a number somebody typed.  It
should be derived, because the two things that actually bound it are known
exactly: the day's cost cap and paid-call cap, and the rate card of the model
the routing policy pins.  ``safe_windows_per_tick`` does that arithmetic, and
``window_reservation_micros`` does the smaller, sharper version of it -- what a
single window can possibly cost, instead of the flat five cents the lane
reserves today against an observed mean of three hundredths of a cent.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from decimal import Decimal, ROUND_CEILING
from typing import Any

from .document_provenance import evidence_value, tier_for_spec

SCHEMA_VERSION = "0.1"

# The launcher refuses anything outside this, and so does the child CLI. The
# derived bound is clamped into it rather than allowed to argue with it.
MIN_WINDOWS_PER_TICK = 1
MAX_WINDOWS_PER_TICK = 50

# How much of the day's paid capacity the prose pass may plan to use. The lane
# shares the ledger with the planner, the screen drafter, the claim index and
# the quality loop; taking the whole cap for reading would starve them, and a
# lane that starves the brain to read more is not reading usefully.
DEFAULT_LANE_SHARE = Decimal("0.5")
# The controller wakes every five minutes.
DEFAULT_TICKS_PER_DAY = 288


def review_sort_key(
    review: Mapping[str, Any],
    *,
    spec_by_document: Mapping[str, str],
    company_rank: Mapping[str, int],
    thinness_rank: Mapping[tuple[str, str], int] | None = None,
) -> tuple[int, int, int, str, str]:
    """Reading order: kind of evidence, then who needs it most, then age.

    ``thinness_rank`` maps (company_ref, tier) to a position -- 0 for the
    company holding fewest Claims of that tier.  It is optional because a
    caller that cannot compute it (no index yet, unreadable ledger) must still
    get a defined order rather than an exception; without it the mission's own
    company priority breaks the tie, which is the old behaviour one level down.
    """

    document_ref = review.get("document_ref") or ""
    company_ref = review.get("company_ref") or ""
    tier = tier_for_spec(spec_by_document.get(document_ref, ""))
    thin = 0
    if thinness_rank:
        thin = thinness_rank.get((company_ref, tier), len(thinness_rank))
    return (
        evidence_value(tier),
        thin,
        company_rank.get(company_ref, len(company_rank)),
        str(review.get("created_at") or ""),
        str(review.get("review_id") or ""),
    )


def thinness_ranks(
    counts: Mapping[str, Mapping[str, int]],
    *,
    companies: Sequence[str] | None = None,
) -> dict[tuple[str, str], int]:
    """Position of each company within each tier, thinnest first.

    ``counts`` is company_ref -> tier -> how many Claims of that tier the
    company already holds.  A company absent from a tier holds none of it and
    therefore ranks first: the whole point is to send the next window where
    nothing has been read yet.
    """

    from .document_provenance import TIERS

    known = list(companies) if companies is not None else sorted(counts)
    ranks: dict[tuple[str, str], int] = {}
    for tier in TIERS:
        ordered = sorted(
            known,
            key=lambda company: (int((counts.get(company) or {}).get(tier, 0) or 0), company),
        )
        for position, company in enumerate(ordered):
            ranks[(company, tier)] = position
    return ranks


def window_reservation_micros(
    rate_card: Mapping[str, Any],
    work_budget: Mapping[str, Any],
    *,
    floor_micros: int = 1,
) -> int:
    """The most one window can cost this model, in micros.

    Derived from the two numbers that decide it: the WorkOrder's own token
    bounds -- which the adapter enforces before the call -- and the served
    profile's published price.  Rounded up, so a reservation is never short.

    This is what a reservation is *for*.  The lane reserves a flat $0.05 and
    settles a mean of $0.0003, which reserves a hundred and seventy times the
    money it spends; on a ledger where an open reservation is charged against
    the day cap until it settles, that is the number that decides how many
    windows a day can hold, not the money.
    """

    def _decimal(value: Any, name: str) -> Decimal:
        if isinstance(value, bool) or not isinstance(value, (int, float, str, Decimal)):
            raise ValueError(f"{name} is not a number")
        try:
            amount = Decimal(str(value))
        except ArithmeticError as exc:
            raise ValueError(f"{name} is not a number") from exc
        if not amount.is_finite() or amount < 0:
            raise ValueError(f"{name} is not a usable amount")
        return amount

    inputs = _decimal(work_budget.get("max_input_tokens"), "max_input_tokens")
    outputs = _decimal(work_budget.get("max_output_tokens"), "max_output_tokens")
    per_input = _decimal(rate_card.get("input_per_million_usd"), "input_per_million_usd")
    per_output = _decimal(rate_card.get("output_per_million_usd"), "output_per_million_usd")
    usd = (inputs * per_input + outputs * per_output) / Decimal(1_000_000)
    micros = int((usd * Decimal(1_000_000)).to_integral_value(rounding=ROUND_CEILING))
    return max(int(floor_micros), micros)


def safe_windows_per_tick(
    *,
    day_cap_micros: int,
    max_daily_paid_calls: int,
    rate_card: Mapping[str, Any],
    work_budget: Mapping[str, Any],
    passes_per_window: int = 1,
    ticks_per_day: int = DEFAULT_TICKS_PER_DAY,
    lane_share: Decimal | float | str = DEFAULT_LANE_SHARE,
    reservation_micros: int | None = None,
) -> dict[str, Any]:
    """How many windows a tick may read without the day's caps being the answer.

    Both caps bind, and the smaller one wins: money (the day cost cap against
    what a window can cost) and count (the paid-call cap against how many calls
    a window makes -- the figures and metric-discovery passes are each another
    call on the same window).

    Returns the bound *and its arithmetic*, because a lane that silently
    changed its own batch size would be worse than one that never grew.  The
    caller shows the derivation; nothing here is hardcoded except the launcher
    bounds the value is clamped into.
    """

    if not isinstance(ticks_per_day, int) or isinstance(ticks_per_day, bool) or ticks_per_day < 1:
        raise ValueError("ticks_per_day must be a positive integer")
    if not isinstance(passes_per_window, int) or isinstance(passes_per_window, bool) or passes_per_window < 1:
        raise ValueError("passes_per_window must be a positive integer")
    share = Decimal(str(lane_share))
    if not share.is_finite() or not (Decimal(0) < share <= Decimal(1)):
        raise ValueError("lane_share must be a fraction in (0, 1]")
    for name, value in (("day_cap_micros", day_cap_micros),
                        ("max_daily_paid_calls", max_daily_paid_calls)):
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise ValueError(f"{name} must be a non-negative integer")

    per_window = (window_reservation_micros(rate_card, work_budget)
                  if reservation_micros is None else int(reservation_micros))
    if per_window <= 0:
        raise ValueError("a window reservation must be positive")
    per_window_total = per_window * passes_per_window

    budget_micros = int(Decimal(day_cap_micros) * share)
    by_cost = budget_micros // per_window_total // ticks_per_day
    calls = int(Decimal(max_daily_paid_calls) * share)
    by_calls = calls // passes_per_window // ticks_per_day

    derived = min(by_cost, by_calls)
    windows = max(MIN_WINDOWS_PER_TICK, min(MAX_WINDOWS_PER_TICK, derived))
    return {
        "schema_version": SCHEMA_VERSION,
        "windows_per_tick": windows,
        "reservation_micros": per_window,
        "reservation_micros_per_window_all_passes": per_window_total,
        "bound_by": ("paid_calls" if by_calls <= by_cost else "day_cost"),
        "windows_by_day_cost": by_cost,
        "windows_by_paid_calls": by_calls,
        "clamped": derived != windows,
        "ticks_per_day": ticks_per_day,
        "lane_share": str(share),
        "passes_per_window": passes_per_window,
    }


__all__ = [
    "DEFAULT_LANE_SHARE",
    "DEFAULT_TICKS_PER_DAY",
    "MAX_WINDOWS_PER_TICK",
    "MIN_WINDOWS_PER_TICK",
    "SCHEMA_VERSION",
    "review_sort_key",
    "safe_windows_per_tick",
    "thinness_ranks",
    "window_reservation_micros",
]
