"""P17b: the checks that do not need a model to know a number is wrong.

Chem's Linde workbook carried forty-two structural checks and every one of
them said OK. It also said the conservative fifteen-year scenario returned
zero percent, because the bisection that solved for the internal rate of
return was bounded to ``[0.0, 1.0]`` and a loss has a negative one. The solver
did not fail; it returned its own lower bound, the bound looked like an
answer, and the answer shipped.

Every check in that workbook was a check *of the model against itself*:
does this cell reference that cell, does this column sum to that row. None of
them could catch a clamp, because a clamped number is arithmetically perfect
-- it is only economically impossible. This module is the other kind of check.
It knows nothing about how the forecast was built and asks only whether the
result could be true of a company:

* **direction** -- the frozen chain is ``operating_income[k] = revenue[k] *
  (1 - cost_share[k] - sum(opex_share[k]))``. Hold the ratios and operating
  income is proportional to revenue, so revenue up and ratios held must not
  produce operating income down whenever the implied margin is not negative.
  Both halves are checked: the proportion, and the direction.
* **band** -- an assumption outside the min and max the company has actually
  filed is not forbidden, it is *unexplained*. It passes when it is inside,
  and it passes when it is outside and says why. It fails when it is outside
  and silent, because that is the assumption nobody argued about.
* **domain** -- a margin may be negative and may not exceed one; growth of
  minus one or less is not growth, it is a company with no revenue; a tax rate
  outside ``[0, 1]`` on a profit is not a rate.
* **segments** -- where the filings break a line down, the parts add to the
  whole. Within a tolerance, because filers round.
* **period basis** -- a quarter and a year-to-date figure both end on the same
  day and differ only in ``period_start``. Summing four of them mixed is how a
  half year gets booked twice, and P14f's other half applies here too: a live
  estimate and the filed actual for the same quarter never sit in one line
  unmarked.
* **solver bounds** -- the Linde case, generalised. A derived quantity that
  came out of a search must lie *strictly inside* the bracket it searched, or
  be reported as ``unbounded``. Landing exactly on a bound is not a solution;
  it is the search saying it did not find one.

Everything here is pure. ``check`` takes normalised data and returns a frozen
tuple of results; it opens nothing, writes nothing and imports no state. The
three builders below turn a forecast model version, a sensitivity projection
and a valuation snapshot into that normalised shape, and
``EconomicInvariantAuthority`` is where a refusal is recorded so the cockpit
can show the reader why an output is not there.

**A failure is a refusal, not a repair.** The whole output is recorded
``unavailable`` with the reasons. Nothing here fixes a number, clamps one, or
publishes the good half of a bad model: a model with one impossible line in it
is a model whose other lines were computed by the same arithmetic.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, localcontext
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence

from .company_model_series import INSTANT, QUARTER, period_kind
from .store import (
    DaltonStore, authorization_flag, authorized_flag, canonical_json, content_hash,
)

SCHEMA_VERSION = "0.1"
RULE_REF = "rule:economic-invariants:1"
ACTOR_REF = "core:economic-invariant-gate"
REFUSAL_PREFIX = "economic-invariant-refusal:"

_SCHEMA_PATH = Path(__file__).with_name("economic_invariant_schema.sql")
# Wide enough that a company's revenue times a ratio carried to twelve places
# never reaches it, matching the forecast engine's own context.
_PRECISION = 60

# Filers round. A segment table that adds to the consolidated line to within a
# millionth of it added up; one that misses by a percent did not.
RELATIVE_TOLERANCE = Decimal("0.000001")
# And below the relative tolerance's reach, an absolute floor, so a line that
# is nearly zero does not need to agree to eight decimal places.
ABSOLUTE_TOLERANCE = Decimal("0.01")

PASS = "pass"
FAIL = "fail"
NOT_APPLICABLE = "not_applicable"
STATUSES: tuple[str, ...] = (PASS, FAIL, NOT_APPLICABLE)

DIRECTION = "direction_consistency"
BAND = "assumption_band"
DOMAIN = "rate_domain"
SEGMENT_SUM = "segment_sum"
PERIOD_BASIS = "period_basis"
SOLVER_BOUNDS = "solver_bounds"
# The order they run in and the order they are reported in. Frozen, because a
# report whose rows move between runs cannot be diffed.
INVARIANTS: tuple[str, ...] = (
    DIRECTION, BAND, DOMAIN, SEGMENT_SUM, PERIOD_BASIS, SOLVER_BOUNDS,
)
INVARIANT_LABELS: dict[str, str] = {
    DIRECTION: "方向一致：收入涨、成本占比不变，营业利润不该跌",
    BAND: "假设落在历史带内，或显式标 outside_band 并写理由",
    DOMAIN: "率类量在定义域内：毛利率不超过 1，税率在 [0,1]",
    SEGMENT_SUM: "分部之和等于合并",
    PERIOD_BASIS: "单季与累计（YTD）不混用；估计与实际不混格",
    SOLVER_BOUNDS: "求解结果严格落在搜索区间内，或报 unbounded，绝不夹边",
}

AVAILABLE = "available"
UNAVAILABLE = "unavailable"

FORECAST_MODEL = "forecast_model_version"
SENSITIVITY_PROJECTION = "sensitivity_projection"
VALUATION_SNAPSHOT = "valuation_snapshot"
OUTPUT_KINDS: tuple[str, ...] = (
    FORECAST_MODEL, SENSITIVITY_PROJECTION, VALUATION_SNAPSHOT,
)
OUTPUT_KIND_LABELS: dict[str, str] = {
    FORECAST_MODEL: "预测模型版本",
    SENSITIVITY_PROJECTION: "敏感性投影",
    VALUATION_SNAPSHOT: "估值快照",
}

# What the direction check reads off a driver's role: which line that driver
# moves, and which way the frozen formula says it moves it. ``None`` means the
# sign is not fixed by the role and has to be read from the implied margin --
# revenue is the one such case, because operating income is revenue times a
# margin that may itself be negative.
_ROLE_DIRECTION: dict[str, tuple[str, int | None]] = {
    "revenue": ("result:operating_income", None),
    "cost_of_revenue": ("result:operating_income", -1),
    "operating_expense": ("result:operating_income", -1),
    "income_tax_expense": ("result:net_income", -1),
    "net_income": ("result:net_income", 1),
    "operating_cash_flow": ("result:free_cash_flow", 1),
    "capital_expenditure": ("result:free_cash_flow", -1),
}
# Roles whose assumption is a share of revenue and therefore sits inside the
# implied margin.
_MARGIN_ROLES = frozenset({"cost_of_revenue", "operating_expense"})


class EconomicInvariantError(RuntimeError):
    """Base error for the invariant layer."""


class EconomicInvariantValidationError(EconomicInvariantError, ValueError):
    """A subject or a refusal was not in the shape this layer accepts."""


class EconomicInvariantConflict(EconomicInvariantError):
    """A refusal could not be stored, or did not read back as written."""


class EconomicInvariantRefused(EconomicInvariantError):
    """An output failed an economic invariant and was not published.

    Carries the whole report rather than a message, because the caller's job
    is to record every reason -- a gate that reports the first failure teaches
    the next person to fix one thing and try again.
    """

    def __init__(self, report: "InvariantReport") -> None:
        super().__init__(report.message())
        self.report = report


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _decimal(value: Any) -> Decimal | None:
    """The value as a Decimal, or ``None`` when it is not one.

    Never a float on the way in: a value that arrives as a float has already
    lost the digits this layer would be comparing.
    """

    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, Decimal):
        return value
    if isinstance(value, float):
        raise EconomicInvariantValidationError(
            "a float never reaches an invariant; the engine is Decimal throughout")
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _close(left: Decimal, right: Decimal) -> bool:
    scale = max(abs(left), abs(right))
    return abs(left - right) <= max(ABSOLUTE_TOLERANCE, RELATIVE_TOLERANCE * scale)


def _sign(value: Decimal) -> int:
    return 0 if value == 0 else (1 if value > 0 else -1)


@dataclass(frozen=True)
class InvariantResult:
    """One invariant's verdict, and every finding behind it."""

    invariant: str
    status: str
    reason: str | None = None
    findings: tuple[str, ...] = ()
    checked: int = 0

    def __post_init__(self) -> None:
        if self.invariant not in INVARIANTS:
            raise EconomicInvariantValidationError(
                f"{self.invariant} is not one of the frozen invariants")
        if self.status not in STATUSES:
            raise EconomicInvariantValidationError(
                f"{self.status} is not an invariant status")
        if self.status == FAIL and not self.findings:
            raise EconomicInvariantValidationError(
                "a failing invariant must say what failed")

    @property
    def label(self) -> str:
        return INVARIANT_LABELS[self.invariant]

    def as_dict(self) -> dict[str, Any]:
        return {
            "invariant": self.invariant,
            "label": self.label,
            "status": self.status,
            "reason": self.reason,
            "findings": list(self.findings),
            "checked": self.checked,
        }


@dataclass(frozen=True)
class InvariantReport:
    """Every invariant's verdict for one output, and what follows from them."""

    output_kind: str
    output_ref: str
    company_ref: str
    results: tuple[InvariantResult, ...] = ()
    rule_ref: str = RULE_REF
    subject_hash: str | None = None

    def __post_init__(self) -> None:
        if self.output_kind not in OUTPUT_KINDS:
            raise EconomicInvariantValidationError(
                f"{self.output_kind} is not an output this gate covers")
        seen = [item.invariant for item in self.results]
        if seen != [name for name in INVARIANTS if name in set(seen)]:
            raise EconomicInvariantValidationError(
                "invariant results must arrive in the frozen order")

    @property
    def failures(self) -> tuple[InvariantResult, ...]:
        return tuple(item for item in self.results if item.status == FAIL)

    @property
    def status(self) -> str:
        return UNAVAILABLE if self.failures else AVAILABLE

    @property
    def reasons(self) -> tuple[str, ...]:
        out: list[str] = []
        for item in self.failures:
            out.extend(f"{item.invariant}: {finding}" for finding in item.findings)
        return tuple(out)

    def message(self) -> str:
        if not self.failures:
            return f"{self.output_ref} satisfies every economic invariant"
        return (
            f"{self.output_ref} is unavailable: "
            + "; ".join(self.reasons)
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "output_kind": self.output_kind,
            "output_kind_label": OUTPUT_KIND_LABELS[self.output_kind],
            "output_ref": self.output_ref,
            "company_ref": self.company_ref,
            "rule_ref": self.rule_ref,
            "subject_hash": self.subject_hash,
            "status": self.status,
            "results": [item.as_dict() for item in self.results],
            "reasons": list(self.reasons),
        }


# ---------------------------------------------------------------------------
# the six invariants
#
# Every one of them takes a normalised sequence and returns one
# ``InvariantResult``.  Nothing an invariant is handed is optional in the sense
# of "assume a default": what is not there is ``not_applicable`` with the
# reason, because an invariant that silently passes on missing input is the
# forty-second check that said OK.
# ---------------------------------------------------------------------------


def _direction(series: Sequence[Mapping[str, Any]]) -> InvariantResult:
    """Revenue up, ratios held, operating income not down.

    Read straight off the frozen chain rather than assumed. ``compute_results``
    computes ``cost_of_revenue[k] = revenue[k] * share[k]``, each operating
    expense the same way, and ``operating_income[k] = gross_profit[k] -
    sum(operating_expense[k])``. Substituting gives

        operating_income[k] = revenue[k] * (1 - cost_share[k] - sum(opex_share[k]))

    so across two quarters that hold every one of those shares, operating
    income is revenue times a constant. Two things follow and both are checked:
    the ratio of operating incomes equals the ratio of revenues, and -- when
    the implied margin is not negative -- revenue up cannot mean operating
    income down. The margin's sign matters: a company whose costs exceed its
    revenue loses more by selling more, and that is the formula behaving, not
    breaking.
    """

    rows = [dict(item) for item in series]
    if len(rows) < 2:
        return InvariantResult(
            DIRECTION, NOT_APPLICABLE,
            reason="fewer than two comparable quarters computed, so there is no "
                   "direction to check")
    findings: list[str] = []
    checked = 0
    with localcontext() as ctx:
        ctx.prec = _PRECISION
        for prior, current in zip(rows, rows[1:]):
            if prior.get("ratios") != current.get("ratios"):
                # A quarter that moved a cost ratio is a quarter where operating
                # income is allowed to move any way at all. Skipping it is the
                # point: this invariant is about the case where nothing but
                # revenue changed.
                continue
            rev_a, rev_b = _decimal(prior.get("revenue")), _decimal(current.get("revenue"))
            op_a, op_b = _decimal(prior.get("operating_income")), _decimal(
                current.get("operating_income"))
            if rev_a is None or rev_b is None or op_a is None or op_b is None:
                continue
            checked += 1
            end_a = str(prior.get("period_end"))
            end_b = str(current.get("period_end"))
            # The economic statement first, because it is the one a reader
            # needs to see; the arithmetic one after it, because a chain that
            # breaks the first necessarily breaks the second and reporting only
            # the second would describe the symptom.
            if rev_a != 0:
                margin = op_a / rev_a
                if rev_b > rev_a and margin >= 0 and op_b < op_a:
                    findings.append(
                        f"{end_a} to {end_b} raises revenue from {rev_a} to "
                        f"{rev_b} with every cost ratio held and a margin of "
                        f"{margin}, and operating income falls from {op_a} to "
                        f"{op_b}")
            if not _close(op_b * rev_a, op_a * rev_b):
                findings.append(
                    f"{end_a} to {end_b} holds every cost ratio, so operating "
                    f"income must stay proportional to revenue, but "
                    f"{op_a}/{rev_a} and {op_b}/{rev_b} are not the same margin")
    if findings:
        return InvariantResult(DIRECTION, FAIL, findings=tuple(findings), checked=checked)
    if not checked:
        return InvariantResult(
            DIRECTION, NOT_APPLICABLE, checked=0,
            reason="no two adjacent quarters both computed with the cost ratios held")
    return InvariantResult(DIRECTION, PASS, checked=checked)


def _scenario_direction(scenarios: Sequence[Mapping[str, Any]]) -> InvariantResult:
    """The same rule applied across a sensitivity column instead of a quarter.

    A what-if holds one driver at a level and recomputes. The frozen chain
    fixes which way the bottom line has to move as that level rises: a bigger
    share of revenue spent is a smaller operating income, a bigger share of
    operating income taken in tax is a smaller net income, and a faster revenue
    growth moves operating income the way the implied margin points. A column
    that moves the other way is a scenario nobody should read.
    """

    findings: list[str] = []
    checked = 0
    with localcontext() as ctx:
        ctx.prec = _PRECISION
        for entry in scenarios:
            role = str(entry.get("role") or "")
            if role not in _ROLE_DIRECTION:
                continue
            _, fixed = _ROLE_DIRECTION[role]
            if fixed is None:
                margin = _decimal(entry.get("margin"))
                if margin is None:
                    continue
                fixed = 1 if margin > 0 else (-1 if margin < 0 else 0)
                if fixed == 0:
                    continue
            points = []
            for point in entry.get("points") or []:
                level = _decimal(point.get("value"))
                total = _decimal(point.get("line_total"))
                if level is None or total is None:
                    continue
                points.append((level, total, str(point.get("scenario"))))
            points.sort(key=lambda item: item[0])
            if len(points) < 2:
                continue
            checked += 1
            label = str(entry.get("label") or entry.get("driver_ref"))
            for (low, low_total, low_name), (high, high_total, high_name) in zip(
                    points, points[1:]):
                if low == high:
                    continue
                moved = _sign(high_total - low_total)
                if moved == 0 or moved == fixed:
                    continue
                way = "rise" if fixed > 0 else "fall"
                findings.append(
                    f"{label}: moving {entry.get('measure')} from {low} "
                    f"({low_name}) to {high} ({high_name}) must make "
                    f"{entry.get('line_ref')} {way}, and it goes from "
                    f"{low_total} to {high_total}")
    if findings:
        return InvariantResult(DIRECTION, FAIL, findings=tuple(findings), checked=checked)
    if not checked:
        return InvariantResult(
            DIRECTION, NOT_APPLICABLE, checked=0,
            reason="no driver has two scenarios that both computed")
    return InvariantResult(DIRECTION, PASS, checked=checked)


def _band(assumptions: Sequence[Mapping[str, Any]]) -> InvariantResult:
    """Inside the filed range, or outside it and saying so.

    Not a refusal of an out-of-band assumption -- most of what a person is paid
    for is the assumption the history does not contain. A refusal of a *silent*
    one: the band is what the company has actually done, and a number outside
    it that nobody wrote a sentence about is a number nobody argued.
    """

    findings: list[str] = []
    checked = 0
    for item in assumptions:
        band = item.get("band") or {}
        ref = str(item.get("ref"))
        if str(band.get("status")) != AVAILABLE:
            continue
        value = _decimal(item.get("value"))
        low, high = _decimal(band.get("min")), _decimal(band.get("max"))
        if value is None or low is None or high is None:
            continue
        checked += 1
        if low <= value <= high:
            if item.get("outside_band"):
                findings.append(
                    f"{ref} is marked outside_band at {value}, and the filed "
                    f"range {low} to {high} contains it")
            continue
        reason = str(item.get("reason") or "").strip()
        if not item.get("outside_band"):
            findings.append(
                f"{ref} assumes {value} for {item.get('measure')}, outside the "
                f"filed range {low} to {high} over {band.get('count')} quarters, "
                "and is not marked outside_band")
        elif not reason:
            findings.append(
                f"{ref} is marked outside_band at {value} against the filed "
                f"range {low} to {high} with no reason given")
    if findings:
        return InvariantResult(BAND, FAIL, findings=tuple(findings), checked=checked)
    if not checked:
        return InvariantResult(
            BAND, NOT_APPLICABLE, checked=0,
            reason="no assumption has enough filed history to have a band")
    return InvariantResult(BAND, PASS, checked=checked)


def _domain(quantities: Sequence[Mapping[str, Any]]) -> InvariantResult:
    """Rate-type quantities inside the domain their own definition gives them.

    ``margin`` may be negative -- a company can lose money -- and may not
    exceed one, because it is what is left of a revenue after spending part of
    it. ``growth`` of minus one or worse is not a shrinking company, it is a
    company with no revenue at all and a chain that divides by it next quarter.
    ``tax_rate`` is a share of a profit and lives in ``[0, 1]``; a benefit is a
    real thing and a benefit *carried forward as a rate* is the Chem error.
    """

    findings: list[str] = []
    checked = 0
    for item in quantities:
        kind = str(item.get("rate_kind"))
        value = _decimal(item.get("value"))
        if value is None:
            continue
        ref = str(item.get("ref"))
        label = str(item.get("label") or ref)
        checked += 1
        if kind == "margin":
            if value > 1:
                findings.append(
                    f"{label} is a margin of {value}; a margin above one means "
                    "a company kept more than it sold")
        elif kind == "growth":
            if value <= -1:
                findings.append(
                    f"{label} grows by {value}; at minus one or below the line "
                    "it multiplies is zero or negative")
        elif kind == "tax_rate":
            if not Decimal(0) <= value <= Decimal(1):
                findings.append(
                    f"{label} is a tax rate of {value}, outside [0, 1]")
        else:
            raise EconomicInvariantValidationError(
                f"{kind} is not a rate kind this invariant knows")
    if findings:
        return InvariantResult(DOMAIN, FAIL, findings=tuple(findings), checked=checked)
    if not checked:
        return InvariantResult(
            DOMAIN, NOT_APPLICABLE, checked=0,
            reason="this output carries no rate-type quantity")
    return InvariantResult(DOMAIN, PASS, checked=checked)


def _segment_sum(groups: Sequence[Mapping[str, Any]]) -> InvariantResult:
    """The parts add to the whole, within a filer's rounding.

    Only where both exist. A consolidated line with no breakdown is not a
    failure, and a breakdown with no consolidated line to check it against is
    nothing to check -- both are ``not_applicable`` rather than a pass, because
    a pass would read as "the segments were checked".
    """

    findings: list[str] = []
    checked = 0
    with localcontext() as ctx:
        ctx.prec = _PRECISION
        for group in groups:
            total = _decimal(group.get("total"))
            parts = [(_decimal(part.get("value")), str(part.get("member")))
                     for part in (group.get("parts") or [])]
            usable = [(value, member) for value, member in parts if value is not None]
            if total is None or len(usable) < 2 or len(usable) != len(parts):
                continue
            checked += 1
            summed = sum((value for value, _ in usable), Decimal(0))
            if _close(summed, total):
                continue
            findings.append(
                f"{group.get('line')} for {group.get('period')} on axis "
                f"{group.get('axis')}: {len(usable)} segments add to {summed} "
                f"and the consolidated line is {total}")
    if findings:
        return InvariantResult(
            SEGMENT_SUM, FAIL, findings=tuple(findings), checked=checked)
    if not checked:
        return InvariantResult(
            SEGMENT_SUM, NOT_APPLICABLE, checked=0,
            reason="no line has both a consolidated figure and a breakdown to "
                   "check it against")
    return InvariantResult(SEGMENT_SUM, PASS, checked=checked)


def _period_basis(lines: Sequence[Mapping[str, Any]]) -> InvariantResult:
    """One line, one kind of period -- and one answer per quarter.

    A 10-Q reports the quarter and the year to date and they end on the same
    day: ``period_kind`` is the only thing that tells them apart, and four
    figures summed with one year-to-date among them books a half year twice.
    The second half is P14f's convention: when a quarter is filed, the estimate
    stays and is marked ``superseded_by`` the actual beside it. A live estimate
    and an actual for the same quarter in one line is two answers to one
    question, and whichever a reader takes is chance.
    """

    findings: list[str] = []
    checked = 0
    for line in lines:
        ref = str(line.get("ref"))
        expected = line.get("expected_period_kind")
        kinds: dict[str, list[str]] = {}
        live: dict[str, list[str]] = {}
        for cell in line.get("cells") or []:
            end = str(cell.get("period_end") or "")
            if not end or cell.get("status") not in (None, "computed"):
                continue
            kind = period_kind(cell.get("period_start"), end)
            kinds.setdefault(kind, []).append(end)
            if not cell.get("superseded_by"):
                live.setdefault(end, []).append(str(cell.get("kind") or "estimate"))
        if not kinds:
            continue
        checked += 1
        if len(kinds) > 1:
            spread = ", ".join(
                f"{kind} ({', '.join(sorted(ends)[:3])})"
                for kind, ends in sorted(kinds.items()))
            findings.append(
                f"{ref} mixes period bases in one line: {spread}")
        elif expected is not None and expected not in kinds:
            findings.append(
                f"{ref} is summed as {expected} figures and carries "
                f"{sorted(kinds)[0]} periods instead")
        for end, seen in sorted(live.items()):
            if len(set(seen)) > 1:
                findings.append(
                    f"{ref} carries a live {' and a live '.join(sorted(set(seen)))} "
                    f"cell for {end}; the estimate an actual replaced must be "
                    "marked superseded_by")
    if findings:
        return InvariantResult(
            PERIOD_BASIS, FAIL, findings=tuple(findings), checked=checked)
    if not checked:
        return InvariantResult(
            PERIOD_BASIS, NOT_APPLICABLE, checked=0,
            reason="this output carries no dated line to check")
    return InvariantResult(PERIOD_BASIS, PASS, checked=checked)


def _solver_bounds(results: Sequence[Mapping[str, Any]]) -> InvariantResult:
    """Strictly inside the bracket, or ``unbounded``. Never on the edge.

    The Linde case. A bisection over ``[0.0, 1.0]`` handed a loss-making
    scenario back as exactly ``0.0``, which is not the root -- it is the lower
    bound, and it is also a number that reads as an answer. A search that ends
    on its own bound did not converge; it ran out of bracket. The honest
    outcome is ``unbounded`` with no value, and this refuses anything else.
    """

    findings: list[str] = []
    checked = 0
    for item in results:
        ref = str(item.get("ref"))
        label = str(item.get("label") or ref)
        status = str(item.get("status") or "solved")
        value = _decimal(item.get("value"))
        low = _decimal(item.get("lower_bound"))
        high = _decimal(item.get("upper_bound"))
        checked += 1
        if status == UNAVAILABLE:
            continue
        if status == "unbounded":
            if value is not None:
                findings.append(
                    f"{label} is reported unbounded and still carries the value "
                    f"{value}; a search that did not bracket a root has no result")
            continue
        if status != "solved":
            raise EconomicInvariantValidationError(
                f"{status} is not a solver status this invariant knows")
        if low is None or high is None:
            findings.append(
                f"{label} is reported solved without naming the bracket it "
                "searched, so nobody can tell a root from a bound")
            continue
        if low >= high:
            findings.append(
                f"{label} searched [{low}, {high}], which is not a bracket")
            continue
        if value is None:
            findings.append(
                f"{label} is reported solved with no value")
        elif value <= low or value >= high:
            findings.append(
                f"{label} came back {value} from a search over [{low}, {high}]; "
                "a result on or outside its own bound is the bound, not a root, "
                "and must be reported unbounded")
    if findings:
        return InvariantResult(
            SOLVER_BOUNDS, FAIL, findings=tuple(findings), checked=checked)
    if not checked:
        return InvariantResult(
            SOLVER_BOUNDS, NOT_APPLICABLE, checked=0,
            reason="this output rests on no solved quantity")
    return InvariantResult(SOLVER_BOUNDS, PASS, checked=checked)


# ---------------------------------------------------------------------------
# the pure entry point
# ---------------------------------------------------------------------------

SUBJECT_KEYS = frozenset({
    "direction_series", "scenarios", "assumption_bands", "rate_quantities",
    "segments", "lines", "solver_results",
})


def _merge_direction(first: InvariantResult, second: InvariantResult) -> InvariantResult:
    """One direction verdict out of the per-quarter and per-scenario halves."""

    findings = first.findings + second.findings
    checked = first.checked + second.checked
    if findings:
        return InvariantResult(DIRECTION, FAIL, findings=findings, checked=checked)
    if first.status == PASS or second.status == PASS:
        return InvariantResult(DIRECTION, PASS, checked=checked)
    return InvariantResult(
        DIRECTION, NOT_APPLICABLE, checked=0,
        reason=first.reason or second.reason)


def check(subject: Mapping[str, Any]) -> tuple[InvariantResult, ...]:
    """Every invariant against one normalised subject, in the frozen order.

    Pure: no clock, no store, no imports of state. Given the same subject this
    returns the same tuple, which is what lets a refusal be replayed against
    the record it refused.
    """

    if not isinstance(subject, Mapping):
        raise EconomicInvariantValidationError("a subject must be an object")
    unknown = sorted(set(subject) - SUBJECT_KEYS)
    if unknown:
        raise EconomicInvariantValidationError(
            f"a subject carries only {sorted(SUBJECT_KEYS)}; got {unknown}")
    return (
        _merge_direction(
            _direction(subject.get("direction_series") or ()),
            _scenario_direction(subject.get("scenarios") or ()),
        ),
        _band(subject.get("assumption_bands") or ()),
        _domain(subject.get("rate_quantities") or ()),
        _segment_sum(subject.get("segments") or ()),
        _period_basis(subject.get("lines") or ()),
        _solver_bounds(subject.get("solver_results") or ()),
    )


def evaluate(
    *, output_kind: str, output_ref: str, company_ref: str,
    subject: Mapping[str, Any],
) -> InvariantReport:
    """``check`` plus the identity of the thing being checked."""

    return InvariantReport(
        output_kind=output_kind,
        output_ref=str(output_ref),
        company_ref=str(company_ref),
        results=check(subject),
        subject_hash=content_hash(json.loads(canonical_json(dict(subject)))),
    )


# ---------------------------------------------------------------------------
# turning the three outputs into a subject
# ---------------------------------------------------------------------------


def segment_groups(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Filed statement rows regrouped as consolidated line plus its parts.

    The shape ``sec_financials_normalise`` already produces: a breakdown row
    carries ``dimension_axis`` and ``dimension_member``, the consolidated one
    carries neither. Grouping by concept, period *and axis* matters -- a filer
    reports the same revenue split by segment and by geography, and adding the
    two together would double it.
    """

    consolidated: dict[tuple[str, str, str], Any] = {}
    parts: dict[tuple[str, str, str, str], list[dict[str, Any]]] = {}
    for row in rows:
        concept = str(row.get("concept") or "")
        end = str(row.get("period_end") or "")
        start = str(row.get("period_start") or "")
        if not concept or not end:
            continue
        axis = row.get("dimension_axis")
        key = (concept, start, end)
        if not axis and not row.get("is_breakdown"):
            consolidated[key] = row.get("value")
        elif axis:
            parts.setdefault((*key, str(axis)), []).append({
                "member": str(row.get("dimension_member") or ""),
                "value": row.get("value"),
            })
    out: list[dict[str, Any]] = []
    for (concept, start, end, axis), members in sorted(parts.items()):
        if (concept, start, end) not in consolidated:
            continue
        out.append({
            "line": concept,
            "period": f"{start}..{end}" if start else end,
            "axis": axis,
            "total": consolidated[(concept, start, end)],
            "parts": sorted(members, key=lambda item: item["member"]),
        })
    return out


def _result_line(record: Mapping[str, Any], ref: str) -> Mapping[str, Any] | None:
    return next((item for item in (record.get("results") or [])
                 if str(item.get("ref")) == ref), None)


def _estimate_values(record: Mapping[str, Any], ref: str) -> dict[str, str]:
    line = _result_line(record, ref)
    if line is None:
        return {}
    return {
        str(cell["period"]["end"]): str(cell["value"])
        for cell in (line.get("cells") or [])
        if cell.get("kind") == "estimate" and cell.get("status") == "computed"
        and not cell.get("superseded_by") and cell.get("value") is not None
    }


def _live_assumptions(record: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    return [item for item in (record.get("assumptions") or [])
            if item.get("kind") != "actual" and not item.get("superseded_by")]


def _driver_roles(record: Mapping[str, Any]) -> dict[str, str | None]:
    return {str(item.get("ref")): item.get("role")
            for item in (record.get("drivers") or [])}


def measure_band(record: Mapping[str, Any], driver_ref: str, measure: str) -> dict[str, Any]:
    """The min and max the company has actually filed for one driver's measure.

    P13-M3 already builds this series and already froze the rule for how few
    observations are too few, so this reads it rather than defining a second
    band that could disagree with the one the sensitivity table prints. The
    import is local because the sensitivity authority calls this module back
    at publication time.
    """

    from .forecast_sensitivity import MIN_BAND_POINTS, measure_series

    series = measure_series(record, driver_ref, measure)
    if series.get("status") != AVAILABLE:
        return {"status": UNAVAILABLE, "reason": series.get("reason"),
                "min": None, "max": None, "count": 0}
    points = [str(item["value"]) for item in (series.get("points") or [])]
    if len(points) < MIN_BAND_POINTS:
        return {
            "status": UNAVAILABLE, "min": None, "max": None, "count": len(points),
            "reason": f"only {len(points)} filed observations; a band needs at "
                      f"least {MIN_BAND_POINTS}",
        }
    values = [Decimal(item) for item in points]
    return {
        "status": AVAILABLE, "reason": None, "count": len(values),
        "min": str(min(values)), "max": str(max(values)),
    }


def forecast_subject(
    record: Mapping[str, Any],
    *,
    statement_rows: Sequence[Mapping[str, Any]] = (),
    solver_results: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """A forecast model version, in the shape ``check`` reads.

    Everything comes out of the record itself -- the drivers' filed history,
    the live assumptions, the result cells -- so a model can be checked before
    it is stored and again after it is read back.
    """

    roles = _driver_roles(record)
    live = _live_assumptions(record)
    revenue = _estimate_values(record, "result:revenue")
    operating = _estimate_values(record, "result:operating_income")

    ratios: dict[str, list[tuple[str, str]]] = {}
    for item in live:
        role = roles.get(str(item["driver_ref"]))
        if role in _MARGIN_ROLES:
            ratios.setdefault(str(item["period"]["end"]), []).append(
                (str(item["driver_ref"]), str(item["value"])))
    ends = [str(item["end"]) for item in (record.get("forecast_periods") or [])]
    direction_series = [{
        "period_end": end,
        "revenue": revenue.get(end),
        "operating_income": operating.get(end),
        "ratios": sorted(ratios.get(end, [])),
    } for end in ends]

    bands: list[dict[str, Any]] = []
    rates: list[dict[str, Any]] = []
    cache: dict[tuple[str, str], dict[str, Any]] = {}
    for item in live:
        driver_ref = str(item["driver_ref"])
        measure = str(item["measure"])
        key = (driver_ref, measure)
        if key not in cache:
            cache[key] = measure_band(record, driver_ref, measure)
        bands.append({
            "ref": str(item["ref"]),
            "driver_ref": driver_ref,
            "measure": measure,
            "value": str(item["value"]),
            "band": cache[key],
            "outside_band": bool(item.get("outside_band")),
            "reason": (item.get("outside_band") or {}).get("reason")
            if isinstance(item.get("outside_band"), Mapping) else None,
        })
        if measure == "quarterly_growth":
            rates.append({"ref": str(item["ref"]), "label": str(item["ref"]),
                          "rate_kind": "growth", "value": str(item["value"])})
        elif roles.get(driver_ref) == "income_tax_expense":
            rates.append({"ref": str(item["ref"]), "label": str(item["ref"]),
                          "rate_kind": "tax_rate", "value": str(item["value"])})

    with localcontext() as ctx:
        ctx.prec = _PRECISION
        for line_ref in ("result:gross_profit", "result:operating_income",
                         "result:net_income"):
            values = _estimate_values(record, line_ref)
            for end, raw in sorted(values.items()):
                base = revenue.get(end)
                if base is None or Decimal(base) == 0:
                    continue
                rates.append({
                    "ref": f"{line_ref}@{end}",
                    "label": f"{line_ref} margin for the quarter ended {end}",
                    "rate_kind": "margin",
                    "value": str(Decimal(raw) / Decimal(base)),
                })

    lines: list[dict[str, Any]] = []
    for driver in record.get("drivers") or []:
        cells = [{
            "period_start": cell.get("period_start"),
            "period_end": cell.get("period_end"),
            "value": cell.get("value"),
            "status": "computed",
            "kind": "actual",
        } for cell in (driver.get("history") or []) if cell.get("period_start")]
        if cells:
            lines.append({"ref": f"driver:{driver.get('ref')}",
                          "expected_period_kind": QUARTER, "cells": cells})
    for line in record.get("results") or []:
        cells = [{
            "period_start": (cell.get("period") or {}).get("start"),
            "period_end": (cell.get("period") or {}).get("end"),
            "value": cell.get("value"),
            "status": cell.get("status"),
            "kind": cell.get("kind"),
            "superseded_by": cell.get("superseded_by"),
        } for cell in (line.get("cells") or [])]
        if cells:
            lines.append({"ref": str(line.get("ref")),
                          "expected_period_kind": QUARTER, "cells": cells})

    return {
        "direction_series": direction_series,
        "assumption_bands": bands,
        "rate_quantities": rates,
        "segments": segment_groups(statement_rows),
        "lines": lines,
        "solver_results": [dict(item) for item in solver_results],
    }


def projection_subject(
    body: Mapping[str, Any],
    *,
    statement_rows: Sequence[Mapping[str, Any]] = (),
    solver_results: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """A sensitivity projection, in the shape ``check`` reads.

    The interesting half is the scenarios: four columns of the same chain at
    four levels of one assumption. The frozen formula fixes which way each
    column has to move, which is a check no amount of structural validation of
    the projection can make.
    """

    starts = {str(item["end"]): item.get("start")
              for item in (body.get("horizon") or [])}
    scenarios: list[dict[str, Any]] = []
    bands: list[dict[str, Any]] = []
    rates: list[dict[str, Any]] = []
    lines: list[dict[str, Any]] = []

    with localcontext() as ctx:
        ctx.prec = _PRECISION
        for driver in body.get("drivers") or []:
            role = str(driver.get("role") or "")
            measure = str(driver.get("measure") or "")
            driver_ref = str(driver.get("driver_ref"))
            label = str(driver.get("label") or driver_ref)
            rows = [row for row in (driver.get("what_if") or [])
                    if row.get("status") == "computed"]
            totals: dict[str, dict[str, str | None]] = {}
            for row in rows:
                totals[str(row.get("scenario"))] = {
                    str(line.get("ref")): line.get("total")
                    for line in (row.get("lines") or [])
                    if line.get("status") == "computed"
                }
                for line in row.get("lines") or []:
                    cells = [{
                        "period_start": starts.get(str(cell.get("period_end"))),
                        "period_end": cell.get("period_end"),
                        "value": cell.get("value"),
                        "status": cell.get("status"),
                        "kind": "estimate",
                    } for cell in (line.get("cells") or [])]
                    if cells:
                        lines.append({
                            "ref": f"{driver_ref}:{row.get('scenario')}:{line.get('ref')}",
                            "expected_period_kind": QUARTER, "cells": cells})
            ours = totals.get("ours") or {}
            margin = None
            base = ours.get("result:revenue")
            top = ours.get("result:operating_income")
            if base is not None and top is not None and Decimal(str(base)) != 0:
                margin = str(Decimal(str(top)) / Decimal(str(base)))
            line_ref = _ROLE_DIRECTION.get(role, (None, None))[0]
            if line_ref is not None:
                scenarios.append({
                    "driver_ref": driver_ref, "label": label, "role": role,
                    "measure": measure, "line_ref": line_ref, "margin": margin,
                    "points": [{
                        "scenario": str(row.get("scenario")),
                        "value": row.get("assumption_value"),
                        "line_total": (totals.get(str(row.get("scenario"))) or {}
                                       ).get(line_ref),
                    } for row in rows],
                })
            band = driver.get("band") or {}
            ours_value = (driver.get("ours") or {}).get("value")
            if band.get("status") == AVAILABLE and ours_value is not None:
                low = Decimal(str((band.get("trough") or {}).get("value")))
                high = Decimal(str((band.get("peak") or {}).get("value")))
                bands.append({
                    "ref": f"{driver_ref}@ours", "driver_ref": driver_ref,
                    "measure": measure, "value": str(ours_value),
                    "band": {"status": AVAILABLE, "reason": None,
                             "count": band.get("count"),
                             "min": str(min(low, high)), "max": str(max(low, high))},
                    "outside_band": False, "reason": None,
                })
            for row in rows:
                value = row.get("assumption_value")
                if value is None:
                    continue
                ref = f"{driver_ref}@{row.get('scenario')}"
                if measure == "quarterly_growth":
                    rates.append({"ref": ref, "label": f"{label} {row.get('scenario')}",
                                  "rate_kind": "growth", "value": str(value)})
                elif role == "income_tax_expense":
                    rates.append({"ref": ref, "label": f"{label} {row.get('scenario')}",
                                  "rate_kind": "tax_rate", "value": str(value)})
                scenario_totals = totals.get(str(row.get("scenario"))) or {}
                revenue_total = scenario_totals.get("result:revenue")
                operating_total = scenario_totals.get("result:operating_income")
                if (revenue_total is not None and operating_total is not None
                        and Decimal(str(revenue_total)) != 0):
                    rates.append({
                        "ref": f"{ref}:margin",
                        "label": f"{label} {row.get('scenario')} operating margin",
                        "rate_kind": "margin",
                        "value": str(Decimal(str(operating_total))
                                     / Decimal(str(revenue_total))),
                    })
    return {
        "scenarios": scenarios,
        "assumption_bands": bands,
        "rate_quantities": rates,
        "segments": segment_groups(statement_rows),
        "lines": lines,
        "solver_results": [dict(item) for item in solver_results],
    }


def valuation_subject(
    body: Mapping[str, Any],
    *,
    statement_rows: Sequence[Mapping[str, Any]] = (),
    solver_results: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """A valuation snapshot, in the shape ``check`` reads.

    A multiple is not a rate and has no band, so most of this file has nothing
    to say about one. What it does have to say is the thing the snapshot's own
    validation cannot: a trailing-year role is four *quarters*, and four
    figures that each end on a quarter end but include a nine-month
    year-to-date among them pass every check in ``_role_input`` and produce a
    price/sales that is wrong by a multiple of the overlap.
    """

    lines: list[dict[str, Any]] = []
    for window in body.get("fundamental_windows") or []:
        as_of = str(window.get("as_of"))
        for role, detail in sorted((window.get("roles") or {}).items()):
            expected = (QUARTER if str(detail.get("aggregation")) == "trailing_sum"
                        else INSTANT)
            cells = [{
                "period_start": component.get("period_start"),
                "period_end": component.get("period_end"),
                "value": component.get("value"),
                "status": "computed",
                "kind": "actual",
            } for component in (detail.get("components") or [])]
            if cells:
                lines.append({"ref": f"window:{as_of}:{role}",
                              "expected_period_kind": expected, "cells": cells})
    return {
        "assumption_bands": [],
        "rate_quantities": [],
        "segments": segment_groups(statement_rows),
        "lines": lines,
        "solver_results": [dict(item) for item in solver_results],
    }


def evaluate_forecast_model(record: Mapping[str, Any], **kwargs: Any) -> InvariantReport:
    return evaluate(
        output_kind=FORECAST_MODEL,
        output_ref=str(record.get("id") or record.get("model_ref") or "forecast-model"),
        company_ref=str(record.get("company_ref")),
        subject=forecast_subject(record, **kwargs),
    )


def evaluate_projection(body: Mapping[str, Any], **kwargs: Any) -> InvariantReport:
    return evaluate(
        output_kind=SENSITIVITY_PROJECTION,
        output_ref=str(body.get("id") or body.get("projection_ref")
                       or "sensitivity-projection"),
        company_ref=str(body.get("company_ref")),
        subject=projection_subject(body, **kwargs),
    )


def evaluate_valuation(body: Mapping[str, Any], **kwargs: Any) -> InvariantReport:
    return evaluate(
        output_kind=VALUATION_SNAPSHOT,
        output_ref=str(body.get("id") or body.get("snapshot_ref")
                       or "valuation-snapshot"),
        company_ref=str(body.get("company_ref")),
        subject=valuation_subject(body, **kwargs),
    )


# ---------------------------------------------------------------------------
# the authority: where a refusal is written down
# ---------------------------------------------------------------------------


class EconomicInvariantAuthority:
    """Append-only gate verdicts, one chain per company per output kind.

    Only two things go in: a refusal, and the pass that clears a standing
    refusal. A pass on an already-clear chain is not a verdict worth a row --
    it is the normal case, and storing it would make the chain unreadable as
    what it is, which is the record of every time this system declined to
    publish a number and why.
    """

    _authorized = authorized_flag()

    def __init__(self, store: DaltonStore):
        self.store = store
        self.connection = store.connection
        self._authorization_flag = authorization_flag(
            self.connection, "dalton_economic_invariant_authorized")
        self.connection.executescript(_SCHEMA_PATH.read_text(encoding="utf-8"))

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Cursor]:
        if self._authorized:
            raise RuntimeError("EconomicInvariantAuthority operation cannot be nested")
        self._authorized = True
        try:
            with self.store._transaction() as cur:
                yield cur
        finally:
            self._authorized = False

    @staticmethod
    def verdict_ref(company_ref: str, output_kind: str) -> str:
        return f"{REFUSAL_PREFIX}{output_kind}:{company_ref}"

    def latest(self, company_ref: str, output_kind: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT * FROM economic_invariant_verdicts WHERE verdict_ref=? "
            "ORDER BY version_number DESC LIMIT 1",
            (self.verdict_ref(str(company_ref), str(output_kind)),),
        ).fetchone()
        return None if row is None else self._decode(row)

    def verdicts(self, company_ref: str, output_kind: str) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT * FROM economic_invariant_verdicts WHERE verdict_ref=? "
            "ORDER BY version_number",
            (self.verdict_ref(str(company_ref), str(output_kind)),),
        ).fetchall()
        return [self._decode(row) for row in rows]

    def open_refusals(self) -> list[dict[str, Any]]:
        """The head of every chain that is currently refusing.

        What the cockpit needs: not the history of refusals but the ones that
        are the reason an output is not on the page today.
        """

        out: list[dict[str, Any]] = []
        rows = self.connection.execute(
            "SELECT verdict_ref, MAX(version_number) AS version_number "
            "FROM economic_invariant_verdicts GROUP BY verdict_ref "
            "ORDER BY verdict_ref").fetchall()
        for row in rows:
            head = self.connection.execute(
                "SELECT * FROM economic_invariant_verdicts WHERE verdict_ref=? "
                "AND version_number=?",
                (row["verdict_ref"], row["version_number"]),
            ).fetchone()
            record = self._decode(head)
            if record["status"] == UNAVAILABLE:
                out.append(record)
        return out

    def _decode(self, row: sqlite3.Row) -> dict[str, Any]:
        record = json.loads(row["record_json"])
        if (record.get("id") != row["verdict_id"]
                or record.get("content_hash") != row["content_hash"]
                or record.get("status") != row["status"]):
            raise EconomicInvariantConflict("economic invariant authority drifted")
        return record

    def record(
        self, report: InvariantReport, *,
        actor_ref: str = ACTOR_REF,
        mission_version_ref: str | None = None,
    ) -> dict[str, Any] | None:
        """Store one verdict, or say why it is not one.

        Returns ``None`` when the report passes and nothing was refusing --
        the caller's output is simply published. Returns the stored record
        otherwise, with ``status`` either the refusal or the clearing of one.
        """

        if not isinstance(report, InvariantReport):
            raise EconomicInvariantValidationError(
                "a verdict is recorded from an InvariantReport")
        ref = self.verdict_ref(report.company_ref, report.output_kind)
        latest = self.connection.execute(
            "SELECT * FROM economic_invariant_verdicts WHERE verdict_ref=? "
            "ORDER BY version_number DESC LIMIT 1", (ref,),
        ).fetchone()
        head = None if latest is None else self._decode(latest)
        if report.status == AVAILABLE and (head is None or head["status"] == AVAILABLE):
            return None
        if (head is not None and head["status"] == report.status
                and head["subject_hash"] == report.subject_hash
                and head["output_ref"] == report.output_ref):
            # The same output failing the same way is the same refusal. A
            # second row would say the gate fired twice, which is true and
            # uninteresting; what a reader needs is when it started.
            return {**head, "status_kind": "duplicate"}
        version = 1 if latest is None else int(latest["version_number"]) + 1
        prior = None if latest is None else str(latest["verdict_id"])
        verdict_id = f"{ref}:{version}"
        record = {
            **report.as_dict(),
            "id": verdict_id,
            "verdict_ref": ref,
            "version": version,
            "prior_version_ref": prior,
            "created_at": _now(),
            "actor_ref": str(actor_ref),
            "mission_version_ref": mission_version_ref,
        }
        record["content_hash"] = content_hash(record)
        with self._transaction() as cur:
            if cur.execute(
                "SELECT 1 FROM economic_invariant_verdicts WHERE verdict_id=?",
                (verdict_id,),
            ).fetchone():
                raise EconomicInvariantConflict(
                    "economic invariant verdict id already exists")
            cur.execute(
                "INSERT INTO economic_invariant_verdicts"
                "(verdict_id,verdict_ref,version_number,prior_verdict_id,company_ref,"
                "output_kind,output_ref,status,rule_ref,subject_hash,failure_count,"
                "record_json,content_hash,actor_ref,created_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    verdict_id, ref, version, prior, record["company_ref"],
                    record["output_kind"], record["output_ref"], record["status"],
                    record["rule_ref"], record["subject_hash"],
                    len(report.failures), canonical_json(record),
                    record["content_hash"], record["actor_ref"], record["created_at"],
                ),
            )
        stored = self.latest(report.company_ref, report.output_kind)
        if stored is None or stored["content_hash"] != record["content_hash"]:
            raise EconomicInvariantConflict(
                "economic invariant verdict did not read back as written")
        return {**stored, "status_kind": "fresh"}


def gate(
    store: Any, report: InvariantReport, *,
    actor_ref: str = ACTOR_REF,
    mission_version_ref: str | None = None,
) -> dict[str, Any] | None:
    """Record the verdict and, when it refuses, stop the publication.

    The one call every publisher makes. It writes first and raises second, so
    a refusal is on the record whether or not the caller catches the exception
    -- the failure mode this exists to prevent is a number that never appeared
    and no trace of why.
    """

    stored = None
    if store is not None:
        stored = EconomicInvariantAuthority(store).record(
            report, actor_ref=actor_ref, mission_version_ref=mission_version_ref)
    if report.failures:
        raise EconomicInvariantRefused(report)
    return stored


__all__ = [
    "ABSOLUTE_TOLERANCE",
    "ACTOR_REF",
    "AVAILABLE",
    "BAND",
    "DIRECTION",
    "DOMAIN",
    "FAIL",
    "FORECAST_MODEL",
    "INVARIANTS",
    "INVARIANT_LABELS",
    "NOT_APPLICABLE",
    "OUTPUT_KINDS",
    "OUTPUT_KIND_LABELS",
    "PASS",
    "PERIOD_BASIS",
    "RELATIVE_TOLERANCE",
    "RULE_REF",
    "SCHEMA_VERSION",
    "SEGMENT_SUM",
    "SENSITIVITY_PROJECTION",
    "SOLVER_BOUNDS",
    "UNAVAILABLE",
    "VALUATION_SNAPSHOT",
    "EconomicInvariantAuthority",
    "EconomicInvariantConflict",
    "EconomicInvariantError",
    "EconomicInvariantRefused",
    "EconomicInvariantValidationError",
    "InvariantReport",
    "InvariantResult",
    "check",
    "evaluate",
    "evaluate_forecast_model",
    "evaluate_projection",
    "evaluate_valuation",
    "forecast_subject",
    "gate",
    "measure_band",
    "projection_subject",
    "segment_groups",
    "valuation_subject",
]
