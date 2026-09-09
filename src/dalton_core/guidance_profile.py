"""P12f: what management guided, what happened, and whether that is a pattern.

The owner's sentence for this slice is "know the style in which management
gives guidance every time".  A style is not a impression; it is a table -- one
row per period, the guided range beside the number that arrived -- and a rule
applied to that table.  So this module computes the table and the
classification deterministically, and the model is only ever allowed to write
the prose *around* an already-computed answer.  A model asked whether
management is conservative will answer; it will not have counted.

Three deliberate refusals.

**A range is read by a stated rule or not at all.**  A guided range lives in
prose ("we now expect revenue growth of 5% to 7%") because a Claim carries one
scalar ``value``.  Every event therefore records ``guide_basis`` -- which rule
read the numbers out of which text -- the same discipline P12b applied to
``as_of``: a number nobody can check how we got is not evidence.  A statement
no rule reads contributes no event and is counted in ``unparsed`` rather than
guessed at.

**A comparison needs the same measure and the same unit.**  "Revenue growth of
5-7%" beside "revenue of USD 18.7bn" is not a beat, it is a category error, and
a table that lets the two meet will manufacture a pattern out of nothing.

**Below four settled events there is no style.**  Three quarters of beats is a
run; a style is what survives a cycle.  ``insufficient_data`` is a real answer
and the commonest correct one today: the Ledger's guidance material is thin,
and a classification produced from two rows would be read as if it were an
observation.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from datetime import date
from decimal import Decimal, InvalidOperation
from types import MappingProxyType
from typing import Any

from .store import content_hash

SCHEMA_VERSION = "0.1"

# The words a style can be.  ``insufficient_data`` means the table is too thin
# to say; ``mixed`` means the table is thick enough and says no single thing.
GUIDANCE_STYLES: tuple[str, ...] = (
    "conservative", "beat_and_raise", "aggressive",
    # Owner decision, 2026-09-09: the fifth word. Without it "we have two rows"
    # and "we have twelve rows and they show nothing" share a name, and those
    # are opposite facts about a management team -- the first is a gap in our
    # evidence, the second is a finding about them.
    "mixed",
    "insufficient_data",
)

# What a guide and an actual have to be about before they can be compared.
# A closed list: an unrecognised measure produces no event rather than an
# event about "whatever these two numbers were".
MEASURES: tuple[str, ...] = (
    "revenue", "revenue_growth", "eps", "operating_margin",
    "free_cash_flow", "bookings",
)
_MEASURE_PATTERNS: Mapping[str, tuple[str, ...]] = MappingProxyType({
    "revenue_growth": ("revenue growth", "growth in revenue", "local-currency growth",
                       "local currency growth", "revenue_yoy", "yoy_growth",
                       "收入增速", "收入增长"),
    "eps": ("eps", "earnings per share", "每股收益"),
    "operating_margin": ("operating margin", "operating-margin", "营业利润率"),
    "free_cash_flow": ("free cash flow", "fcf", "自由现金流"),
    "bookings": ("bookings", "new bookings", "订单"),
    # Last, so that "revenue growth" is not read as "revenue".
    "revenue": ("revenue", "revenues", "收入"),
})

# Units, reduced to the only distinction that matters for a comparison: a
# percentage and an amount of money are never the same measurement.
_PERCENT_UNITS = frozenset({"percent", "%", "percentage", "pct", "percentage_point"})

DEVIATIONS: tuple[str, ...] = ("beat", "miss", "inline", "unknown")

_SPAN_RE = re.compile(r"^\s*(\d{4}-\d{2}-\d{2})\s*\.\.\s*(\d{4}-\d{2}-\d{2})\s*$")
_DATE_RE = re.compile(r"^\s*(\d{4}-\d{2}-\d{2})\s*$")


def period_bounds(value: Any) -> tuple[str | None, str | None]:
    """``(start, end)`` for the two period shapes this system writes.

    A Claim's period is a span (``2026-03-01..2026-05-31``) or a bare date; a
    statement line has a start and an end. A fiscal label (``Q2 FY2026``) is
    neither, and this deliberately does not resolve one: P12b refused to guess
    a company's fiscal calendar for exactly this reason, and a guide paired
    against the wrong quarter is worse than a guide paired against nothing.
    """

    text = str(value or "").strip()
    span = _SPAN_RE.match(text)
    if span:
        return span.group(1), span.group(2)
    date = _DATE_RE.match(text)
    if date:
        return None, date.group(1)
    return None, None


def period_key(value: Any) -> str:
    """What a guide and an actual have to agree on before they are compared.

    The period *end*, when the shape yields one, so that a Claim's span and a
    filed line's end join; otherwise the label itself, so that two labels can
    still meet each other and nothing else can.
    """

    text = str(value or "").strip()
    _, end = period_bounds(text)
    return end or text

# The rule, in words, stored beside every classification it produced.  A
# classification whose rule is only in the code cannot be argued with.
GUIDANCE_STYLE_RULE = (
    "A settled event has a guided range and an actual for the same measure in "
    "the same unit class. Fewer than 4 settled events: insufficient_data. "
    "Otherwise: beats >= 75% of settled and at least half of the consecutive "
    "guide pairs were revised upward -> beat_and_raise; beats >= 75% without "
    "those raises -> conservative; misses >= 50% -> aggressive; anything else "
    "-> mixed, which is a finding about them rather than a gap in our evidence."
)
MIN_SETTLED_EVENTS = 4
BEAT_SHARE = Decimal("0.75")
MISS_SHARE = Decimal("0.50")
RAISE_SHARE = Decimal("0.50")
MAX_EVENTS = 24


class GuidanceProfileError(ValueError):
    """The guidance profile is malformed."""


def _decimal(value: Any) -> Decimal | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _format(value: Decimal) -> str:
    raw = format(value, "f")
    if "." in raw:
        raw = raw.rstrip("0").rstrip(".")
    return raw or "0"


def measure_of(text: str) -> str | None:
    """Which of the closed measures this label or sentence is about."""

    lowered = (text or "").lower()
    for measure, patterns in _MEASURE_PATTERNS.items():
        if any(pattern in lowered for pattern in patterns):
            return measure
    return None


def unit_class(unit: Any) -> str:
    """``percent`` or ``amount``; the only distinction a comparison needs."""

    lowered = str(unit or "").strip().lower()
    return "percent" if lowered in _PERCENT_UNITS or lowered.endswith("%") else "amount"


# The rules that read a range out of a sentence, tried in order.  Each one is
# named, and the name is stored on the event.
_RANGE_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("between_x_and_y", re.compile(
        r"between\s+(-?\d+(?:\.\d+)?)\s*%?\s+and\s+(-?\d+(?:\.\d+)?)\s*%?",
        re.IGNORECASE)),
    ("x_to_y", re.compile(
        r"(?<![\d-])(-?\d+(?:\.\d+)?)\s*%?\s*(?:to|-|–|—|~|至|到)\s*"
        r"(-?\d+(?:\.\d+)?)\s*%")),
    # Anchored to a unit word on purpose. Unanchored, "2025-09-01..2026-05-31"
    # reads as a range from 9 to 2026 and every dated span in the ledger
    # becomes a guidance range.
    ("x_dash_y", re.compile(
        r"(?<![\d-])(\d+(?:\.\d+)?)\s*(?:-|–|—|~|至|到)\s*(\d+(?:\.\d+)?)\s*"
        r"(?:%|percent|pct|个百分点)")),
    ("point", re.compile(r"(-?\d+(?:\.\d+)?)\s*%")),
)


def read_range(text: str) -> dict[str, Any] | None:
    """The guided low and high, and which rule read them.

    A point guide is a range whose ends are equal; that is what a point guide
    means for the purpose of "did they beat it".
    """

    body = text or ""
    for name, pattern in _RANGE_RULES:
        match = pattern.search(body)
        if match is None:
            continue
        numbers = [_decimal(group) for group in match.groups()]
        if any(item is None for item in numbers):
            continue
        low, high = (numbers[0], numbers[-1])
        if low > high:
            low, high = high, low
        return {"low": low, "high": high, "guide_basis": name}
    return None


def _span_days(start: str | None, end: str | None) -> int:
    """How long a measured period is, for preferring a quarter over a year."""

    if not start or not end:
        return 10 ** 6
    try:
        return (date.fromisoformat(end) - date.fromisoformat(start)).days
    except ValueError:
        return 10 ** 6


def _index_actuals(
    actuals: Sequence[Mapping[str, Any]]
) -> dict[tuple[str, str], list[dict[str, Any]]]:
    """Settled numbers by (period key, measure), several per key on purpose.

    A quarter is filed twice -- in the 10-Q and again in the 10-K -- and the
    year-to-date figure carries the same period end as the quarter inside it.
    Keeping the candidates and choosing at pairing time is what stops a guide
    for one quarter from being marked a beat against nine months of revenue.
    """

    index: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in actuals:
        measure = row.get("measure") or measure_of(
            str(row.get("label") or row.get("text") or ""))
        value = _decimal(row.get("value"))
        raw_period = row.get("period")
        span_start, span_end = period_bounds(raw_period)
        span_start = row.get("period_start") or span_start
        span_end = row.get("period_end") or span_end
        key = period_key(span_end or raw_period)
        if measure not in MEASURES or value is None or not key:
            continue
        index.setdefault((key, measure), []).append({
            "value": value, "unit": row.get("unit"), "refs": [str(row["ref"])],
            "start": span_start, "end": span_end,
            "span_days": _span_days(span_start, span_end),
        })
    return index


def _choose_actual(
    rows: Sequence[Mapping[str, Any]], guide_start: str | None
) -> dict[str, Any]:
    """The candidate a guide is actually about.

    An exact span match wins; failing that the shortest measured period, so a
    quarterly guide meets the quarter rather than the year to date.
    """

    if guide_start:
        exact = [row for row in rows if row.get("start") == guide_start]
        if exact:
            return dict(exact[0])
    return dict(min(rows, key=lambda row: (row["span_days"], row["refs"][0])))


def guidance_events(
    guides: Sequence[Mapping[str, Any]],
    actuals: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Pair every readable guide with the number that answered it.

    ``guides`` are Claim rows the index filed under ``guidance_style``;
    ``actuals`` are rows from anywhere that reports a settled number for a
    period -- quantitative Claims, a filed statement line, a forecast model's
    ``actual`` cells. Neither is fetched here: this function is arithmetic over
    what it was handed, so it can be replayed and tested without a Core.
    """

    by_key = _index_actuals(actuals)
    events: list[dict[str, Any]] = []
    unparsed: list[dict[str, Any]] = []
    for row in guides:
        text = str(row.get("text") or row.get("normalized_statement") or "")
        raw_period = row.get("period")
        key = period_key(raw_period)
        guide_start, _ = period_bounds(raw_period)
        measure = row.get("measure") or measure_of(
            f"{row.get('label') or row.get('metric_or_aspect') or ''} {text}")
        parsed = read_range(text)
        if not key or measure not in MEASURES or parsed is None:
            unparsed.append({
                "ref": str(row.get("ref") or ""),
                "reason": ("no period" if not key else
                           "no closed measure" if measure not in MEASURES else
                           "no rule read a range from the statement"),
            })
            continue
        guide_unit = row.get("unit") or ("percent" if "%" in text else None)
        candidates = by_key.get((key, measure))
        deviation = {"verdict": "unknown", "distance": None,
                     "reason": "no actual for this period and measure"}
        actual_wire = None
        if candidates:
            actual = _choose_actual(candidates, guide_start)
            actual_wire = {
                "value": _format(actual["value"]), "unit": actual["unit"],
                "refs": list(actual["refs"]),
            }
            if unit_class(guide_unit) != unit_class(actual["unit"]):
                deviation = {
                    "verdict": "unknown", "distance": None,
                    "reason": "the guide and the actual are not in the same unit class",
                }
            else:
                value = actual["value"]
                if value > parsed["high"]:
                    deviation = {"verdict": "beat",
                                 "distance": _format(value - parsed["high"]),
                                 "reason": "above the top of the guided range"}
                elif value < parsed["low"]:
                    deviation = {"verdict": "miss",
                                 "distance": _format(parsed["low"] - value),
                                 "reason": "below the bottom of the guided range"}
                else:
                    deviation = {"verdict": "inline", "distance": "0",
                                 "reason": "inside the guided range"}
        events.append({
            "period": key,
            "measure": measure,
            "guide": {
                "low": _format(parsed["low"]), "high": _format(parsed["high"]),
                "unit": guide_unit, "guide_basis": parsed["guide_basis"],
                "refs": [str(row.get("ref") or "")],
            },
            "actual": actual_wire,
            "deviation": deviation,
        })
    events.sort(key=lambda item: (item["period"], item["measure"]))
    return {"events": events[:MAX_EVENTS], "unparsed": unparsed[:MAX_EVENTS]}


def classify(events: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Apply :data:`GUIDANCE_STYLE_RULE` and say which branch answered."""

    settled = [item for item in events if item["deviation"]["verdict"] in
               ("beat", "miss", "inline")]
    counts = {word: 0 for word in DEVIATIONS}
    for item in events:
        counts[item["deviation"]["verdict"]] += 1
    total = len(settled)
    if total < MIN_SETTLED_EVENTS:
        return {
            "classification": "insufficient_data", "counts": counts, "settled": total,
            "basis": (f"{total} settled events; the rule needs "
                      f"{MIN_SETTLED_EVENTS} before it will call a style"),
        }
    beats = Decimal(counts["beat"]) / Decimal(total)
    misses = Decimal(counts["miss"]) / Decimal(total)
    raises = _raise_share(settled)
    if beats >= BEAT_SHARE and raises is not None and raises >= RAISE_SHARE:
        return {"classification": "beat_and_raise", "counts": counts, "settled": total,
                "basis": (f"{counts['beat']}/{total} beats and {_format(raises * 100)}% "
                          "of consecutive guides were revised upward")}
    if beats >= BEAT_SHARE:
        return {"classification": "conservative", "counts": counts, "settled": total,
                "basis": f"{counts['beat']}/{total} beats without a pattern of raises"}
    if misses >= MISS_SHARE:
        return {"classification": "aggressive", "counts": counts, "settled": total,
                "basis": f"{counts['miss']}/{total} misses"}
    return {"classification": "mixed", "counts": counts, "settled": total,
            "basis": (f"{total} settled events and no pattern reached its "
                      "threshold (beats <75%, misses <50%): the table is thick "
                      "enough to read and says no single thing")}


def _raise_share(settled: Sequence[Mapping[str, Any]]) -> Decimal | None:
    """How often the next guide for a measure was above the last one."""

    by_measure: dict[str, list[Mapping[str, Any]]] = {}
    for item in settled:
        by_measure.setdefault(item["measure"], []).append(item)
    pairs = raised = 0
    for rows in by_measure.values():
        ordered = sorted(rows, key=lambda item: item["period"])
        for before, after in zip(ordered, ordered[1:]):
            pairs += 1
            if Decimal(after["guide"]["high"]) > Decimal(before["guide"]["high"]):
                raised += 1
    if not pairs:
        return None
    return Decimal(raised) / Decimal(pairs)


def build_profile(
    *,
    company_ref: str,
    guides: Sequence[Mapping[str, Any]],
    actuals: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """The whole computed table, ready to be carried by the dossier."""

    table = guidance_events(guides, actuals)
    verdict = classify(table["events"])
    refs = sorted({
        ref
        for item in table["events"]
        for ref in list(item["guide"]["refs"]) + list((item["actual"] or {}).get("refs") or [])
        if ref
    })
    return validate_profile({
        "schema_version": SCHEMA_VERSION,
        "company_ref": company_ref,
        "events": table["events"],
        "unparsed": table["unparsed"],
        "classification": verdict["classification"],
        "counts": verdict["counts"],
        "settled": verdict["settled"],
        "basis": verdict["basis"],
        "rule": GUIDANCE_STYLE_RULE,
        "rule_hash": content_hash(GUIDANCE_STYLE_RULE),
        "refs": refs,
    })


def validate_profile(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise GuidanceProfileError("guidance profile must be an object")
    wire = dict(value)
    expected = {
        "schema_version", "company_ref", "events", "unparsed", "classification",
        "counts", "settled", "basis", "rule", "rule_hash", "refs",
    }
    if set(wire) != expected or wire.get("schema_version") != SCHEMA_VERSION:
        raise GuidanceProfileError("guidance profile has an invalid closed shape")
    if wire["classification"] not in GUIDANCE_STYLES:
        raise GuidanceProfileError(
            f"classification must be one of {', '.join(GUIDANCE_STYLES)}")
    if wire["rule_hash"] != content_hash(wire["rule"]):
        raise GuidanceProfileError("the profile's rule hash is not its rule")
    if not isinstance(wire["events"], list) or len(wire["events"]) > MAX_EVENTS:
        raise GuidanceProfileError(f"events must be a list of at most {MAX_EVENTS}")
    for index, item in enumerate(wire["events"]):
        if not isinstance(item, Mapping) or set(item) != {
            "period", "measure", "guide", "actual", "deviation"
        }:
            raise GuidanceProfileError(f"events[{index}] has an invalid closed shape")
        if item["measure"] not in MEASURES:
            raise GuidanceProfileError(f"events[{index}].measure is not a closed measure")
        if set(item["guide"]) != {"low", "high", "unit", "guide_basis", "refs"}:
            raise GuidanceProfileError(f"events[{index}].guide has an invalid closed shape")
        if item["actual"] is not None and set(item["actual"]) != {"value", "unit", "refs"}:
            raise GuidanceProfileError(f"events[{index}].actual has an invalid closed shape")
        if set(item["deviation"]) != {"verdict", "distance", "reason"}:
            raise GuidanceProfileError(f"events[{index}].deviation has an invalid closed shape")
        if item["deviation"]["verdict"] not in DEVIATIONS:
            raise GuidanceProfileError(f"events[{index}].deviation.verdict is not closed")
    return wire


def render_profile_table(profile: Mapping[str, Any]) -> str:
    """The computed table as a prompt table: one row per event, tab separated.

    A table rather than JSON for the reason ``company_model_spec`` found: the
    same content as objects costs several times the bytes, and the router
    reserves budget against the size of the prompt.
    """

    lines = [
        "period\tmeasure\tguided_low\tguided_high\tunit\tactual\tverdict\tguide_refs",
    ]
    for item in profile.get("events") or []:
        actual = item["actual"] or {}
        lines.append("\t".join([
            item["period"], item["measure"], item["guide"]["low"], item["guide"]["high"],
            str(item["guide"]["unit"] or "-"), str(actual.get("value") or "-"),
            item["deviation"]["verdict"], ",".join(item["guide"]["refs"]),
        ]))
    lines.append(
        f"classification\t{profile['classification']}\t({profile['basis']})")
    return "\n".join(lines)


__all__ = [
    "BEAT_SHARE",
    "period_bounds",
    "period_key",
    "DEVIATIONS",
    "GUIDANCE_STYLES",
    "GUIDANCE_STYLE_RULE",
    "GuidanceProfileError",
    "MAX_EVENTS",
    "MEASURES",
    "MIN_SETTLED_EVENTS",
    "MISS_SHARE",
    "SCHEMA_VERSION",
    "build_profile",
    "classify",
    "guidance_events",
    "measure_of",
    "read_range",
    "render_profile_table",
    "unit_class",
    "validate_profile",
]
