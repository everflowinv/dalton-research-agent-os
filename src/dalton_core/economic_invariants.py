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
from .driver_template import COST_REGISTRY_HASH
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

# These are the dimensional axes whose members describe mutually exclusive
# operating/geographic parts of a reported total.  Other XBRL axes commonly
# found beside statement facts are not additive: consolidation axes distinguish
# parent/non-controlling views, equity-component axes describe a roll-forward,
# and class-of-stock or measurement-basis axes describe attributes.  Treating
# those members as segments rejects sound statements and, worse, gives their
# sum an economic meaning the filing never asserted.
ADDITIVE_SEGMENT_AXES = frozenset({
    "srt:SegmentAxis",
    "srt:StatementBusinessSegmentsAxis",
    "us-gaap:StatementBusinessSegmentsAxis",
    "srt:StatementGeographicalAxis",
    "us-gaap:StatementGeographicalAxis",
    "srt:ProductOrServiceAxis",
    "us-gaap:ProductOrServiceAxis",
})
# Geographic axes carry two granularities side by side.  Meta's 10-K
# (accession 0001628280-26-003942) files revenue on srt:StatementGeographicalAxis
# as United States & Canada 52,888, Europe 31,210, Asia-Pacific 36,154 and Rest
# of World 14,650 -- which add to the consolidated 134,902 for 2023 -- and also
# ``country:US`` 49,780, the United States inside "United States & Canada".
# Summing all five gave 184,682 and failed every forecast of META since
# 2026-09-25.  An ISO country member beside regional members is therefore
# either a disjoint part ("United States" + "International") or an "of which"
# inside one region; both readings are checked exactly and neither is assumed.
GEOGRAPHIC_AXES = frozenset({
    "srt:StatementGeographicalAxis",
    "us-gaap:StatementGeographicalAxis",
})
COUNTRY_MEMBER_PREFIX = "country:"

# Operating segments may leave out company-level amounts. A disclosed gap
# must reconcile exactly; declared missing members are findings, never an
# exemption from the publication gate.
BUSINESS_SEGMENT_AXES = frozenset({
    "srt:SegmentAxis",
    "srt:StatementBusinessSegmentsAxis",
    "us-gaap:StatementBusinessSegmentsAxis",
})
CONSOLIDATION_ITEMS_AXES = frozenset({
    "srt:ConsolidationItemsAxis",
    "us-gaap:ConsolidationItemsAxis",
})
RECONCILING_MEMBERS = frozenset({
    "us-gaap:CorporateNonSegmentMember",
    "us-gaap:SegmentReconcilingItemsMember",
    "us-gaap:MaterialReconcilingItemsMember",
    "us-gaap:IntersegmentEliminationMember",
    "srt:ConsolidationEliminationsMember",
    "us-gaap:ConsolidationEliminationsMember",
})
# A bridge combines segment revenue/costs, each member's filed operating
# income, and company-level operating income. Multi-dimensional facts retain
# their full context identity. Amounts are never assigned to members by size
# or by a coincidentally matching margin.
ALL_OTHER_MEMBERS = frozenset({"us-gaap:AllOtherSegmentsMember"})
OPERATING_SEGMENT_MEMBERS = frozenset({"us-gaap:OperatingSegmentsMember"})
OPERATING_INCOME_CONCEPT = "us-gaap:OperatingIncomeLoss"

# Durable identity for the closed semantics used to admit a forecast. This is
# deliberately narrower than a source-code or release hash: only a change to
# the validator contract may release a run held by a previous invariant
# refusal. Version 2 distinguishes proven one-dimensional additive segment
# axes from lossy legacy/multi-dimensional XBRL projections. Derive the wire
# list from the evaluator's actual allowlist so the declaration cannot drift.
# Version 3 also admits the strictly validated cost-slot driver wire. Version
# 5 binds the structured filing-proof unit projection: SEC currency-per-share
# spellings are normalized exactly as model inputs are, while legacy replay
# retains its byte-exact unit comparison. Version 6 makes cumulative filing
# proof independent of statement-row order while retaining exact arithmetic.
# Version 7 applies the direction check only where operating income is
# provably revenue times a constant: a cost line carried on its own growth
# rate no longer counts as a "held cost ratio".
# Version 8 reads an ISO ``country:`` member beside regional members on one
# geographic axis as a possible "of which" country (see
# ``GEOGRAPHIC_AXES``), not as a sixth disjoint region.
# Versions 9/10 admitted declared incomplete segment bridges as uncovered.
# Version 11 requires every disclosed bridge to close, reports differences
# and missing members, and maps operating income by context identity only.
FORECAST_INVARIANT_CONTRACT_REF = "forecast-economic-invariants:11"
FORECAST_INVARIANT_CONTRACT = {
    "schema_version": "forecast-economic-invariant-contract-0.1",
    "contract_ref": FORECAST_INVARIANT_CONTRACT_REF,
    "segment_sum": {
        "additive_axes": sorted(ADDITIVE_SEGMENT_AXES),
        "required_dimension_count": 1,
        "members_must_be_unique": True,
        "geographic_country_members": "partition-or-nested-in-region:v1",
        "unallocated_residual": (
            "declared-reconciling-items-or-fail-with-gap-and-missing-members:v2"),
        "reconciling_members": sorted(RECONCILING_MEMBERS),
        "segment_bridge": (
            "segments+all-other+corporate-non-segment=group-by-member-identity-or-fail:v2"),
        "all_other_members": sorted(ALL_OTHER_MEMBERS),
        "member_mapping": "complete-context-identity-only:no-value-ranking:v1",
    },
    # The model validator is part of admission just as the economic checks
    # are. Version the optional cost-slot wire here so a run refused by the
    # former closed shape receives a bounded retry without changing the
    # successful forecast generator or model digest.
    "driver_wire": {
        "cost_driver_slots": "optional_nonempty_unique_frozen_registry_slots:v1",
        "cost_registry_hash": COST_REGISTRY_HASH,
    },
    "cash_flow_companion_validation": (
        "cash-flow-companion-pre-persistence-validation:0.2"
    ),
    "filing_proof_units": "structured-sec-per-share-normalization:v1",
    "cumulative_pair_selection": "all-exact-adjacent-pairs:v1",
    "direction_premise": "revenue-proportional-operating-income:v1",
}
FORECAST_INVARIANT_CONTRACT_HASH = content_hash(FORECAST_INVARIANT_CONTRACT)
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
# The measures under which a margin-role assumption *is* a share. A constant
# ``quarterly_growth`` on a cost line is a constant assumption value but not a
# held share of revenue, so it cannot stand in for "every cost ratio held".
# ``share_of_line`` is a share of whatever base the structure names; whether
# that base is itself proportional to revenue is settled by the structure walk.
_REVENUE_SHARE_MEASURES = frozenset({"revenue_share", "share_of_line"})


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
            not_checked = [item.invariant for item in self.results
                           if item.status == NOT_APPLICABLE]
            if not_checked:
                return (
                    f"{self.output_ref} has no economic-invariant failure; "
                    f"not checked: {', '.join(not_checked)}"
                )
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
            if (prior.get("revenue_proportional") is False
                    or current.get("revenue_proportional") is False):
                # The chain above holds only when every cost line is a share of
                # revenue. A cost carried forward on its own growth rate (a
                # "fixed" D&A line, say) keeps its assumption *value* constant
                # while its share of revenue moves every quarter, so the
                # operating margin legitimately drifts -- operating leverage,
                # not a broken chain. Neither the proportion nor the direction
                # follows from the formula then, so there is nothing to check.
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
    """Every disclosed partition or bridge must close, or fail explicitly."""

    findings: list[str] = []
    checked = 0
    pending: list[tuple[Mapping[str, Any], Decimal, Decimal, int]] = []
    with localcontext() as ctx:
        ctx.prec = _PRECISION
        for group in groups:
            total = _decimal(group.get("total"))
            parts = [(_decimal(part.get("value")), str(part.get("member")))
                     for part in (group.get("parts") or [])]
            usable = [(value, member) for value, member in parts if value is not None]
            if total is None or not parts:
                continue
            summed = sum((value for value, _ in usable), Decimal(0))
            if len(usable) != len(parts):
                checked += 1
                missing = sorted(member for value, member in parts if value is None)
                findings.append(f"{group.get('line')} for {group.get('period')}: "
                                f"missing values for members {', '.join(missing)}; "
                                f"known segments add to {summed}; difference {total - summed}")
                continue
            bridge = group.get("bridge")
            if isinstance(bridge, Mapping):
                assessment = _segment_bridge_assessment(group)
                checked += 1
                if assessment["status"] != PASS:
                    findings.append(f"{group.get('line')} for {group.get('period')} on axis "
                                    f"{group.get('axis')}: {len(usable)} segments add to {summed} "
                                    f"and the consolidated line is {total}; difference {total - summed}; "
                                    + "; ".join(assessment["findings"]))
                continue
            if (_close(summed, total) or _nested_countries_close(group, usable, total)
                    or _reconciled(group, summed, total)):
                checked += 1
                continue
            pending.append((group, summed, total, len(usable)))
        for group, summed, total, count in pending:
            checked += 1
            why = _incomplete_members(group)
            reconciliation = sum((_decimal(item.get("value")) or Decimal(0)
                                  for item in group.get("reconciling_items") or ()), Decimal(0))
            missing = why or "missing members: not identified in the disclosed context"
            findings.append(
                f"{group.get('line')} for {group.get('period')} on axis "
                f"{group.get('axis')}: {count} segments add to {summed} "
                f"and the consolidated line is {total}; difference {total - summed}; "
                f"unreconciled difference {total - summed - reconciliation}; {missing}")
    if findings:
        return InvariantResult(SEGMENT_SUM, FAIL, findings=tuple(findings), checked=checked)
    if not checked:
        return InvariantResult(SEGMENT_SUM, NOT_APPLICABLE, checked=0,
                               reason="no line has both a consolidated figure and a breakdown to check it against")
    return InvariantResult(SEGMENT_SUM, PASS, checked=checked)


def _reconciled(group: Mapping[str, Any], summed: Decimal, total: Decimal) -> bool:
    """The gap is one declared reconciling amount, or all of them together."""

    items = [value for value in (_decimal(item.get("value"))
                                 for item in (group.get("reconciling_items") or []))
             if value is not None]
    if not items:
        return False
    if any(_close(summed + value, total) for value in items):
        return True
    return len(items) > 1 and _close(summed + sum(items, Decimal(0)), total)


def _incomplete_members(group: Mapping[str, Any]) -> str | None:
    """Why the filer's own declarations say this breakdown is not the whole."""

    head = (f"{group.get('line')} for {group.get('period')} on axis "
            f"{group.get('axis')}")
    missing = [str(item) for item in (group.get("undeclared_members") or [])]
    if missing:
        return (f"{head} omits member(s) {', '.join(sorted(missing))} the "
                "filer declared on that axis for the same period")
    # A reconciling amount filed for this very line is the filer's own
    # account of what lies outside the segments; when it does not close the
    # gap that is a contradiction, not a reason to stop checking.
    if (str(group.get("axis")) in BUSINESS_SEGMENT_AXES
            and not group.get("reconciling_items")):
        non_segment = [str(item) for item in (group.get("non_segment_members") or [])]
        if non_segment:
            return (f"{head} leaves out amounts the filer reports for the same "
                    f"period outside any segment ({', '.join(sorted(non_segment))})")
    return None


def _segment_bridge_assessment(group: Mapping[str, Any]) -> dict[str, Any]:
    """Check identities and arithmetic; anonymous amounts are never assigned."""

    bridge = group.get("bridge") or {}
    findings: list[str] = []

    def amounts(parts: Any, label: str) -> dict[str, Decimal]:
        out: dict[str, Decimal] = {}
        for part in parts or ():
            if not isinstance(part, Mapping):
                findings.append(f"{label}: missing member identity")
                continue
            value = _decimal(part.get("value"))
            member = str(part.get("member") or "")
            if not member or value is None:
                findings.append(f"{label}: missing member identity or value ({member or 'unknown'})")
            elif member in out:
                findings.append(f"{label}: duplicate member {member}")
            else:
                out[member] = value
        return out

    with localcontext() as ctx:
        ctx.prec = _PRECISION
        revenue = amounts(bridge.get("revenue_parts"), "segment revenue")
        costs = amounts(bridge.get("cost_parts"), "segment costs")
        segments = amounts(bridge.get("segment_operating_income"), "segment operating income")
        corporate = amounts(bridge.get("corporate_operating_income"), "corporate operating income")
        r_total, c_total, operating = (_decimal(bridge.get(key)) for key in
                                     ("revenue_total", "cost_total", "operating_income"))
        missing = sorted((set(revenue) | set(costs)) - set(segments))
        if missing:
            findings.append("missing operating income members: " + ", ".join(missing))
        extra = sorted(set(segments) - set(revenue))
        if extra:
            findings.append("missing revenue members: " + ", ".join(extra))
        if bridge.get("unmapped_operating_income"):
            findings.append("operating income facts lack segment identity; value matching is prohibited")
        if not corporate:
            findings.append("missing corporate member: us-gaap:CorporateNonSegmentMember")
        if None in (r_total, c_total, operating):
            findings.append("missing consolidated revenue, costs or operating income")
        else:
            difference = r_total - c_total - operating
            if not _close(r_total - c_total, operating):
                findings.append(f"group revenue minus costs differs from operating income by {difference}")
            income_gap = operating - sum(segments.values(), Decimal(0)) - sum(corporate.values(), Decimal(0))
            if not _close(sum(segments.values(), Decimal(0)) + sum(corporate.values(), Decimal(0)), operating):
                findings.append(f"segment plus corporate operating income difference {income_gap}")
        matched: dict[str, str] = {}
        derived: dict[str, str] = {}
        for member in sorted(set(revenue) & set(costs) & set(segments)):
            difference = revenue[member] - costs[member] - segments[member]
            if not _close(revenue[member] - costs[member], segments[member]):
                findings.append(f"member {member}: revenue minus costs differs from its operating income by {difference}")
            matched[member] = format(segments[member], "f")
        for member in sorted(set(revenue) - set(costs)):
            if member in ALL_OTHER_MEMBERS and member in segments:
                derived[member] = format(revenue[member] - segments[member], "f")
            else:
                findings.append(f"missing cost member: {member}")
        for member in sorted(set(costs) - set(revenue)):
            findings.append(f"missing revenue member: {member}")
        if findings:
            return {"status": FAIL, "findings": findings}
        corporate_total = sum(corporate.values(), Decimal(0))
        residual_revenue = r_total - sum(revenue.values(), Decimal(0))
        residual_costs = c_total - sum(costs.values(), Decimal(0)) - sum((Decimal(v) for v in derived.values()), Decimal(0))
        return {"status": PASS, "findings": [], "bridge": {
            "operating_income": format(operating, "f"),
            "segment_operating_income_matched": matched,
            "derived_all_other_costs": derived,
            "corporate_operating_income": format(corporate_total, "f"),
            "corporate_revenue_minus_costs": format(residual_revenue - residual_costs, "f"),
        }}


def segment_bridge(group: Mapping[str, Any]) -> dict[str, Any] | None:
    """The closed bridge, mapped by filed member identity; else None."""

    if not isinstance(group.get("bridge"), Mapping):
        return None
    return _segment_bridge_assessment(group).get("bridge")


def _nested_countries_close(
    group: Mapping[str, Any], usable: Sequence[tuple[Decimal, str]], total: Decimal,
) -> bool:
    """The regional members alone add to the whole, the countries nest inside.

    Only on a geographic axis that mixes ISO ``country:`` members with at
    least two regional members, only when the regions add to the
    consolidated line exactly as any partition must, and only when every
    country is no larger than some region that could hold it.  Anything else
    is still the failure it was.
    """

    if str(group.get("axis")) not in GEOGRAPHIC_AXES:
        return False
    countries = [value for value, member in usable
                 if member.startswith(COUNTRY_MEMBER_PREFIX)]
    regions = [value for value, member in usable
               if not member.startswith(COUNTRY_MEMBER_PREFIX)]
    if not countries or len(regions) < 2:
        return False
    if not _close(sum(regions, Decimal(0)), total):
        return False
    largest = max(regions)
    return all(value <= largest for value in countries)


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
    # What the filer itself declared for each period, whatever the line: the
    # members of each additive axis, and the reconciling (non-segment) amounts
    # on the consolidation-items axis.  See ``RECONCILING_MEMBERS``.
    axis_members: dict[tuple[str, str, str], set[str]] = {}
    reconciling: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    non_segment: dict[tuple[str, str], set[str]] = {}
    # Operating income per segment retains its complete identity, including
    # an optional operating-segments consolidation marker.
    segment_income: dict[tuple[str, str], list[dict[str, Any]]] = {}
    unmapped_income: dict[tuple[str, str], list[Any]] = {}
    for row in rows:
        concept = str(row.get("concept") or "")
        end = str(row.get("period_end") or "")
        start = str(row.get("period_start") or "")
        if not concept or not end:
            continue
        axis = row.get("dimension_axis")
        key = (concept, start, end)
        member = str(row.get("dimension_member") or "")
        dimensions = row.get("dimensions")
        operating_member = None
        if isinstance(dimensions, Mapping):
            business = [(a, m) for a, m in dimensions.items() if a in BUSINESS_SEGMENT_AXES]
            other = {a: m for a, m in dimensions.items() if a not in BUSINESS_SEGMENT_AXES}
            if (len(business) == 1 and len(other) <= 1
                    and all(a in CONSOLIDATION_ITEMS_AXES and m in OPERATING_SEGMENT_MEMBERS
                            for a, m in other.items())
                    and row.get("dimension_count") == len(dimensions)):
                operating_member = str(business[0][1])
        elif axis in BUSINESS_SEGMENT_AXES and row.get("dimension_count") == 1:
            operating_member = member
        if operating_member and isinstance(dimensions, Mapping):
            axis = next(a for a in dimensions if a in BUSINESS_SEGMENT_AXES)
            member = operating_member
        effective_count = 1 if operating_member else row.get("dimension_count")
        if concept == OPERATING_INCOME_CONCEPT and start and operating_member:
            item = {"member": operating_member, "value": row.get("value")}
            if item not in segment_income.setdefault((start, end), []):
                segment_income[(start, end)].append(item)
        if not axis and not row.get("is_breakdown"):
            consolidated[key] = row.get("value")
        elif (start and axis and str(axis) in ADDITIVE_SEGMENT_AXES
              and effective_count == 1):
            parts.setdefault((*key, str(axis)), []).append({
                "member": member,
                "value": row.get("value"),
            })
            if member:
                axis_members.setdefault((start, end, str(axis)), set()).add(member)
        elif (start and axis and str(axis) in CONSOLIDATION_ITEMS_AXES
              and member in RECONCILING_MEMBERS
              and row.get("dimension_count") == 1):
            reconciling.setdefault(key, []).append(
                {"member": member, "value": row.get("value")})
            non_segment.setdefault((start, end), set()).add(member)
        elif (start and axis and str(axis) in CONSOLIDATION_ITEMS_AXES
              and member in OPERATING_SEGMENT_MEMBERS and concept == OPERATING_INCOME_CONCEPT
              and row.get("dimension_count") == 2):
            if operating_member is None:
                unmapped_income.setdefault((start, end), []).append(row.get("value"))
    out: list[dict[str, Any]] = []
    for (concept, start, end, axis), members in sorted(parts.items()):
        if (concept, start, end) not in consolidated:
            continue
        # The normalized statement shape carries one axis/member pair.  A raw
        # context can carry more dimensions, so the same member may survive
        # more than once after that projection.  Such rows are not a complete
        # one-dimensional partition and cannot honestly be summed.  Refuse to
        # manufacture a finding from the lossy projection; a unique-member
        # business/geography/product series remains fully checked below.
        member_names = [str(item.get("member") or "") for item in members]
        if not all(member_names) or len(set(member_names)) != len(member_names):
            continue
        group: dict[str, Any] = {
            "line": concept,
            "period": f"{start}..{end}" if start else end,
            "axis": axis,
            "total": consolidated[(concept, start, end)],
            "parts": sorted(members, key=lambda item: item["member"]),
        }
        # Only present when there is something to say, so a subject whose
        # filer declared none of these is byte-for-byte what it was.
        undeclared = sorted(
            axis_members.get((start, end, axis), set()) - set(member_names))
        if undeclared:
            group["undeclared_members"] = undeclared
        items = reconciling.get((concept, start, end))
        if items:
            group["reconciling_items"] = sorted(
                items, key=lambda item: (item["member"], str(item["value"])))
        if axis in BUSINESS_SEGMENT_AXES and non_segment.get((start, end)):
            group["non_segment_members"] = sorted(non_segment[(start, end)])
        out.append(group)
    _attach_bridges(out, consolidated, reconciling, segment_income, unmapped_income)
    return out


# These are statement concepts, not a ranking of amounts. Unknown company
# concepts remain unbridged until their statement identity is available.
_BRIDGE_REVENUE_CONCEPTS = frozenset({
    "us-gaap:Revenues", "us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax",
    "us-gaap:RevenueFromContractWithCustomerIncludingAssessedTax", "us-gaap:SalesRevenueNet",
})
_BRIDGE_COST_CONCEPTS = frozenset({"us-gaap:CostsAndExpenses"})


def _attach_bridges(groups: list[dict[str, Any]], consolidated: Mapping[tuple[str, str, str], Any],
                    reconciling: Mapping[tuple[str, str, str], Sequence[Mapping[str, Any]]],
                    segment_income: Mapping[tuple[str, str], Sequence[Mapping[str, Any]]],
                    unmapped_income: Mapping[tuple[str, str], Sequence[Any]]) -> None:
    """Attach attempts by statement identity even when their arithmetic fails."""

    by_period: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for group in groups:
        if str(group.get("axis")) in BUSINESS_SEGMENT_AXES and ".." in str(group.get("period")):
            by_period.setdefault((str(group["period"]), str(group["axis"])), []).append(group)
    for (period, _axis), members in sorted(by_period.items()):
        start, end = period.split("..", 1)
        revenues = [g for g in members if g["line"] in _BRIDGE_REVENUE_CONCEPTS]
        costs = [g for g in members if g["line"] in _BRIDGE_COST_CONCEPTS]
        # Distinct presentations cannot be paired by whichever numbers happen
        # to agree; leave their own partitions to the ordinary sum check.
        if len(revenues) != 1 or len(costs) != 1:
            continue
        revenue, cost = revenues[0], costs[0]
        corporate = [dict(item) for item in reconciling.get((OPERATING_INCOME_CONCEPT, start, end)) or ()
                     if item.get("member") == "us-gaap:CorporateNonSegmentMember"]
        if not (corporate or segment_income.get((start, end)) or unmapped_income.get((start, end))):
            continue
        bridge = {
            "kind": "operating_income",
            "revenue_line": revenue["line"], "revenue_total": revenue["total"],
            "revenue_parts": [dict(item) for item in revenue["parts"]],
            "cost_line": cost["line"], "cost_total": cost["total"],
            "cost_parts": [dict(item) for item in cost["parts"]],
            "operating_income": consolidated.get((OPERATING_INCOME_CONCEPT, start, end)),
            "segment_operating_income": sorted((dict(item) for item in segment_income.get((start, end)) or ()),
                                                key=lambda item: item["member"]),
            "unmapped_operating_income": list(unmapped_income.get((start, end)) or ()),
            "corporate_operating_income": corporate,
        }
        revenue["bridge"] = bridge
        cost["bridge"] = bridge


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


def _operating_income_revenue_proportional(record: Mapping[str, Any]) -> bool | None:
    """Whether a structure-backed model's operating income is revenue times a constant.

    ``None`` when the record carries no statement structure (the legacy chain,
    whose cost lines are all ``revenue_share`` by construction) or names no
    operating-income line. Otherwise walk the structure: revenue is the anchor;
    a ``share_of_line`` line is proportional when its base is; a ``sum``
    formula is proportional when every term is. Anything else -- a line carried
    on its own ``quarterly_growth``, a ratio formula, an unknown line -- is not,
    and one such term makes operating income not proportional to revenue.
    """

    structure = record.get("financial_statement_structure")
    if not isinstance(structure, Mapping):
        return None
    lines = {str(line.get("ref")): line for line in (structure.get("lines") or [])
             if isinstance(line, Mapping) and line.get("ref") is not None}
    formulas = {str(item.get("output_ref")): item
                for item in (structure.get("formulas") or [])
                if isinstance(item, Mapping) and item.get("output_ref") is not None}
    target = next((ref for ref, line in lines.items()
                   if line.get("role") == "operating_income"), None)
    if target is None:
        return None
    memo: dict[str, bool] = {}

    def proportional(ref: str, trail: frozenset[str]) -> bool:
        if ref in memo:
            return memo[ref]
        if ref in trail:
            return False
        line = lines.get(ref)
        result = False
        if line is not None:
            method = line.get("forecast_method")
            if line.get("role") == "revenue":
                result = True
            elif method == "share_of_line":
                result = proportional(str(line.get("forecast_base_ref")), trail | {ref})
            elif method == "formula":
                formula = formulas.get(ref) or line.get("formula")
                if isinstance(formula, Mapping) and formula.get("operator") == "sum":
                    terms = formula.get("terms") or []
                    result = bool(terms) and all(
                        isinstance(term, Mapping)
                        and proportional(str(term.get("line_ref")), trail | {ref})
                        for term in terms)
        memo[ref] = result
        return result

    return proportional(target, frozenset())


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
    # Quarters in which some cost line is not a share of revenue: its
    # assumption can stay constant (a growth rate) while its share moves.
    not_shares: set[str] = set()
    for item in live:
        role = roles.get(str(item["driver_ref"]))
        if role in _MARGIN_ROLES:
            end = str(item["period"]["end"])
            ratios.setdefault(end, []).append(
                (str(item["driver_ref"]), str(item["value"])))
            if str(item.get("measure")) not in _REVENUE_SHARE_MEASURES:
                not_shares.add(end)
    structural = _operating_income_revenue_proportional(record)
    ends = [str(item["end"]) for item in (record.get("forecast_periods") or [])]
    direction_series = [{
        "period_end": end,
        "revenue": revenue.get(end),
        "operating_income": operating.get(end),
        "ratios": sorted(ratios.get(end, [])),
        "revenue_proportional": structural is not False and end not in not_shares,
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


    def record_version(self, report: InvariantReport) -> dict[str, Any]:
        """Append the report for the exact output version it judged.

        Pass or refusal alike: this ledger is what "the verdict for this
        version" means.  The same report for the same version is one row.
        """

        if not isinstance(report, InvariantReport):
            raise EconomicInvariantValidationError(
                "a version verdict is recorded from an InvariantReport")
        record = {**report.as_dict(), "verdict_scope": "exact_version",
                  "created_at": _now()}
        record["content_hash"] = content_hash(
            {key: value for key, value in record.items() if key != "created_at"})
        with self._transaction() as cur:
            cur.execute(
                "INSERT OR IGNORE INTO economic_invariant_version_verdicts"
                "(output_ref,rule_ref,output_kind,company_ref,status,subject_hash,"
                "record_json,content_hash,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (report.output_ref, report.rule_ref, report.output_kind,
                 report.company_ref, report.status, report.subject_hash or "",
                 canonical_json(record), record["content_hash"], record["created_at"]))
        return record


# ---------------------------------------------------------------------------
# batch an: verdicts by exact version, and what a reader may call "covered"
# ---------------------------------------------------------------------------

#: How one rule's result reads.  ``not_applicable`` or ``checked == 0`` is
#: 「未覆盖」 -- the rule had nothing to look at -- and never 「通过」.
COVERAGE_PASS = "pass"
COVERAGE_FAIL = "fail"
COVERAGE_UNCOVERED = "uncovered"
COVERAGE_LABELS: dict[str, str] = {
    COVERAGE_PASS: "通过", COVERAGE_FAIL: "未通过", COVERAGE_UNCOVERED: "未覆盖",
}
#: A published output whose exact version has no verdict.
NO_VERDICT_LABEL = "该版本没有经济不变量检查结论（未验证）"


def coverage_status(result: Mapping[str, Any]) -> str:
    """``pass`` / ``fail`` / ``uncovered`` for one rule's stored result."""

    status = str(result.get("status") or "")
    if status == FAIL:
        return COVERAGE_FAIL
    try:
        checked = int(result.get("checked") or 0)
    except (TypeError, ValueError):
        checked = 0
    if status == NOT_APPLICABLE or checked <= 0:
        return COVERAGE_UNCOVERED
    return COVERAGE_PASS if status == PASS else COVERAGE_UNCOVERED


def _ro_table(connection: Any, name: str) -> bool:
    try:
        return connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone() is not None
    except sqlite3.Error:
        return False


def _loads(text: Any) -> dict[str, Any] | None:
    try:
        value = json.loads(text)
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def verdict_for_output(connection: Any, output_ref: str) -> dict[str, Any] | None:
    """The stored verdict for exactly ``output_ref``, or ``None``.

    In order: the exact-version ledger; a refusal-chain row naming this
    output; the invariant report a forecast model version's filing proof
    carries.  A verdict on any *other* version of the same output is not
    returned -- it covers that version, not this one.
    """

    if _ro_table(connection, "economic_invariant_version_verdicts"):
        row = connection.execute(
            "SELECT record_json FROM economic_invariant_version_verdicts WHERE output_ref=? "
            "ORDER BY created_at DESC LIMIT 1", (output_ref,)).fetchone()
        if row is not None:
            record = _loads(row[0])
            if record is not None:
                return {**record, "verdict_source": "exact_version"}
    if _ro_table(connection, "economic_invariant_verdicts"):
        row = connection.execute(
            "SELECT record_json FROM economic_invariant_verdicts WHERE output_ref=? "
            "ORDER BY version_number DESC LIMIT 1", (output_ref,)).fetchone()
        if row is not None:
            record = _loads(row[0])
            if record is not None:
                return {**record, "verdict_source": "refusal_chain"}
    if _ro_table(connection, "forecast_model_filing_proofs"):
        row = connection.execute(
            "SELECT record_json FROM forecast_model_filing_proofs WHERE model_version_id=?",
            (output_ref,)).fetchone()
        proof = None if row is None else _loads(row[0])
        report = (proof or {}).get("invariant_report")
        if isinstance(report, Mapping) and report.get("output_ref") in (None, output_ref):
            return {**dict(report), "output_ref": output_ref, "verdict_source": "filing_proof"}
    return None


def publication_gate(connection: Any, output_ref: str) -> dict[str, Any]:
    """May ``output_ref`` be shown as a current result?

    Only with a verdict for this exact version that is not a refusal.  No
    verdict is ``unverified`` (shown as 「未验证」), never a pass.
    """

    verdict = verdict_for_output(connection, output_ref)
    if verdict is None:
        return {"output_ref": output_ref, "publishable": False, "status": "unverified",
                "label": NO_VERDICT_LABEL}
    if verdict.get("status") == UNAVAILABLE:
        return {"output_ref": output_ref, "publishable": False, "status": "refused",
                "label": "该版本未通过经济不变量检查，没有发布",
                "verdict_source": verdict.get("verdict_source")}
    uncovered = [str(item.get("invariant")) for item in verdict.get("results") or ()
                 if isinstance(item, Mapping) and coverage_status(item) == COVERAGE_UNCOVERED]
    return {"output_ref": output_ref, "publishable": True, "status": "verified",
            "label": ("该版本已检查" + (f"；{len(uncovered)} 项规则未覆盖" if uncovered else "")),
            "uncovered": uncovered, "verdict_source": verdict.get("verdict_source")}


def rule_coverage(verdict: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    """Per rule: checked count, whether it applied, and how it reads."""

    by_rule = {str(item.get("invariant")): item for item in (verdict or {}).get("results") or ()
               if isinstance(item, Mapping)}
    out = []
    for name in INVARIANTS:
        item = by_rule.get(name)
        if item is None:
            out.append({"invariant": name, "label": INVARIANT_LABELS[name], "checked": 0,
                        "applicable": False, "status": None,
                        "coverage": COVERAGE_UNCOVERED,
                        "display_status": COVERAGE_LABELS[COVERAGE_UNCOVERED],
                        "reason": None if verdict else NO_VERDICT_LABEL})
            continue
        coverage = coverage_status(item)
        try:
            checked = int(item.get("checked") or 0)
        except (TypeError, ValueError):
            checked = 0
        out.append({"invariant": name, "label": INVARIANT_LABELS[name], "checked": checked,
                    "applicable": item.get("status") != NOT_APPLICABLE and checked > 0,
                    "status": item.get("status"), "coverage": coverage,
                    "display_status": COVERAGE_LABELS[coverage],
                    "reason": item.get("reason")})
    return out


#: output kind -> (table, id column, chain column, company column)
_OUTPUT_TABLES: dict[str, tuple[str, str, str, str]] = {
    FORECAST_MODEL: ("forecast_model_versions", "version_id", "model_ref", "company_ref"),
    SENSITIVITY_PROJECTION: ("sensitivity_projections", "projection_id", "projection_ref",
                             "company_ref"),
}


def coverage_report(connection: Any, company_ref: str | None = None) -> dict[str, dict[str, Any]]:
    """company -> output kind -> the current version's per-rule coverage.

    Read for the *current* version of each output only: a verdict on an older
    model is reported as such (``verdict_for_current`` false) and its rules
    read 「未覆盖」 for the current version.
    """

    out: dict[str, dict[str, Any]] = {}
    for kind, (table, id_col, chain_col, company_col) in _OUTPUT_TABLES.items():
        if not _ro_table(connection, table):
            continue
        sql = (f"SELECT t.{id_col} AS id, t.{company_col} AS company_ref, t.created_at AS created_at "
               f"FROM {table} t JOIN (SELECT {chain_col} AS chain, MAX(version_number) AS top "
               f"FROM {table} GROUP BY {chain_col}) h ON h.chain=t.{chain_col} "
               "AND h.top=t.version_number")
        params: tuple[Any, ...] = ()
        if company_ref is not None:
            sql += f" WHERE t.{company_col}=?"
            params = (company_ref,)
        for row in connection.execute(sql, params).fetchall():
            current = str(row[0])
            verdict = verdict_for_output(connection, current)
            older = None
            if verdict is None and _ro_table(connection, "economic_invariant_verdicts"):
                prior = connection.execute(
                    "SELECT output_ref FROM economic_invariant_verdicts WHERE company_ref=? "
                    "AND output_kind=? ORDER BY version_number DESC LIMIT 1",
                    (row[1], kind)).fetchone()
                older = None if prior is None else prior[0]
            rules = rule_coverage(verdict)
            applicable = [item for item in rules if item["applicable"]]
            failed = [item for item in rules if item["coverage"] == COVERAGE_FAIL]
            gate = publication_gate(connection, current)
            out.setdefault(str(row[1]), {})[kind] = {
                "output_kind": kind, "output_kind_label": OUTPUT_KIND_LABELS[kind],
                "output_ref": current, "verdict_for_current": verdict is not None,
                "verdict_source": None if verdict is None else verdict.get("verdict_source"),
                "older_verdict_output_ref": older,
                "publication": gate,
                "rules": rules,
                "checked_rules": len(applicable), "total_rules": len(rules),
                "failed_rules": len(failed),
                "display_summary": (
                    NO_VERDICT_LABEL if verdict is None else
                    f"{len(applicable)}/{len(rules)} 项规则有可检查的数据；其余显示为未覆盖，不算通过"),
            }
    return out


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
        authority = EconomicInvariantAuthority(store)
        stored = authority.record(
            report, actor_ref=actor_ref, mission_version_ref=mission_version_ref)
        # Batch an: the exact version's verdict, pass or refusal.
        authority.record_version(report)
    if report.failures:
        raise EconomicInvariantRefused(report)
    return stored


__all__ = [
    "COVERAGE_FAIL", "COVERAGE_LABELS", "COVERAGE_PASS", "COVERAGE_UNCOVERED", "NO_VERDICT_LABEL",
    "coverage_report", "coverage_status", "publication_gate", "rule_coverage", "verdict_for_output",
    "ABSOLUTE_TOLERANCE",
    "ACTOR_REF",
    "AVAILABLE",
    "BAND",
    "DIRECTION",
    "DOMAIN",
    "FAIL",
    "FORECAST_MODEL",
    "FORECAST_INVARIANT_CONTRACT_HASH",
    "FORECAST_INVARIANT_CONTRACT_REF",
    "FORECAST_INVARIANT_CONTRACT",
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
    "segment_bridge", "segment_groups",
    "valuation_subject",
]
