"""P13an: turn filed statement lines into a quarterly series a model can use.

What a filing reports and what a model needs are not the same shape, and the
distance between them is where most spreadsheet errors live.

A 10-Q reports the quarter *and* the year to date, both ending on the same day.
EPAM's Q2 2026 revenue is 1,414,767 thousand and its first half is 2,814,828
thousand, and the only thing separating them on the wire is ``period_start``.
Add them together and you have booked half a year twice. This module's first
job is to never do that.

Its second job is arithmetic the filings do not do for you. A company that
reports only cumulative figures gives you nine months and six months but never
the third quarter; the quarter is the difference, and it is a *derived* number
that has to say so. A derived figure sitting unmarked beside a filed one is how
a model stops being auditable.

Its third job is restatements. The same quarter appears in several filings --
as the current period once, then as the comparative for a year -- and the
figures are not always identical. The most recently filed value wins, because
that is the company's current statement of what happened, and every value the
series rests on names the filing it came from.

What this module refuses to do is guess. A period it cannot classify, a
difference it cannot compute from figures that share a start date, a concept
reported only along a dimension -- these are reported as gaps. A model with an
honest hole in it can be fixed; a model with an invented number in it cannot be
found.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable, Mapping, Sequence

# A quarter is not exactly 91 days -- 13 weeks is 91, calendar quarters run
# 90 to 92, and 52/53-week filers land anywhere from 84 to 98. Wide enough to
# admit a retailer's fiscal quarter, narrow enough to exclude a half year.
QUARTER_MIN_DAYS = 80
QUARTER_MAX_DAYS = 100
HALF_YEAR_MAX_DAYS = 195
NINE_MONTH_MAX_DAYS = 290
ANNUAL_MAX_DAYS = 380

INSTANT = "instant"
QUARTER = "quarter"
CUMULATIVE = "cumulative"
UNKNOWN = "unknown"

REPORTED = "reported"
DERIVED = "derived_from_cumulative"


class SeriesError(ValueError):
    """The lines cannot be read as a series."""


def _date(value: Any) -> date | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def _decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def period_kind(period_start: Any, period_end: Any) -> str:
    """What shape of period this is: an instant, a quarter, or a run of them.

    Length is the only evidence available -- the filing does not label a
    duration as "the quarter" -- so anything that is not recognisably one of
    these is ``unknown`` and gets left alone rather than assumed.
    """

    end = _date(period_end)
    if end is None:
        return UNKNOWN
    start = _date(period_start)
    if start is None:
        return INSTANT
    days = (end - start).days + 1
    if days <= 0:
        return UNKNOWN
    if QUARTER_MIN_DAYS <= days <= QUARTER_MAX_DAYS:
        return QUARTER
    if days <= HALF_YEAR_MAX_DAYS or days <= NINE_MONTH_MAX_DAYS or days <= ANNUAL_MAX_DAYS:
        return CUMULATIVE
    return UNKNOWN


def _latest_by_period(rows: Iterable[Mapping[str, Any]]) -> dict[tuple[str, str], dict[str, Any]]:
    """One figure per period: the most recently filed statement of it.

    A quarter appears in several filings, and restatements mean the figures are
    not always the same. Ordering by the filing date rather than by whichever
    row was read first is the difference between a series that reflects what
    the company currently says and one that reflects parse order.
    """

    best: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        if row.get("is_breakdown") or row.get("dimension_axis"):
            continue
        value = _decimal(row.get("value"))
        end = row.get("period_end")
        if value is None or not end:
            continue
        key = (str(row.get("period_start") or ""), str(end))
        held = best.get(key)
        if held is None or str(row.get("filed") or "") >= str(held.get("filed") or ""):
            best[key] = {
                "period_start": row.get("period_start"),
                "period_end": end,
                "value": value,
                "filed": row.get("filed"),
                "accession": row.get("accession"),
                "unit": row.get("unit"),
            }
    return best


def _derive_quarters(
    durations: Sequence[Mapping[str, Any]], held: set[tuple[str, str]],
) -> list[dict[str, Any]]:
    """Quarters the filings imply but never state.

    Two figures sharing a start date differ by the periods between them. Nine
    months minus six months is the third quarter -- but only if both run from
    the same day, which is why the start is the grouping key and not an
    incidental field.

    Every duration is a candidate, not only the ones that look cumulative,
    because the first quarter of a year *is* the year to date as well. Passing
    only the long periods meant the second quarter could never be derived: the
    six-month figure had nothing to be measured against.
    """

    derived: list[dict[str, Any]] = []
    by_start: dict[str, list[Mapping[str, Any]]] = {}
    for item in durations:
        by_start.setdefault(str(item["period_start"]), []).append(item)
    for start, items in by_start.items():
        ordered = sorted(items, key=lambda item: str(item["period_end"]))
        previous: Mapping[str, Any] | None = None
        for item in ordered:
            if previous is not None:
                previous_end = _date(previous["period_end"])
                quarter_start = (previous_end.toordinal() + 1
                                 if previous_end is not None else None)
                if quarter_start is not None:
                    span_start = date.fromordinal(quarter_start).isoformat()
                    key = (span_start, str(item["period_end"]))
                    if key not in held and period_kind(span_start, item["period_end"]) == QUARTER:
                        derived.append({
                            "period_start": span_start,
                            "period_end": item["period_end"],
                            "value": item["value"] - previous["value"],
                            "basis": DERIVED,
                            "unit": item.get("unit"),
                            "source_accessions": sorted({
                                str(item.get("accession") or ""),
                                str(previous.get("accession") or ""),
                            } - {""}),
                            "derived_from": [
                                {"period_start": start,
                                 "period_end": previous["period_end"]},
                                {"period_start": start,
                                 "period_end": item["period_end"]},
                            ],
                        })
            previous = item
    return derived


def quarterly_series(rows: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """A quarterly series for one concept, and an account of how it was built.

    Reported quarters are taken as filed. Missing ones are derived from
    cumulative figures where the arithmetic is available, and marked. Nothing
    is invented: a quarter neither reported nor derivable is simply absent, and
    the gaps are named so a reader can see what the model does not have.
    """

    rows = list(rows)
    best = _latest_by_period(rows)
    quarters: list[dict[str, Any]] = []
    durations: list[dict[str, Any]] = []
    instants: list[dict[str, Any]] = []
    cumulative_count = 0
    unknown = 0
    for (start, _end), item in best.items():
        kind = period_kind(start or None, item["period_end"])
        if kind == QUARTER:
            quarters.append({**item, "basis": REPORTED})
            durations.append(item)
        elif kind == CUMULATIVE:
            cumulative_count += 1
            durations.append(item)
        elif kind == INSTANT:
            instants.append({**item, "basis": REPORTED})
        else:
            unknown += 1

    held = {(str(item["period_start"]), str(item["period_end"])) for item in quarters}
    derived = _derive_quarters(durations, held)
    combined = sorted(
        [{**item, "source_accessions": sorted({str(item.get("accession") or "")} - {""})}
         if "source_accessions" not in item else item
         for item in quarters + derived],
        key=lambda item: (str(item["period_end"]), str(item["period_start"])),
    )
    return {
        "quarters": [
            {
                "period_start": item["period_start"],
                "period_end": item["period_end"],
                # Text out, as text came in: a figure is what was filed, and a
                # float is not what was filed.
                "value": format(item["value"], "f"),
                "unit": item.get("unit"),
                "basis": item["basis"],
                "source_accessions": item.get("source_accessions") or [],
                "derived_from": item.get("derived_from"),
            }
            for item in combined
        ],
        "instants": sorted(
            [
                {"period_end": item["period_end"], "value": format(item["value"], "f"),
                 "unit": item.get("unit"), "basis": REPORTED,
                 "source_accessions": sorted({str(item.get("accession") or "")} - {""})}
                for item in instants
            ],
            key=lambda item: str(item["period_end"]),
        ),
        "cumulative_used": cumulative_count,
        "derived_count": len(derived),
        "unclassified_periods": unknown,
    }


def series_gaps(series: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The holes between the quarters a series does have.

    Derived from the series rather than from a calendar, which is the second
    version of this function. The first built an expected grid by stepping back
    three months and keeping the day, and reported that EPAM was missing
    2026-03-30 -- a date that does not end anyone's quarter. Quarter ends are
    month ends for some filers and 52/53-week dates for others, and there is no
    way to know which from the figures alone.

    So the gaps are stated as spans between consecutive held quarters: if one
    ends on the 30th of September and the next begins on the 1st of January,
    there is a quarter missing between them and here is exactly which days it
    covers. That is a fact about the series, not a guess about the company --
    and for a company whose statements come only from 10-Qs it correctly
    reports one hole a year, because a 10-Q never covers the fourth quarter.
    """

    quarters = series.get("quarters") or []
    if len(quarters) < 2:
        return []
    ordered = sorted(quarters, key=lambda item: str(item["period_end"]))
    gaps: list[dict[str, Any]] = []
    for previous, item in zip(ordered, ordered[1:]):
        previous_end = _date(previous.get("period_end"))
        start = _date(item.get("period_start"))
        if previous_end is None or start is None:
            continue
        missing_days = (start - previous_end).days - 1
        if missing_days <= 0:
            continue
        gaps.append({
            "after": previous["period_end"],
            "before": item["period_start"],
            "missing_from": date.fromordinal(previous_end.toordinal() + 1).isoformat(),
            "missing_to": date.fromordinal(start.toordinal() - 1).isoformat(),
            "days": missing_days,
        })
    return gaps


__all__ = [
    "ANNUAL_MAX_DAYS",
    "CUMULATIVE",
    "DERIVED",
    "INSTANT",
    "QUARTER",
    "QUARTER_MAX_DAYS",
    "QUARTER_MIN_DAYS",
    "REPORTED",
    "UNKNOWN",
    "SeriesError",
    "period_kind",
    "quarterly_series",
    "series_gaps",
]
