"""P13-M3: which assumptions actually move the answer, and how far they have moved before.

A driver model (P13-M2) is a complete set of assumptions with the arithmetic
that follows from them. It says nothing about which of those assumptions
matter. Five drivers get equal billing in the record, and on any real company
one or two of them decide the bottom line while the rest are rounding. A person
reading the model cannot see that, so a person reading the model argues about
the wrong number.

This slice answers three questions and refuses to answer anything else:

1. **Which drivers matter.** Every assumption gets a one-point move and the
   bottom line is recomputed, which gives its elasticity. The *ranking*,
   though, is by how far the bottom line moves across the driver's own
   historical range -- because every share assumption in this model is a share
   of the same revenue, so a one-point move in each of them moves operating
   income by exactly the same number, and a ranking by elasticity alone would
   be a ranking by rounding noise. What separates cost of revenue from
   depreciation is not their slope, it is that one of them has moved four
   points of revenue in three years and the other has moved a quarter of one.
   The rule is frozen (``SELECTION_RULE_REF``) and hashed into the record,
   because a ranking whose rule can change silently is a ranking nobody can
   reproduce.
2. **How far each one has moved before.** The peak, trough and mean of the same
   measure over the filed history, with the quarter each extreme happened in
   and the accession it was filed under. A sensitivity band invented out of
   round numbers -- plus or minus ten percent -- is a decoration. A band read
   off the company's own history is a fact, and it is usually wider and more
   lopsided than anyone guesses.
3. **What the model says at each of those.** The income chain recomputed at the
   historical trough, the mean, the peak, and at our own estimate, so the four
   numbers sit beside each other and the reader can see whether our estimate is
   near an edge of the range the company has actually lived in. Each column
   **holds that one level across every open quarter** and says so; none of them
   is a path. "The trough" is not the trough quarter happening once, it is the
   whole horizon at that level -- a harsher question, and one the reader has to
   know is being asked.

Plus a bridge from those numbers to the street's, which is
:mod:`consensus_bridge`.

**Nothing here publishes a forecast.** Every recomputation happens in memory,
through the same ``compute_results`` the model itself uses, and lands in a
``SensitivityProjection`` -- a ``derived_deterministic`` record bound to the
ForecastModelVersion's ``content_hash`` and the statement ``inputs_hash`` it
rested on. A what-if is not a view: the moment a scenario could be published as
a forecast version, the version chain stops meaning "what we thought" and
starts meaning "what we tried". Changing an assumption for real is
``revise_assumptions``, it needs a reason and evidence, and it is not in this
module.

The projection is append-only and content-hashed, so recomputing it against an
unchanged model produces the same bytes and is a ``duplicate`` rather than a
second opinion.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP, localcontext
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence

from .consensus_estimate import CONSENSUS_GAP_RULE_REF
from .model_forecast_driver import (
    ForecastModelUnavailable,
    STRUCTURED_SCHEMA_VERSION,
    STRUCTURED_CASH_SCHEMA_VERSION,
    compute_cash_flow_companion_results,
    is_structured_schema,
    _structure_historical_values,
    chain_base,
    company_slug,
    compute_results,
    compute_structure_results,
    quarterly_history,
    revenue_anchor,
)
from .store import DaltonStore, canonical_json, content_hash

SCHEMA_VERSION = "0.1"
PROJECTION_PREFIX = "sensitivity-projection:"
AUTOMATION_ACTOR = "automation:driver-model"
VALUE_KIND = "derived_deterministic"

# The write scope this projection needs. ``model_run`` is the existing word for
# "this automation may record what it computed by running the model", which is
# exactly what a what-if table is: a set of model runs nobody published.
WRITE_SCOPE = "model_run"

_SCHEMA_PATH = Path(__file__).with_name("forecast_sensitivity_schema.sql")

# Same quantisation as the model, for the same reason: the bytes that were
# hashed have to be the bytes that were computed, on any machine, forever.
_VALUE_QUANT = Decimal("0.00000001")
_RATE_QUANT = Decimal("0.000000000001")
_PERCENT_QUANT = Decimal("0.0001")
_PRECISION = 60

# -- the frozen selection rule ----------------------------------------------
#
# Written out rather than described, and hashed, because "the three drivers
# that matter most" is a claim about a company and the reader is entitled to
# know precisely what produced it.

#: One percentage point, in whatever the assumption's own measure is. Growth
#: assumptions and share assumptions are both ratios, so the move is
#: comparable across them without a second convention -- and it is a move a
#: person can hold in their head, unlike "one standard deviation", which needs
#: a distribution nobody agreed to.
UNIT_MOVE = Decimal("0.01")

#: Which line the ranking is measured on, most preferred first. The bottom-most
#: line this model actually computes, because a driver's importance is its
#: effect on what the company is worth, and a ranking taken on revenue would
#: put the top line first on every company by construction.
IMPACT_PRECEDENCE: tuple[tuple[str, str], ...] = (
    ("result:free_cash_flow", "free cash flow"),
    ("result:net_income", "net income"),
    ("result:operating_income", "operating income"),
    ("result:revenue", "revenue"),
)

#: How many drivers a projection names. The blueprint's three to five: fewer
#: than three is not a sensitivity, more than five is the model again.
MIN_DRIVERS = 3
MAX_DRIVERS = 5

#: The fewest filed observations a peak/trough/mean is allowed to rest on.
#: Three quarters is a list of three numbers; the word "peak" implies a
#: distribution, and four is the smallest window the model's own generator
#: already treats as one.
MIN_BAND_POINTS = 4

SCENARIOS: tuple[str, ...] = ("trough", "mean", "ours", "peak")

#: The four lines a what-if prints, in the order the chain computes them.
WHAT_IF_LINES: tuple[str, ...] = (
    "result:revenue", "result:operating_income", "result:net_income",
    "result:free_cash_flow",
)

SELECTION_RULE_REF = "rule:swing-rank:1"
BAND_RULE_REF = "rule:historical-band:2"

#: The rule itself, hashed into every projection. Changing any word of this
#: changes ``SELECTION_RULE_HASH``, which changes every projection's
#: ``content_hash`` -- so a projection computed under the old rule can never be
#: mistaken for one computed under the new one.
SELECTION_RULE: dict[str, Any] = {
    "ref": SELECTION_RULE_REF,
    "unit_move": format(UNIT_MOVE, "f"),
    "metric_precedence": [ref for ref, _ in IMPACT_PRECEDENCE],
    "horizon": "the forecast quarters this model has not yet had filed",
    "measure": "the absolute change in the metric summed over the horizon, "
               "with the driver's assumption held at one level in every quarter "
               "of that horizon",
    "held_flat": "every scenario and every elasticity holds one level across "
                 "all open quarters; none of them is a path. A driver whose "
                 "live assumption already differs quarter to quarter therefore "
                 "gets no elasticity, because there is no single level to add a "
                 "point to",
    "rank_by": "swing: the metric with this driver held flat at its historical "
               "peak, against the metric with it held flat at its historical "
               "trough",
    "rank_fallback": "a driver with no historical band is demoted, not excluded: "
                     "it ranks below every driver that has one, and among those "
                     "by its unit-move elasticity. A driver with neither a swing "
                     "nor an elasticity is the only one dropped",
    "rate_domain": "a historical tax-rate band containing a value outside [0, 1] "
                   "is retained as history but is unavailable as a forecast "
                   "scenario; it is never clamped or carried forward",
    "order": "largest swing first; ties broken by driver ref",
    "why_not_elasticity": "a one-point move ranks every share driver identically, "
                          "because each is a share of the same revenue and one "
                          "point of revenue is one number. What separates them is "
                          "how far each has actually moved, which is the band.",
    "min_drivers": MIN_DRIVERS,
    "max_drivers": MAX_DRIVERS,
    "excluded": "a driver with no live assumption over the horizon, and a "
                "driver whose move leaves the metric unavailable",
    "band_ref": BAND_RULE_REF,
    "consensus_gap_rule_ref": CONSENSUS_GAP_RULE_REF,
    "band_window": "every quarter of filed history the model input table holds",
    "band_min_points": MIN_BAND_POINTS,
    "band_statistics": ["trough", "mean", "peak", "latest"],
    "band_mean": "the arithmetic mean of the observations, quantised to 1e-12",
    "band_runner_up": "each extreme carries the next distinct observation and "
                      "its quarter, so a one-quarter extreme is visible without "
                      "an exclusion rule nobody agreed to",
    "scenarios": list(SCENARIOS),
    "lines": list(WHAT_IF_LINES),
    "note": "every scenario is recomputed in memory with the model's own "
            "formula and is never published as a forecast version",
}
SELECTION_RULE_HASH = content_hash(SELECTION_RULE)

_UNSET = object()

_RECORD_FIELDS = frozenset({
    "schema_version", "id", "created_at", "projection_ref", "version",
    "prior_projection_ref", "company_ref", "model_ref", "model_version_ref",
    "model_version_hash", "spec_ref", "spec_hash", "inputs_hash", "value_kind",
    "selection_rule_ref", "selection_rule_hash", "band_rule_ref",
    "formula_ref", "formula_hash", "unit", "currency", "history_window",
    "horizon", "impact_metric", "selection", "drivers", "consensus_bridge",
    "bridge_detail", "consensus_fingerprint", "fingerprint",
    "mission_version_ref", "actor_ref", "body_hash", "content_hash",
})
#: What a projection *is*, as opposed to when it was made and which mission
#: asked. Two projections with the same body are the same projection: a tick
#: that recomputes an unchanged model is a duplicate, not a second opinion.
_BODY_EXCLUDED = frozenset({
    "id", "created_at", "version", "prior_projection_ref",
    "mission_version_ref", "body_hash", "content_hash",
})


class SensitivityError(RuntimeError):
    """Base error for the sensitivity projection authority."""


class SensitivityValidationError(SensitivityError, ValueError):
    """A projection does not satisfy the closed contract."""


class SensitivityConflict(SensitivityError):
    """A projection conflicts with the immutable chain."""


class SensitivityNotFound(SensitivityError, LookupError):
    """No such projection."""


class SensitivityUnavailable(SensitivityError):
    """There is nothing here to be sensitive about, and why."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SensitivityValidationError(f"{name} must be non-empty text")
    return value.strip()


def _decimal(value: Any, name: str) -> Decimal:
    try:
        number = Decimal(str(value))
    except Exception as exc:  # noqa: BLE001
        raise SensitivityValidationError(f"{name} is not a number") from exc
    if not number.is_finite():
        raise SensitivityValidationError(f"{name} must be finite")
    return number


def _plain(value: Decimal) -> str:
    return format(value.quantize(_VALUE_QUANT, ROUND_HALF_UP), "f")


def _rate(value: Decimal) -> str:
    return format(value.quantize(_RATE_QUANT, ROUND_HALF_UP), "f")


def _percent(value: Decimal) -> str:
    return format(value.quantize(_PERCENT_QUANT, ROUND_HALF_UP), "f")


def _cell_ref(cell: Mapping[str, Any]) -> dict[str, Any]:
    """One filed cell, in the same ref shape the model already uses."""

    accessions = cell.get("accessions") or []
    return {
        "kind": "input_cell", "ref": None, "concept": str(cell["concept"]),
        "period_end": str(cell["period_end"]),
        "accession": str(accessions[0]) if accessions else None,
    }


# ---------------------------------------------------------------------------
# the historical band: what this measure has actually done
# ---------------------------------------------------------------------------


def _adjacent(prior_end: str, end: str) -> bool:
    from .model_forecast_driver import (
        QUARTER_GAP_MAX_DAYS, QUARTER_GAP_MIN_DAYS, _days,
    )

    return QUARTER_GAP_MIN_DAYS <= _days(prior_end, end) <= QUARTER_GAP_MAX_DAYS


def _operating_income_history(record: Mapping[str, Any]) -> list[dict[str, Any]]:
    from .model_forecast_driver import _operating_income_history as history

    drivers = list(record.get("drivers") or [])
    try:
        anchor = revenue_anchor(drivers)
    except ForecastModelUnavailable:
        return []
    return history(drivers, quarterly_history(anchor))


def measure_series(
    record: Mapping[str, Any], driver_ref: str, measure: str,
) -> dict[str, Any]:
    """Every filed observation of one driver's own measure, oldest first.

    The measure, not the level. A band on the level of an expense line says
    only that the company grew; the assumption being moved is the *share*, and
    a band that is not in the units of the thing being moved cannot be
    substituted into it. Growth is measured across adjacent quarters only, for
    the reason the model's generator already found: a history assembled from
    10-Qs has a hole where the fourth quarter should be, and two quarters of
    growth read as one is not a rate.
    """

    drivers = list(record.get("drivers") or [])
    driver = next((item for item in drivers if str(item.get("ref")) == driver_ref), None)
    if driver is None:
        return {"status": "unavailable", "measure": measure, "points": [],
                "reason": f"this model has no driver {driver_ref}"}
    cells = quarterly_history(driver)
    if not cells:
        return {"status": "unavailable", "measure": measure, "points": [],
                "reason": "this driver has no filed quarterly history"}
    points: list[dict[str, Any]] = []
    with localcontext() as ctx:
        ctx.prec = _PRECISION
        if measure == "quarterly_growth":
            for prior, current in zip(cells, cells[1:]):
                if not _adjacent(str(prior["period_end"]), str(current["period_end"])):
                    continue
                base = _decimal(prior["value"], "history value")
                if base == 0:
                    continue
                rate = (_decimal(current["value"], "history value") - base) / base
                points.append({
                    "period_end": str(current["period_end"]),
                    "value": _rate(rate),
                    "refs": [_cell_ref(prior), _cell_ref(current)],
                })
        elif measure in ("revenue_share", "operating_income_share", "share_of_line"):
            if measure == "share_of_line":
                structure = record.get("financial_statement_structure")
                line_ref = driver.get("structure_line_ref")
                line = next((item for item in (structure or {}).get("lines") or []
                             if item.get("ref") == line_ref), None)
                if (line is None
                        and record.get("schema_version") == STRUCTURED_CASH_SCHEMA_VERSION
                        and driver.get("kind") == "cash_flow"):
                    companion = record.get("cash_flow_companion") or {}
                    source = next(
                        (item for item in companion.get("lines") or []
                         if item.get("ref") == line_ref), None,
                    )
                    if source is not None:
                        line = {"forecast_base_ref": source.get("forecast_base_ref")}
                base_ref = None if line is None else line.get("forecast_base_ref")
                if not isinstance(base_ref, str):
                    return {"status": "unavailable", "measure": measure, "points": [],
                            "reason": "the structure names no exact base for this share"}
                historical, historical_refs = _structure_historical_values(
                    drivers, structure)
                base_values = historical.get(base_ref, {})
                signs: set[bool] = set()
                for cell in cells:
                    end = str(cell["period_end"])
                    divisor = base_values.get(end)
                    if divisor is None or divisor == 0:
                        continue
                    signs.add(divisor > 0)
                    points.append({
                        "period_end": end,
                        "value": _rate(_decimal(cell["value"], "history value") / divisor),
                        "refs": ([_cell_ref(cell)]
                                 + list(historical_refs.get(base_ref, {}).get(end, []))),
                    })
                if len(signs) > 1:
                    return {"status": "unavailable", "measure": measure, "points": [],
                            "reason": "the base of this share changes sign inside the "
                                      "window, so its extremes describe the base rather "
                                      "than the share"}
                base_cells = None
            elif measure == "revenue_share":
                try:
                    base_cells = quarterly_history(revenue_anchor(drivers))
                except ForecastModelUnavailable as exc:
                    return {"status": "unavailable", "measure": measure, "points": [],
                            "reason": str(exc)}
            else:
                base_cells = _operating_income_history(record)
            if measure != "share_of_line" and not base_cells:
                return {"status": "unavailable", "measure": measure, "points": [],
                        "reason": "the base line of this share has no filed history"}
            if measure != "share_of_line":
                by_period = {str(item["period_end"]): item for item in base_cells}
                signs = set()
                for cell in cells:
                    base = by_period.get(str(cell["period_end"]))
                    if base is None:
                        continue
                    divisor = _decimal(base["value"], "history value")
                    if divisor == 0:
                        continue
                    signs.add(divisor > 0)
                    share = _decimal(cell["value"], "history value") / divisor
                    points.append({
                        "period_end": str(cell["period_end"]),
                        "value": _rate(share),
                        "refs": [_cell_ref(cell), _cell_ref(base)],
                    })
                if len(signs) > 1:
                    # A ratio whose base crosses zero has no comparable peak.
                    return {"status": "unavailable", "measure": measure, "points": [],
                            "reason": "the base of this share changes sign inside the "
                                      "window, so its extremes describe the base rather "
                                      "than the share"}
        else:
            return {"status": "unavailable", "measure": measure, "points": [],
                    "reason": f"no historical series is defined for the measure {measure}"}
    if not points:
        return {"status": "unavailable", "measure": measure, "points": [],
                "reason": "no pair of filed quarters could be compared"}
    return {"status": "available", "measure": measure, "reason": None, "points": points}


def historical_band(series: Mapping[str, Any]) -> dict[str, Any]:
    """Peak, trough, mean and latest, each with the quarter and the accession.

    ``latest`` is beside the extremes on purpose. A band alone invites the
    reader to place our estimate inside it; where the measure *most recently
    was* is the number our estimate has to be argued against, and it is often
    at one end.

    Each extreme also carries its **runner-up**: the next most extreme
    observation, with its quarter. Cognizant's tax share peaks at 70.79% in one
    quarter and the next highest is far below it -- that peak is an event, not
    a rate, and a scenario run at it is a scenario about a one-off charge.
    Deciding *for* the reader which extremes are outliers would need an
    exclusion rule, and an exclusion rule with no principle behind it is
    picking numbers; showing the second one costs nothing and lets a person see
    the gap. ``None`` when every observation is the same value, because then
    there is no second one to show.
    """

    empty = {
        "status": "unavailable", "measure": series.get("measure"), "unit": "ratio",
        "count": 0, "first_period": None, "last_period": None,
        "peak": None, "trough": None, "mean": None, "latest": None,
    }
    if series.get("status") != "available":
        return {**empty, "reason": str(series.get("reason") or "no series")}
    points = list(series.get("points") or [])
    if len(points) < MIN_BAND_POINTS:
        return {**empty, "count": len(points),
                "reason": f"only {len(points)} filed observations; a peak, a trough "
                          f"and a mean need at least {MIN_BAND_POINTS}"}
    with localcontext() as ctx:
        ctx.prec = _PRECISION
        values = [(_decimal(item["value"], "band value"), item) for item in points]
        # The first quarter that reached the extreme, not an arbitrary one:
        # two identical peaks must not be attributed to different quarters on
        # two machines, and ``max`` over tuples would order by the ref next.
        best = max(item[0] for item in values)
        worst = min(item[0] for item in values)
        peak = next(item for item in values if item[0] == best)
        trough = next(item for item in values if item[0] == worst)
        # The runner-up is the first quarter at the next distinct level, by the
        # same rule as the extreme itself: distinct so that two quarters tied at
        # the peak do not report each other and hide a real gap, first so that
        # two machines agree on the quarter.
        below = [item for item in values if item[0] < best]
        above = [item for item in values if item[0] > worst]
        runner_peak = (next(item for item in values
                            if item[0] == max(pair[0] for pair in below))
                       if below else None)
        runner_trough = (next(item for item in values
                              if item[0] == min(pair[0] for pair in above))
                         if above else None)
        total = sum((item[0] for item in values), Decimal(0))
        mean = total / Decimal(len(values))
        latest = values[-1]
    refs: list[dict[str, Any]] = []
    seen: set[tuple[Any, ...]] = set()
    for _, item in values:
        for ref in item["refs"]:
            key = (ref["concept"], ref["period_end"])
            if key not in seen:
                seen.add(key)
                refs.append(ref)
    return {
        "status": "available", "reason": None,
        "measure": series.get("measure"), "unit": "ratio", "count": len(values),
        "first_period": str(points[0]["period_end"]),
        "last_period": str(points[-1]["period_end"]),
        "peak": {"value": _rate(peak[0]), "period_end": peak[1]["period_end"],
                 "refs": list(peak[1]["refs"]),
                 "runner_up": None if runner_peak is None else {
                     "value": _rate(runner_peak[0]),
                     "period_end": runner_peak[1]["period_end"]}},
        "trough": {"value": _rate(trough[0]), "period_end": trough[1]["period_end"],
                   "refs": list(trough[1]["refs"]),
                   "runner_up": None if runner_trough is None else {
                       "value": _rate(runner_trough[0]),
                       "period_end": runner_trough[1]["period_end"]}},
        "mean": {"value": _rate(mean), "period_end": None, "refs": refs},
        "latest": {"value": _rate(latest[0]), "period_end": latest[1]["period_end"],
                   "refs": list(latest[1]["refs"])},
    }


# ---------------------------------------------------------------------------
# the engine: one what-if run, in memory
# ---------------------------------------------------------------------------


def open_periods(record: Mapping[str, Any]) -> list[dict[str, str]]:
    """The forecast quarters the filings have not yet answered.

    Sensitivity is about what is still ahead. A quarter this model estimated
    and the company has since reported is settled: moving an assumption over it
    would produce a scenario for a number everyone can already look up.

    Which is already true of ``forecast_periods`` and does not need filtering
    here. ``actualize_model`` moves a settled quarter out of that list and into
    ``realised_periods``, and ``validate_forecast_model`` refuses a record where
    a quarter is in both. A filter against ``realised_periods`` would therefore
    only ever fire on a record the authority would not have stored -- which is
    worse than useless: it would make this function *appear* covered by a test
    built on a shape that cannot exist, and quietly accept a malformed record
    instead of letting it fail.
    """

    return [dict(item) for item in (record.get("forecast_periods") or [])]


def live_assumptions(record: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The assumptions the model currently stands on, superseded ones dropped."""

    ends = {str(item["end"]) for item in (record.get("forecast_periods") or [])}
    return [dict(item) for item in (record.get("assumptions") or [])
            if item.get("kind") != "actual" and not item.get("superseded_by")
            and str(item["period"]["end"]) in ends]


def recompute(
    record: Mapping[str, Any], driver_ref: str | None, value: Decimal | None,
) -> tuple[list[dict[str, Any]], list[str]]:
    """The model's own arithmetic, with one driver's assumption replaced.

    Deliberately the same three calls ``revise_assumptions`` makes -- the same
    ``chain_base``, the same ``compute_results``, the same period list -- so a
    what-if and a revision of the same size produce the same numbers. If they
    did not, the table would be describing a model nobody could publish.

    Nothing is written. The result is a list of result lines and the refs of
    the assumptions that were replaced to get them.
    """

    drivers = list(record.get("drivers") or [])
    periods = [dict(item) for item in (record.get("forecast_periods") or [])]
    if not periods:
        raise SensitivityUnavailable("this model forecasts no quarters")
    assumptions: list[dict[str, Any]] = []
    replaced: list[str] = []
    for item in live_assumptions(record):
        if driver_ref is not None and value is not None \
                and str(item["driver_ref"]) == driver_ref:
            replaced.append(str(item["ref"]))
            item = {**item, "value": _rate(value)}
        assumptions.append(item)
    if is_structured_schema(record.get("schema_version")):
        structure = record.get("financial_statement_structure")
        if not isinstance(structure, Mapping):
            raise SensitivityUnavailable("structured model has no statement authority")
        income_drivers = [item for item in drivers if item.get("kind") != "cash_flow"]
        results = compute_structure_results(
            income_drivers, assumptions, periods, structure)
        if record.get("schema_version") == STRUCTURED_CASH_SCHEMA_VERSION:
            companion = record.get("cash_flow_companion")
            if not isinstance(companion, Mapping):
                raise SensitivityUnavailable("structured cash model has no companion authority")
            results.extend(compute_cash_flow_companion_results(
                [item for item in drivers if item.get("kind") == "cash_flow"],
                assumptions, periods, companion, results))
    else:
        base = chain_base(revenue_anchor(drivers), record, str(periods[0]["end"]))
        results = compute_results(
            drivers, assumptions, periods,
            statements=record.get("statements") or {}, base=base)
    return results, sorted(set(replaced))


def _line(results: Sequence[Mapping[str, Any]], ref: str) -> Mapping[str, Any] | None:
    return next((item for item in results if str(item["ref"]) == ref), None)


def horizon_total(
    results: Sequence[Mapping[str, Any]], line_ref: str, ends: Sequence[str],
) -> dict[str, Any]:
    """One line summed over the open quarters, or the reason it is not a number.

    Partial is refused rather than summed. A total over the three quarters that
    computed, printed beside a total over four, is a comparison of two
    different things -- and the difference would be read as sensitivity.
    """

    line = _line(results, line_ref)
    if line is None:
        return {"status": "unavailable", "value": None,
                "reason": f"this model has no {line_ref}"}
    cells = {str(cell["period"]["end"]): cell for cell in (line.get("cells") or [])
             if cell.get("kind") == "estimate"}
    missing = [end for end in ends
               if cells.get(end) is None or cells[end].get("status") != "computed"]
    if missing:
        reason = next(
            (cells[end].get("reason") for end in missing
             if cells.get(end) is not None and cells[end].get("reason")),
            f"{len(missing)} of {len(ends)} quarters did not compute")
        return {"status": "unavailable", "value": None, "reason": str(reason)}
    with localcontext() as ctx:
        ctx.prec = _PRECISION
        total = sum((_decimal(cells[end]["value"], "cell value") for end in ends),
                    Decimal(0))
    return {"status": "computed", "value": _plain(total), "reason": None}


def impact_metric(record: Mapping[str, Any]) -> dict[str, Any]:
    """The line the ranking is measured on: the bottom-most one that computes.

    Precedence rather than a choice, so two companies with different chains are
    still ranked by a rule, not by whichever line happened to be available.
    """

    ends = [str(item["end"]) for item in open_periods(record)]
    if not ends:
        return {"status": "unavailable", "result_ref": None, "label": None,
                "reason": "every quarter this model forecasts has been filed"}
    try:
        results, _ = recompute(record, None, None)
    except (SensitivityUnavailable, ForecastModelUnavailable) as exc:
        return {"status": "unavailable", "result_ref": None, "label": None,
                "reason": str(exc)}
    tried: list[str] = []
    for ref, label in IMPACT_PRECEDENCE:
        total = horizon_total(results, ref, ends)
        if total["status"] == "computed":
            return {"status": "available", "result_ref": ref, "label": label,
                    "base_total": total["value"], "reason": None}
        tried.append(f"{label}: {total['reason']}")
    return {"status": "unavailable", "result_ref": None, "label": None,
            "reason": "no line of this model computes over the whole horizon "
                      f"({'; '.join(tried)})"}


def driver_impact(
    record: Mapping[str, Any], driver_ref: str, metric: Mapping[str, Any],
) -> dict[str, Any]:
    """How far the metric moves when this driver's assumption moves one unit.

    Both directions are not run. The chain is linear in every assumption except
    the revenue growth rate, which compounds, and for a one-point move the
    asymmetry is far smaller than the precision anyone reads this at; running
    one direction and saying which one keeps the table honest about what was
    computed rather than implying a symmetry that was never checked.
    """

    ends = [str(item["end"]) for item in open_periods(record)]
    base_value = metric.get("base_total")
    if metric.get("status") != "available" or base_value is None:
        return {"status": "unavailable", "reason": str(metric.get("reason")),
                "metric_ref": None, "unit_move": _rate(UNIT_MOVE),
                "base_total": None, "moved_total": None, "delta": None,
                "delta_per_unit": None, "percent_of_base": None}
    live = [item for item in live_assumptions(record)
            if str(item["driver_ref"]) == driver_ref
            and str(item["period"]["end"]) in set(ends)]
    if not live:
        return {"status": "unavailable",
                "reason": "this driver carries no assumption over the open horizon",
                "metric_ref": metric["result_ref"], "unit_move": _rate(UNIT_MOVE),
                "base_total": base_value, "moved_total": None, "delta": None,
                "delta_per_unit": None, "percent_of_base": None}
    # An elasticity is "the answer moves this much when *this* assumption moves
    # one point". A scenario column holds one level flat across the horizon,
    # which is fine for a scenario -- it is a stated hypothetical -- but a
    # driver whose live assumption differs quarter to quarter has no single
    # level to add a point to. Taking the first quarter's would flatten the
    # other quarters onto it and report the flattening as elasticity: on a
    # driver revised for one quarter only, most of the "delta" would be the
    # revision being undone. The same guard the ``ours`` column already makes.
    values = {str(item["value"]) for item in live}
    if len(values) > 1:
        return {"status": "unavailable",
                "reason": "this driver's assumption is not one number across the "
                          "horizon, so there is no single level a unit move could "
                          "be added to",
                "metric_ref": metric["result_ref"], "unit_move": _rate(UNIT_MOVE),
                "base_total": base_value, "moved_total": None, "delta": None,
                "delta_per_unit": None, "percent_of_base": None}
    with localcontext() as ctx:
        ctx.prec = _PRECISION
        moved_value = _decimal(live[0]["value"], "assumption value") + UNIT_MOVE
    results, replaced = recompute(record, driver_ref, moved_value)
    moved = horizon_total(results, metric["result_ref"], ends)
    if moved["status"] != "computed":
        return {"status": "unavailable",
                "reason": f"moving this driver leaves the metric unavailable: "
                          f"{moved['reason']}",
                "metric_ref": metric["result_ref"], "unit_move": _rate(UNIT_MOVE),
                "base_total": base_value, "moved_total": None, "delta": None,
                "delta_per_unit": None, "percent_of_base": None}
    with localcontext() as ctx:
        ctx.prec = _PRECISION
        base = _decimal(base_value, "base total")
        delta = _decimal(moved["value"], "moved total") - base
        per_unit = delta / UNIT_MOVE
        percent = (delta / abs(base) * Decimal(100)) if base != 0 else None
    return {
        "status": "computed", "reason": None,
        "metric_ref": metric["result_ref"],
        "unit_move": _rate(UNIT_MOVE),
        "moved_from": _rate(_decimal(live[0]["value"], "assumption value")),
        "moved_to": _rate(moved_value),
        "replaced_assumption_refs": replaced,
        "base_total": _plain(base), "moved_total": moved["value"],
        "delta": _plain(delta), "delta_per_unit": _plain(per_unit),
        "percent_of_base": None if percent is None else _percent(percent),
    }


def driver_swing(
    record: Mapping[str, Any], driver_ref: str, band: Mapping[str, Any],
    metric: Mapping[str, Any],
) -> dict[str, Any]:
    """The metric at this driver's historical peak against its historical trough.

    This is what ranks the drivers, and the reason is arithmetic rather than
    taste. Every expense assumption in this model is a share of the same
    forecast revenue, so moving any of them by one percentage point moves
    operating income by one percent of revenue -- *the same number for all of
    them*, to the last digit. A ranking by unit elasticity therefore ranks
    cost of revenue, SG&A and depreciation identically and then breaks the tie
    on rounding noise, which is a ranking of nothing.

    What actually differs between those drivers is how far each one has moved
    in the company's own history: on Cognizant, SG&A has ranged over four
    percentage points of revenue and depreciation over a quarter of one. A
    driver's importance is its elasticity times its range, and the range is the
    part the filings can supply.
    """

    ends = [str(item["end"]) for item in open_periods(record)]
    if metric.get("status") != "available":
        return {"status": "unavailable", "reason": str(metric.get("reason")),
                "trough_total": None, "peak_total": None, "swing": None,
                "percent_of_base": None}
    if band.get("status") != "available":
        return {"status": "unavailable",
                "reason": str(band.get("reason") or "no historical band"),
                "trough_total": None, "peak_total": None, "swing": None,
                "percent_of_base": None}
    totals: dict[str, dict[str, Any]] = {}
    for edge in ("trough", "peak"):
        results, _ = recompute(
            record, driver_ref, _decimal(band[edge]["value"], f"band.{edge}"))
        totals[edge] = horizon_total(results, metric["result_ref"], ends)
        if totals[edge]["status"] != "computed":
            return {"status": "unavailable",
                    "reason": f"the metric is unavailable at the historical "
                              f"{edge}: {totals[edge]['reason']}",
                    "trough_total": None, "peak_total": None, "swing": None,
                    "percent_of_base": None}
    with localcontext() as ctx:
        ctx.prec = _PRECISION
        low = _decimal(totals["trough"]["value"], "trough total")
        high = _decimal(totals["peak"]["value"], "peak total")
        base = _decimal(metric["base_total"], "base total")
        swing = abs(high - low)
        percent = (swing / abs(base) * Decimal(100)) if base != 0 else None
    return {
        "status": "computed", "reason": None,
        "metric_ref": metric["result_ref"],
        "trough_total": totals["trough"]["value"],
        "peak_total": totals["peak"]["value"],
        "swing": _plain(swing),
        "percent_of_base": None if percent is None else _percent(percent),
    }


def _measure_of(record: Mapping[str, Any], driver_ref: str) -> str | None:
    for item in live_assumptions(record):
        if str(item["driver_ref"]) == driver_ref:
            return str(item["measure"])
    return None


def _scenario_band(
    driver: Mapping[str, Any], measure: str | None, band: Mapping[str, Any],
) -> dict[str, Any]:
    """A historical band only when each extreme can be used as an assumption.

    A tax benefit is a real filed observation, so it stays in
    :func:`measure_series`.  It is not a tax *rate* that can be held flat over
    the forecast horizon.  Mark the entire band unavailable when any summary
    point falls outside the model's closed tax-rate domain.  Dropping just the
    offending quarter would manufacture a narrower history; clamping it would
    manufacture a scenario.
    """

    wire = dict(band)
    if (wire.get("status") != "available"
            or driver.get("role") != "income_tax_expense"
            or measure != "operating_income_share"):
        return wire
    values = []
    for name in ("trough", "mean", "peak"):
        point = wire.get(name)
        if isinstance(point, Mapping) and point.get("value") is not None:
            values.append(_decimal(point["value"], f"band.{name}"))
    if values and all(Decimal(0) <= value <= Decimal(1) for value in values):
        return wire
    return {
        "status": "unavailable",
        "measure": wire.get("measure"),
        "count": wire.get("count", 0),
        "reason": "the filed tax-rate history contains a value outside [0, 1]; "
                  "a tax benefit or charge above profit cannot be held flat as "
                  "a forecast tax rate",
        "peak": None,
        "trough": None,
        "mean": None,
        "latest": None,
    }


def candidate_drivers(record: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Every driver that carries a live assumption, in the model's own order."""

    order = {str(item["ref"]): index
             for index, item in enumerate(record.get("drivers") or [])}
    refs = sorted({str(item["driver_ref"]) for item in live_assumptions(record)},
                  key=lambda ref: (order.get(ref, len(order)), ref))
    out = []
    for ref in refs:
        driver = next((item for item in (record.get("drivers") or [])
                       if str(item["ref"]) == ref), None)
        if driver is not None:
            out.append(dict(driver))
    return out


def select_drivers(record: Mapping[str, Any]) -> dict[str, Any]:
    """The three to five drivers that move the answer most, and why those.

    Refused whole when no line of the model computes: a ranking of drivers by
    their effect on a number that does not exist would still print an order,
    and an order is the one thing a reader takes away.
    """

    metric = impact_metric(record)
    considered = candidate_drivers(record)
    if metric.get("status") != "available":
        return {"metric": metric, "selection": {
            "status": "unavailable", "reason": str(metric.get("reason")),
            "considered": len(considered), "selected": 0}, "ranked": []}
    ranked: list[dict[str, Any]] = []
    skipped: list[str] = []
    for driver in considered:
        ref = str(driver["ref"])
        measure = _measure_of(record, ref)
        band = _scenario_band(
            driver, measure,
            historical_band(measure_series(record, ref, str(measure))),
        )
        impact = driver_impact(record, ref, metric)
        swing = driver_swing(record, ref, band, metric)
        # Dropped only when neither number exists. The two are independently
        # unavailable for independent reasons -- a driver with too little
        # history has no swing, a driver revised for one quarter has no single
        # level to take an elasticity at -- and either one is enough to place
        # it. A driver dropped for want of one of them would be a driver the
        # reader is told nothing about, which reads as a driver that does not
        # matter.
        if impact["status"] != "computed" and swing["status"] != "computed":
            skipped.append(f"{ref}: {swing['reason'] or impact['reason']}")
            continue
        ranked.append({"driver": driver, "impact": impact, "measure": measure,
                       "band": band, "swing": swing})
    # Two classes, never mixed on one scale. A driver whose band could not be
    # read is not a driver that moves little; it is a driver we cannot say how
    # far moves, and putting a number on it to sort it beside the others would
    # be inventing the very range this slice exists to source from filings. So
    # it is *demoted* -- ranked below every driver that has a swing, and among
    # those by elasticity -- rather than excluded.
    ranked.sort(key=lambda item: (
        0 if item["swing"]["status"] == "computed" else 1,
        -abs(_decimal(item["swing"]["swing"], "swing"))
        if item["swing"]["status"] == "computed"
        else (-abs(_decimal(item["impact"]["delta"], "delta"))
              if item["impact"]["status"] == "computed" else Decimal(0)),
        str(item["driver"]["ref"]),
    ))
    chosen = ranked[:MAX_DRIVERS]
    if not chosen:
        status, reason = "unavailable", (
            "no driver of this model moves a line that computes"
            + (f" ({'; '.join(skipped)})" if skipped else ""))
    elif len(chosen) < MIN_DRIVERS:
        status, reason = "partial", (
            f"only {len(chosen)} of this model's drivers move a line that "
            f"computes; the rule asks for {MIN_DRIVERS}"
            + (f" ({'; '.join(skipped)})" if skipped else ""))
    else:
        status, reason = "available", None
    return {"metric": metric, "ranked": chosen, "selection": {
        "status": status, "reason": reason,
        "considered": len(considered), "selected": len(chosen),
        "skipped": skipped}}


# ---------------------------------------------------------------------------
# the what-if table
# ---------------------------------------------------------------------------


def _our_value(record: Mapping[str, Any], driver_ref: str,
               ends: Sequence[str]) -> dict[str, Any]:
    live = [item for item in live_assumptions(record)
            if str(item["driver_ref"]) == driver_ref
            and str(item["period"]["end"]) in set(ends)]
    values = {str(item["value"]) for item in live}
    refs: list[dict[str, Any]] = []
    seen: set[tuple[Any, ...]] = set()
    for item in live:
        for ref in item.get("refs") or []:
            key = (ref.get("concept"), ref.get("period_end"), ref.get("ref"))
            if key not in seen:
                seen.add(key)
                refs.append(dict(ref))
    return {
        "value": values.pop() if len(values) == 1 else None,
        "varies": len(values) > 1,
        "assumption_refs": sorted(str(item["ref"]) for item in live),
        "because": (live[0].get("because") if len(
            {str(item.get("because")) for item in live}) == 1 else None),
        "refs": refs,
    }


def scenario_lines(
    record: Mapping[str, Any], results: Sequence[Mapping[str, Any]],
    ends: Sequence[str],
) -> list[dict[str, Any]]:
    """Revenue, operating income, net income and free cash flow, per quarter."""

    out = []
    for ref in WHAT_IF_LINES:
        line = _line(results, ref)
        if line is None:
            out.append({"ref": ref, "label": None, "status": "unavailable",
                        "reason": f"this model has no {ref}", "total": None,
                        "cells": []})
            continue
        cells = {str(cell["period"]["end"]): cell for cell in (line.get("cells") or [])
                 if cell.get("kind") == "estimate"}
        total = horizon_total(results, ref, ends)
        out.append({
            "ref": ref, "label": line.get("label"),
            "status": total["status"], "reason": total["reason"],
            "total": total["value"],
            "cells": [{
                "period_end": end,
                "status": (cells[end].get("status") if end in cells else "unavailable"),
                "value": (cells[end].get("value") if end in cells else None),
                "reason": (cells[end].get("reason") if end in cells
                           else "this quarter is not in the recomputed chain"),
            } for end in ends],
        })
    return out


def what_if(
    record: Mapping[str, Any], driver_ref: str, band: Mapping[str, Any],
    ours: Mapping[str, Any], ends: Sequence[str],
) -> list[dict[str, Any]]:
    """One row per scenario, each tracing to the assumption it replaced.

    ``ours`` is run through the same path as the other three with nothing
    replaced, rather than copied out of the stored record. If the recomputation
    of our own estimate did not reproduce the model, every other column would
    be measured against a baseline that is not the model -- and the difference
    would be invisible, because the number printed would still be ours.

    **Every column holds one level flat across every open quarter**, which each
    row states (``held_flat``, ``quarters_held``). None of them is a path. "The
    trough" is not what would happen if the trough quarter repeated once; it is
    what the model says if this driver sat at that level for the whole horizon,
    which is a harsher and more useful question, and one the reader has to know
    is being asked.
    """

    rows: list[dict[str, Any]] = []
    for scenario in SCENARIOS:
        if scenario == "ours":
            value = None
            source_refs = list(ours.get("refs") or [])
            shown = ours.get("value")
            period = None
            if ours.get("value") is None:
                rows.append({
                    "scenario": scenario, "status": "unavailable",
                    "reason": "this driver's assumption is not one number across "
                              "the horizon, so it has no single column here",
                    "assumption_value": None, "from_period": None,
                    "held_flat": False, "quarters_held": 0,
                    "replaced_assumption_refs": [], "input_refs": source_refs,
                    "lines": [],
                })
                continue
        else:
            if band.get("status") != "available":
                rows.append({
                    "scenario": scenario, "status": "unavailable",
                    "reason": str(band.get("reason") or "no historical band"),
                    "assumption_value": None, "from_period": None,
                    "held_flat": False, "quarters_held": 0,
                    "replaced_assumption_refs": [], "input_refs": [], "lines": [],
                })
                continue
            point = band[scenario]
            value = _decimal(point["value"], f"band.{scenario}")
            shown = point["value"]
            period = point.get("period_end")
            source_refs = list(point.get("refs") or [])
        results, replaced = recompute(record, driver_ref, value)
        rows.append({
            "scenario": scenario, "status": "computed", "reason": None,
            "assumption_value": shown, "from_period": period,
            # True for ``ours`` as well: our own assumption is one number across
            # the horizon whenever this row computes at all, so that column is
            # a flat hold too, and saying so keeps the four columns comparable.
            "held_flat": True, "quarters_held": len(ends),
            "replaced_assumption_refs": replaced,
            "input_refs": source_refs,
            "lines": scenario_lines(record, results, ends),
        })
    return rows


# ---------------------------------------------------------------------------
# the projection
# ---------------------------------------------------------------------------


def fingerprint(record: Mapping[str, Any], consensus_fingerprint: str | None) -> str:
    """What a projection is *of*: this model version and this street.

    The lane compares it against the last projection's. A model that has not
    been revised and a street that has not moved is a company with nothing new
    to say, and recomputing it every tick would bury the ones that do.
    """

    return content_hash({
        "model_version_hash": str(record.get("content_hash")),
        "consensus_fingerprint": consensus_fingerprint,
        "selection_rule_hash": SELECTION_RULE_HASH,
        "schema_version": SCHEMA_VERSION,
    })


def build_projection(
    record: Mapping[str, Any],
    *,
    bridge: Mapping[str, Any] | None = None,
    bridge_detail: Sequence[Mapping[str, Any]] | None = None,
    consensus_fingerprint: str | None = None,
    actor_ref: str = AUTOMATION_ACTOR,
    mission_version_ref: str | None = None,
) -> dict[str, Any]:
    """One company's sensitivity table, computed entirely in memory.

    Raises ``SensitivityUnavailable`` when there is nothing to be sensitive
    about -- no open quarter, or no line that computes -- because a projection
    with an empty driver list is not a projection with a gap in it, it is a
    claim that nothing matters.
    """

    company_ref = _text(record.get("company_ref"), "company_ref")
    periods = open_periods(record)
    if not periods:
        raise SensitivityUnavailable(
            "every quarter this model forecasts has already been filed")
    ends = [str(item["end"]) for item in periods]
    picked = select_drivers(record)
    if picked["selection"]["status"] == "unavailable":
        raise SensitivityUnavailable(str(picked["selection"]["reason"]))

    drivers: list[dict[str, Any]] = []
    for rank, item in enumerate(picked["ranked"], start=1):
        driver = item["driver"]
        ref = str(driver["ref"])
        measure = str(item["measure"])
        band = item["band"]
        ours = _our_value(record, ref, ends)
        drivers.append({
            "rank": rank,
            "driver_ref": ref,
            "label": driver.get("label"),
            "concept": driver.get("concept"),
            "role": driver.get("role"),
            "statement": driver.get("statement"),
            "measure": measure,
            "unit": "ratio",
            "ours": ours,
            "impact": item["impact"],
            "swing": item["swing"],
            "band": band,
            "what_if": what_if(record, ref, band, ours, ends),
        })

    history = [str(item) for item in (record.get("history_periods") or [])]
    body = {
        "schema_version": SCHEMA_VERSION,
        "projection_ref": f"{PROJECTION_PREFIX}{company_ref}",
        "company_ref": company_ref,
        "model_ref": str(record.get("model_ref")),
        "model_version_ref": str(record.get("id")),
        "model_version_hash": str(record.get("content_hash")),
        "spec_ref": str(record.get("spec_ref")),
        "spec_hash": str(record.get("spec_hash")),
        "inputs_hash": str(record.get("inputs_hash")),
        "value_kind": VALUE_KIND,
        "selection_rule_ref": SELECTION_RULE_REF,
        "selection_rule_hash": SELECTION_RULE_HASH,
        "band_rule_ref": BAND_RULE_REF,
        "formula_ref": str(record.get("formula_ref")),
        "formula_hash": str(record.get("formula_hash")),
        "unit": str(record.get("unit") or "usd"),
        "currency": str(record.get("currency") or "USD"),
        "history_window": {
            "first": history[0] if history else None,
            "last": history[-1] if history else None,
            "quarters": len(history),
        },
        "horizon": [dict(item) for item in periods],
        "impact_metric": picked["metric"],
        "selection": picked["selection"],
        "drivers": drivers,
        "consensus_bridge": dict(bridge) if bridge is not None else {
            "status": "unavailable", "metrics": [],
            "reason": "no consensus was read for this projection"},
        "bridge_detail": [dict(item) for item in (bridge_detail or [])],
        "consensus_fingerprint": consensus_fingerprint,
        "fingerprint": fingerprint(record, consensus_fingerprint),
        "actor_ref": actor_ref,
        "mission_version_ref": mission_version_ref,
    }
    return body


def projection_readiness(record: Mapping[str, Any]) -> dict[str, Any]:
    """What this projection has and what it is missing, counted plainly.

    No score, for the reason the input table gives: "eighty percent" invites a
    reader to accept a table in which the driver that matters has no band.
    """

    drivers = list(record.get("drivers") or [])
    bands = [item for item in drivers
             if (item.get("band") or {}).get("status") == "available"]
    cells = 0
    unavailable_cells = 0
    for item in drivers:
        for row in item.get("what_if") or []:
            for line in row.get("lines") or []:
                for cell in line.get("cells") or []:
                    cells += 1
                    if cell.get("status") != "computed":
                        unavailable_cells += 1
    bridge = record.get("consensus_bridge") or {}
    return {
        "drivers_selected": len(drivers),
        "drivers_with_bands": len(bands),
        "drivers_without_bands": [
            str(item["driver_ref"]) for item in drivers
            if (item.get("band") or {}).get("status") != "available"],
        "selection_status": (record.get("selection") or {}).get("status"),
        "impact_metric": (record.get("impact_metric") or {}).get("result_ref"),
        "horizon_quarters": len(record.get("horizon") or []),
        "history_quarters": (record.get("history_window") or {}).get("quarters"),
        "what_if_cells": cells,
        "what_if_cells_unavailable": unavailable_cells,
        "bridge_status": bridge.get("status"),
        "bridge_metrics": len(bridge.get("metrics") or []),
    }


# ---------------------------------------------------------------------------
# validation and the authority
# ---------------------------------------------------------------------------


def validate_projection(value: Mapping[str, Any]) -> dict[str, Any]:
    """A closed shape, and the two invariants worth a refusal.

    Everything else is arithmetic this module produced, and re-checking it here
    would only assert that the same code ran twice. The two that matter are the
    ones a caller could get wrong: a projection must name the model version it
    was computed from, and a bridge must be in the shape the conviction call
    validates -- because a bridge nobody downstream can read is a bridge that
    will be discovered as broken after a model call has been paid for.
    """

    if not isinstance(value, Mapping):
        raise SensitivityValidationError("a projection must be an object")
    wire = dict(value)
    unknown = sorted(set(wire) - _RECORD_FIELDS)
    missing = sorted(_RECORD_FIELDS - set(wire))
    if unknown or missing:
        raise SensitivityValidationError(
            f"projection has an invalid closed shape; missing={missing}, "
            f"unknown={unknown}")
    if str(wire.get("value_kind")) != VALUE_KIND:
        raise SensitivityValidationError(
            f"a sensitivity projection is {VALUE_KIND}")
    for field in ("company_ref", "model_version_ref", "model_version_hash",
                  "inputs_hash", "selection_rule_ref", "selection_rule_hash",
                  "fingerprint"):
        _text(wire.get(field), field)
    if not str(wire["model_version_ref"]).startswith("forecast-model-version:"):
        raise SensitivityValidationError(
            "model_version_ref must name a forecast model version; a projection "
            "that cannot be traced to the model it was computed from is a "
            "table of numbers with no provenance")
    from .consensus_bridge import validate_bridge

    wire["consensus_bridge"] = validate_bridge(wire["consensus_bridge"])
    return wire


def body_hash(body: Mapping[str, Any]) -> str:
    return content_hash({key: value for key, value in body.items()
                         if key not in _BODY_EXCLUDED})


class SensitivityProjectionAuthority:
    """Append-only SensitivityProjections, one chain per company."""

    def __init__(self, store: DaltonStore):
        self.store = store
        self.connection = store.connection
        self._authorized = False
        self.connection.create_function(
            "dalton_sensitivity_projection_authorized", 0,
            lambda: int(self._authorized))
        self.connection.executescript(_SCHEMA_PATH.read_text(encoding="utf-8"))

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Cursor]:
        if self._authorized:
            raise RuntimeError(
                "SensitivityProjectionAuthority operation cannot be nested")
        self._authorized = True
        try:
            with self.store._transaction() as cur:
                yield cur
        finally:
            self._authorized = False

    def publish(
        self,
        body: Mapping[str, Any],
        *,
        statement_rows: Sequence[Mapping[str, Any]] = (),
        solver_results: Sequence[Mapping[str, Any]] = (),
    ) -> dict[str, Any]:
        """Store one projection, or say it is the one already stored.

        A projection is derived: it holds no judgement, so unlike a forecast
        version it needs no ``change_reason``. What it does need is to be
        recognisable as the same projection -- recomputing an unchanged model
        must be a ``duplicate`` rather than a second row saying the same thing,
        or the chain would grow by one every tick and mean nothing.
        """

        body = {key: value for key, value in dict(body).items()
                if key not in (_BODY_EXCLUDED - {"mission_version_ref"})}
        company_ref = _text(body.get("company_ref"), "company_ref")
        projection_ref = f"{PROJECTION_PREFIX}{company_ref}"
        body["projection_ref"] = projection_ref
        digest = body_hash(body)
        latest = self.connection.execute(
            "SELECT * FROM sensitivity_projections WHERE projection_ref=? "
            "ORDER BY version_number DESC LIMIT 1", (projection_ref,),
        ).fetchone()
        if latest is not None and latest["body_hash"] == digest:
            return {**self.projection(latest["projection_id"]), "status": "duplicate"}
        version = 1 if latest is None else int(latest["version_number"]) + 1
        prior = None if latest is None else latest["projection_id"]
        projection_id = f"{PROJECTION_PREFIX}{company_slug(company_ref)}:{version}"
        record = {
            **body,
            "id": projection_id,
            "created_at": _now(),
            "version": version,
            "prior_projection_ref": prior,
            "body_hash": digest,
        }
        # Validate first, then hash what validation produced. The validator
        # normalises -- ``validate_bridge`` runs every gap row through P15d's
        # ``_text``, which strips -- so hashing the draft and storing the wire
        # would put a hash in the row that is not the hash of the JSON beside
        # it, and the read-back check below would be comparing a number with
        # itself rather than with the bytes. The hash still covers everything
        # except itself, which is what it covered before.
        record["content_hash"] = None
        wire = validate_projection(record)
        wire["content_hash"] = content_hash(
            {key: value for key, value in wire.items() if key != "content_hash"})
        # P17b. Four columns of the same chain at four levels of one
        # assumption: the frozen formula fixes which way each has to move, and
        # a column that moves the other way is a what-if nobody should read.
        # A failure refuses the whole table rather than the offending column,
        # because the ranking is computed across all of them.
        from .economic_invariants import evaluate_projection, gate

        gate(self.store, evaluate_projection(
            wire, statement_rows=statement_rows, solver_results=solver_results),
            mission_version_ref=wire.get("mission_version_ref"))
        with self._transaction() as cur:
            if cur.execute(
                "SELECT 1 FROM sensitivity_projections WHERE projection_id=?",
                (projection_id,),
            ).fetchone():
                raise SensitivityConflict("sensitivity projection id already exists")
            cur.execute(
                "INSERT INTO sensitivity_projections"
                "(projection_id,projection_ref,version_number,prior_projection_id,"
                "company_ref,model_version_ref,model_version_hash,inputs_hash,"
                "fingerprint,body_hash,record_json,content_hash,actor_ref,created_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    projection_id, projection_ref, version, prior, company_ref,
                    wire["model_version_ref"], wire["model_version_hash"],
                    wire["inputs_hash"], wire["fingerprint"], digest,
                    canonical_json(wire), wire["content_hash"], wire["actor_ref"],
                    wire["created_at"],
                ),
            )
        stored = self.projection(projection_id)
        if stored["content_hash"] != wire["content_hash"]:
            raise SensitivityConflict("sensitivity projection did not read back as written")
        return {**stored, "status": "fresh"}

    def projection(self, projection_ref: str) -> dict[str, Any]:
        projection_ref = _text(projection_ref, "projection_ref")
        row = self.connection.execute(
            "SELECT * FROM sensitivity_projections WHERE projection_id=?",
            (projection_ref,),
        ).fetchone()
        if row is None:
            raise SensitivityNotFound("sensitivity projection was not found")
        wire = validate_projection(json.loads(row["record_json"]))
        if (
            wire["id"] != row["projection_id"]
            or wire["version"] != row["version_number"]
            or wire["company_ref"] != row["company_ref"]
            or wire["body_hash"] != row["body_hash"]
            or wire["content_hash"] != row["content_hash"]
        ):
            raise SensitivityConflict("sensitivity projection authority drifted")
        return wire

    def latest(self, company_ref: str) -> dict[str, Any] | None:
        company_ref = _text(company_ref, "company_ref")
        row = self.connection.execute(
            "SELECT projection_id FROM sensitivity_projections WHERE company_ref=? "
            "ORDER BY version_number DESC LIMIT 1", (company_ref,),
        ).fetchone()
        return None if row is None else self.projection(row["projection_id"])

    def versions(self, company_ref: str) -> list[dict[str, Any]]:
        company_ref = _text(company_ref, "company_ref")
        rows = self.connection.execute(
            "SELECT projection_id FROM sensitivity_projections WHERE company_ref=? "
            "ORDER BY version_number", (company_ref,),
        ).fetchall()
        return [self.projection(row["projection_id"]) for row in rows]

    def companies(self) -> list[str]:
        return [str(row["company_ref"]) for row in self.connection.execute(
            "SELECT DISTINCT company_ref FROM sensitivity_projections "
            "ORDER BY company_ref").fetchall()]


def table_exists(connection: Any) -> bool:
    try:
        row = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            ("sensitivity_projections",),
        ).fetchone()
    except sqlite3.Error:
        return False
    return row is not None


__all__ = [
    "AUTOMATION_ACTOR",
    "BAND_RULE_REF",
    "IMPACT_PRECEDENCE",
    "MAX_DRIVERS",
    "MIN_BAND_POINTS",
    "MIN_DRIVERS",
    "PROJECTION_PREFIX",
    "SCENARIOS",
    "SCHEMA_VERSION",
    "SELECTION_RULE",
    "SELECTION_RULE_HASH",
    "SELECTION_RULE_REF",
    "UNIT_MOVE",
    "VALUE_KIND",
    "WHAT_IF_LINES",
    "WRITE_SCOPE",
    "SensitivityConflict",
    "SensitivityError",
    "SensitivityNotFound",
    "SensitivityProjectionAuthority",
    "SensitivityUnavailable",
    "SensitivityValidationError",
    "body_hash",
    "build_projection",
    "candidate_drivers",
    "driver_impact",
    "driver_swing",
    "fingerprint",
    "historical_band",
    "horizon_total",
    "impact_metric",
    "live_assumptions",
    "measure_series",
    "open_periods",
    "projection_readiness",
    "recompute",
    "scenario_lines",
    "select_drivers",
    "table_exists",
    "validate_projection",
    "what_if",
]
