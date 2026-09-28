"""FY − 9M: the fourth quarter a 10-K reports only inside its fiscal year.

Most issuers' 10-Ks carry the fiscal year and nothing shorter (AMZN, GOOGL,
META, MSFT, IBM, EPAM, CTSH, DXC), so the same-accession annual rule
(``research-auto-commit:sec-public-company-facts-growth-annual:v1``) has no
quarter to compare and the newest fourth quarter never had a growth Claim.
This module is the rule that answers it by arithmetic on filed rows:

    Q4            = fiscal-year revenue (10-K) − nine months (10-Qs)
    Q4, prior     = the same, for the fiscal year before
    growth        = (Q4 / Q4 prior − 1) × 100

Rule ``research-auto-commit:sec-statement-line-growth-fy-minus-9m:v1``.  It
is named for where its inputs come from -- the filed XBRL statement rows the
financial-statements lane recorded in Core (``coverage_mission_statement_*``),
each under a settled, mission-authorised dispatch and an approved
connector-governance record -- and for the growth family it belongs to.  No
connector is called and no model is asked: the whole candidate is a pure
function of Core rows, built once to stage it and again, byte for byte, by the
auto-commit evaluator before the Ledger will take it.

What the rule requires (every failure is a refusal with its reason; nothing is
guessed around):

* **One concept, one basis.**  The revenue concept is the first of
  ``DEFAULT_REVENUE_CONCEPT_CANDIDATES`` that the 10-K files for its fiscal
  year.  Every component is that concept, consolidated (no dimension), in
  US dollars (unit id ``usd`` or ``U_USD``, both iso4217:USD).  A period
  filed only under another concept is a concept change and refuses the
  derivation.
* **Exact fiscal boundaries.**  Nothing is calendar: the fiscal year is the
  10-K's own row ending on its report date (350..380 days, so 52/53-week years
  and June/August/March year ends are the same case).  Q1 starts on the fiscal
  year's first day, Q2 the day after Q1 ends, Q3 the day after Q2, and the
  fourth quarter runs from the day after Q3 to the fiscal year end; each span
  is a quarter (80..100 days, as the quarter lane counts).  The prior fiscal
  year ends the day before this one starts.  A gap, an overlap or two
  candidate periods is a refusal.
* **The first three quarters are held.**  This fiscal year's Q1, Q2 and Q3
  are each reported by their own 10-Q held in Core; the prior year's three
  quarters are held as filed rows (their own 10-Qs or the comparatives the
  newer 10-Qs repeat).  Nine months is the filer's own nine-month
  year-to-date row when one is held, otherwise Q1 + Q2 + Q3.
* **No restatement.**  Every period used is compared across every held filing
  that reports it; two values further apart than half the coarser filed
  precision are a restatement and refuse.  The filed six- and nine-month
  cumulative rows must equal the sum of their quarters within the same
  rounding tolerance, and a fourth quarter any filing reports directly must
  equal the derived one within its bound.  A 10-K that files its own fourth
  quarter is not derived at all: that number is filed, and the annual pair
  rule is the rule for it.

Precision (why the stated digits are what they are).  The statement rows carry
the filer's value as filed, and XBRL revenue is presented in units, thousands
or millions; the row does not carry the ``decimals`` attribute.  A value's
precision is therefore read off the value itself: the largest power of ten
dividing it, capped at 10^6.  That is never finer than the filer's rounding
(a figure presented in millions is a multiple of 10^6), and at worst coarser,
which only widens the bound.  Each filed component is within half its unit of
the unrounded figure, so the derived quarter is exact on the filed values and
within the sum of those half-units of the true one.  The growth is then
bounded by interval arithmetic over both quarters, and it is stated to at most
two decimals -- the same ceiling as the same-accession rule -- and to no more
decimals than that bound supports (bound ≤ half the last stated digit).  A
bound wider than half a percentage point refuses.  The bound, the interval and
every component are written into the material and the sentence.

The Claim says it is derived: ``basis`` is ``official-filing-xbrl-derived``
(not the filed-number basis, so nothing that reads a filed quarter's sentence
back takes it for one), the sentence says "derived as fiscal year less nine
months, not a filed quarter", and the material lists every component row by
filing, accession, line and value, with every other held filing that reported
the same number.
"""

from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Mapping, Sequence
from datetime import date, timedelta
from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP, Decimal
from typing import Any

from .quantitative_claim_promotion import (
    QuantitativeClaimPromotionError,
    SecStatementLineAuthorityResolver,
    _finding,
    _raw_sink,
    _source_records,
    canonical_decimal,
    filing_rows,
    line_projection,
)
from .research_auto_commit import SEC_FY_MINUS_9M_RULE_REF
from .research_verification import (
    SEC_STATEMENT_LINE_AUTHORITY_MODE,
    SEC_STATEMENT_LINE_OPERATION,
    SEC_STATEMENT_LINE_SOURCE_REF,
)
from .store import canonical_json, content_hash

RULE_REF = SEC_FY_MINUS_9M_RULE_REF
PAYLOAD_KIND = "fy_minus_9m_growth"
METRIC = "quarterly_revenue_yoy_growth"
DERIVED_BASIS = "official-filing-xbrl-derived"
QUARTER_DAYS = (80, 100)
YEAR_DAYS = (350, 380)
MAX_DIGITS = 2
MAX_PRECISION_EXPONENT = 6
MAX_BOUND_PP = Decimal("0.5")
PRECISION_BASIS = (
    "each filed value is taken exactly as filed; its precision is the largest power "
    "of ten dividing it, capped at 10^6 (XBRL revenue is presented in units, "
    "thousands or millions and the row carries no decimals attribute), so it is "
    "never finer than the filer's rounding; a component is within half its unit, "
    "the derived quarter within the sum of its components' half-units, the growth "
    "within the interval those bounds give; stated to at most 2 decimals and to no "
    "more decimals than the bound supports (bound <= half the last digit); a bound "
    "over 0.5 percentage points refuses"
)


# The unit a row names is the filer's XBRL unit id; its measure is what
# matters.  ``usd`` is how most instances spell iso4217:USD, ``U_USD`` is
# MSFT's.  Nothing else is dollars as far as this rule is concerned.
USD_UNIT_IDS = frozenset({"usd", "u_usd", "iso4217:usd", "iso4217_usd", "unit_usd"})


def is_usd(unit: Any) -> bool:
    return str(unit or "").strip().lower() in USD_UNIT_IDS


def _revenue_concepts() -> tuple[str, ...]:
    from .research_plan import DEFAULT_REVENUE_CONCEPT_CANDIDATES

    return tuple(f"us-gaap:{name}" for name in DEFAULT_REVENUE_CONCEPT_CANDIDATES)


class FyMinus9mRefused(QuantitativeClaimPromotionError):
    """The filed rows Core holds do not support the derivation, and why."""


def _day(value: Any) -> date | None:
    try:
        return date.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def _span(start: date, end: date) -> int:
    return (end - start).days


def _is_quarter(start: date, end: date) -> bool:
    return QUARTER_DAYS[0] <= _span(start, end) <= QUARTER_DAYS[1]


def _is_year(start: date, end: date) -> bool:
    return YEAR_DAYS[0] <= _span(start, end) <= YEAR_DAYS[1]


def precision_unit(value: str) -> Decimal:
    """The filed precision of one value: 10^k, k the trailing zeros, capped at 6."""

    amount = Decimal(value)
    if amount == 0:
        return Decimal(1).scaleb(MAX_PRECISION_EXPONENT)
    exponent = amount.normalize().as_tuple().exponent
    return Decimal(1).scaleb(min(int(exponent), MAX_PRECISION_EXPONENT))


def _plain(value: Decimal) -> str:
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return "0" if text in {"", "-0"} else text


def _rounded(value: Decimal, digits: int, rounding: str = ROUND_HALF_UP) -> str:
    """The number as ``verify_numeric_spec`` writes it for these digits."""

    return _plain(value.quantize(Decimal(1).scaleb(-digits), rounding=rounding))


class _Rows:
    """Every consolidated revenue row one company's held filings report."""

    def __init__(self, connection: sqlite3.Connection, company_ref: str) -> None:
        self.connection = connection
        self.company_ref = company_ref
        concepts = _revenue_concepts()
        self.filings: dict[str, dict[str, Any]] = {
            row["ingest_id"]: {key: row[key] for key in row.keys()}
            for row in connection.execute(
                "SELECT * FROM coverage_mission_statement_filings WHERE company_ref=? "
                "ORDER BY filed, accession", (company_ref,)).fetchall()
        }
        marks = ",".join("?" for _ in concepts)
        rows = connection.execute(
            "SELECT l.* FROM coverage_mission_statement_lines l "
            "JOIN coverage_mission_statement_filings f ON f.ingest_id=l.ingest_id "
            f"WHERE f.company_ref=? AND l.concept IN ({marks}) "
            "AND l.dimension_axis IS NULL AND l.dimension_member IS NULL "
            "ORDER BY l.ingest_id, l.ordinal", (company_ref, *concepts)).fetchall()
        # (concept, start, end) -> [(line, filing)]
        self.by_period: dict[tuple[str, date, date], list[tuple[dict, dict]]] = {}
        for row in rows:
            line = {key: row[key] for key in row.keys()}
            start, end = _day(line["period_start"]), _day(line["period_end"])
            value = canonical_decimal(line["value"])
            if start is None or end is None or value is None or not is_usd(line["unit"]):
                continue
            filing = self.filings.get(line["ingest_id"])
            if filing is None:
                continue
            self.by_period.setdefault((line["concept"], start, end), []).append((line, filing))

    def periods(self, concept: str, *, start: date | None = None,
                end: date | None = None) -> list[tuple[date, date]]:
        return sorted({
            (s, e) for (c, s, e) in self.by_period
            if c == concept and (start is None or s == start) and (end is None or e == end)
        })


def _period_text(start: date, end: date) -> str:
    return f"{start.isoformat()}..{end.isoformat()}"


class _Derivation:
    """One FY − 9M derivation for the 10-K ``annual``; raises FyMinus9mRefused."""

    def __init__(self, rows: _Rows, annual: Mapping[str, Any]) -> None:
        self.rows = rows
        self.annual = annual
        self.components: list[dict[str, Any]] = []
        self.checks: list[str] = []

    def refuse(self, reason: str) -> None:
        raise FyMinus9mRefused(
            f"FY−9M refused for {self.annual['accession']} ({self.rows.company_ref}): {reason}")

    # -- periods ----------------------------------------------------------

    def fiscal_year(self) -> tuple[str, date, date]:
        fy_end = _day(self.annual["report_date"])
        if fy_end is None:
            self.refuse("the 10-K names no report date")
        for concept in _revenue_concepts():
            spans = [
                (start, end) for start, end in self.rows.periods(concept, end=fy_end)
                if _is_year(start, end) and any(
                    filing["ingest_id"] == self.annual["ingest_id"]
                    for _, filing in self.rows.by_period[(concept, start, end)])
            ]
            if len(spans) > 1:
                self.refuse(f"the 10-K files {len(spans)} fiscal years ending {fy_end} "
                            f"under {concept}")
            if spans:
                return concept, spans[0][0], fy_end
        self.refuse("the 10-K files no consolidated fiscal-year revenue row ending on its "
                    f"report date {fy_end} under {', '.join(_revenue_concepts())}")
        raise AssertionError  # pragma: no cover

    def prior_year(self, concept: str, fy_start: date) -> tuple[date, date]:
        prior_end = fy_start - timedelta(days=1)
        spans = [(s, e) for s, e in self.rows.periods(concept, end=prior_end) if _is_year(s, e)]
        if not spans:
            self.missing(concept, None, prior_end,
                         f"no filed fiscal-year row ends {prior_end}, the day before "
                         f"this fiscal year starts")
        if len(spans) > 1:
            self.refuse(f"{len(spans)} candidate prior fiscal years end {prior_end}")
        return spans[0]

    def quarter(self, concept: str, start: date, name: str) -> tuple[date, date]:
        spans = [(s, e) for s, e in self.rows.periods(concept, start=start) if _is_quarter(s, e)]
        if not spans:
            self.missing(concept, start, None,
                         f"no filed {name} row starts {start}")
        if len(spans) > 1:
            self.refuse(f"{name} starting {start} is ambiguous: "
                        + ", ".join(_period_text(*item) for item in spans))
        return spans[0]

    def missing(self, concept: str, start: date | None, end: date | None, reason: str) -> None:
        others = sorted({
            c for (c, s, e) in self.rows.by_period
            if c != concept and (start is None or s == start) and (end is None or e == end)
        })
        if others:
            self.refuse(f"concept changed: {reason} under {concept}, but "
                        f"{', '.join(others)} does")
        self.refuse(reason + f" (concept {concept})")

    # -- values -----------------------------------------------------------

    def component(self, role: str, concept: str, start: date, end: date, *,
                  prefer: Sequence[str] = ()) -> dict[str, Any]:
        held = self.rows.by_period.get((concept, start, end)) or []
        if not held:
            self.missing(concept, start, end, f"no filed row for {_period_text(start, end)}")
        by_value: dict[str, list[tuple[dict, dict]]] = {}
        for line, filing in held:
            by_value.setdefault(canonical_decimal(line["value"]), []).append((line, filing))
        values = sorted(by_value, key=Decimal)
        if len(values) > 1:
            low, high = Decimal(values[0]), Decimal(values[-1])
            tolerance = max(precision_unit(item) for item in values) / 2
            if high - low > tolerance:
                seen = "; ".join(
                    f"{value} in " + ", ".join(sorted({f['accession'] for _, f in by_value[value]}))
                    for value in values)
                self.refuse(f"restated: {_period_text(start, end)} is filed as {seen}")
            self.checks.append(
                f"{role}: {_period_text(start, end)} filed as {', '.join(values)} -- "
                "within half the coarser filed unit, a presentation difference")

        def rank(item: tuple[dict, dict]) -> tuple:
            line, filing = item
            preferred = (prefer.index(filing["ingest_id"])
                         if filing["ingest_id"] in prefer else len(prefer))
            own = 0 if filing["report_date"] == end.isoformat() else 1
            return (preferred, own, -_ordinal_date(filing["filed"]),
                    filing["accession"], 0 if line["statement"] == "income" else 1,
                    int(line["ordinal"]))

        line, filing = min(held, key=rank)
        value = canonical_decimal(line["value"])
        same_filing = [item for item in held if item[1]["ingest_id"] == filing["ingest_id"]]
        if len({canonical_decimal(item[0]["value"]) for item in same_filing}) > 1:
            self.refuse(f"{filing['accession']} files {_period_text(start, end)} twice with "
                        "different values")
        wire = {
            "role": role,
            "concept": concept,
            "period_start": start.isoformat(),
            "period_end": end.isoformat(),
            "value": value,
            "precision_unit": _plain(precision_unit(value)),
            "line_id": str(line["line_id"]),
            "ingest_id": str(filing["ingest_id"]),
            "accession": str(filing["accession"]),
            "form": str(filing["form"]),
            "filed": str(filing["filed"]),
            "report_date": str(filing["report_date"]),
            "filing_content_hash": str(filing["content_hash"]),
            "reported_by": sorted({str(f["accession"]) for _, f in held}),
        }
        self.components.append(wire)
        return wire

    def own_ten_q(self, quarter: tuple[date, date], name: str) -> str:
        end = quarter[1].isoformat()
        own = [f for f in self.rows.filings.values()
               if f["form"] == "10-Q" and f["report_date"] == end]
        if not own:
            self.refuse(f"this fiscal year's {name} ({_period_text(*quarter)}) is not held: "
                        f"no 10-Q for {end} in Core")
        return own[0]["ingest_id"]

    # -- one fiscal year ----------------------------------------------------

    def year(self, prefix: str, concept: str, fy_start: date, fy_end: date, *,
             current: bool, prefer: Sequence[str]) -> dict[str, Any]:
        fy = self.component(f"{prefix}.fiscal_year", concept, fy_start, fy_end,
                            prefer=[self.annual["ingest_id"], *prefer])
        q1 = self.quarter(concept, fy_start, f"{prefix} Q1")
        q2 = self.quarter(concept, q1[1] + timedelta(days=1), f"{prefix} Q2")
        q3 = self.quarter(concept, q2[1] + timedelta(days=1), f"{prefix} Q3")
        q4_start = q3[1] + timedelta(days=1)
        if q3[1] >= fy_end or not _is_quarter(q4_start, fy_end):
            self.refuse(f"{prefix} fiscal boundaries do not align: Q3 ends {q3[1]}, the "
                        f"fiscal year ends {fy_end}, leaving {_span(q4_start, fy_end) + 1} days")
        own: list[str] = []
        if current:
            own = [self.own_ten_q(q, n) for q, n in ((q1, "Q1"), (q2, "Q2"), (q3, "Q3"))]
        quarter_prefer = [*prefer, *own]
        quarters = [
            self.component(f"{prefix}.q{index}", concept, q[0], q[1],
                           prefer=[*quarter_prefer])
            for index, q in enumerate((q1, q2, q3), start=1)
        ]
        values = [Decimal(item["value"]) for item in quarters]
        halves = [Decimal(item["precision_unit"]) / 2 for item in quarters]
        for months, end, count in (("six", q2[1], 2), ("nine", q3[1], 3)):
            if (concept, fy_start, end) not in self.rows.by_period:
                continue
            ytd = self.component(f"{prefix}.{months}_months", concept, fy_start, end,
                                 prefer=[*prefer, *own[count - 1:count]])
            total = sum(values[:count], Decimal(0))
            tolerance = sum(halves[:count], Decimal(0)) + Decimal(ytd["precision_unit"]) / 2
            if abs(Decimal(ytd["value"]) - total) > tolerance:
                self.refuse(
                    f"{prefix} filed {months}-month row {ytd['value']} "
                    f"({ytd['accession']}) is not the sum of its quarters {_plain(total)}")
            self.checks.append(
                f"{prefix}: filed {months} months {ytd['value']} = sum of quarters "
                f"{_plain(total)} within {_plain(tolerance)}")
        nine = next((item for item in self.components
                     if item["role"] == f"{prefix}.nine_months"), None)
        if nine is not None:
            nine_value = Decimal(nine["value"])
            nine_half = Decimal(nine["precision_unit"]) / 2
            nine_basis = {"basis": "filed_nine_month_row", "roles": [nine["role"]],
                          "value": nine["value"]}
        else:
            nine_value = sum(values, Decimal(0))
            nine_half = sum(halves, Decimal(0))
            nine_basis = {"basis": "sum_of_quarters",
                          "roles": [item["role"] for item in quarters],
                          "value": _plain(nine_value)}
        q4 = Decimal(fy["value"]) - nine_value
        uncertainty = Decimal(fy["precision_unit"]) / 2 + nine_half
        if q4 <= 0:
            self.refuse(f"{prefix} derived fourth quarter {_plain(q4)} is not positive")
        q4_period = (q4_start, fy_end)
        filed_q4 = self.rows.by_period.get((concept, q4_start, fy_end)) or []
        if current and any(f["ingest_id"] == self.annual["ingest_id"] for _, f in filed_q4):
            self.refuse("the 10-K files its own fourth quarter "
                        f"{_period_text(*q4_period)}; that number is filed, and the "
                        "same-accession annual rule is the rule for it")
        for line, filing in filed_q4:
            filed_value = Decimal(canonical_decimal(line["value"]))
            slack = uncertainty + precision_unit(canonical_decimal(line["value"])) / 2
            if abs(filed_value - q4) > slack:
                self.refuse(f"restated: {filing['accession']} files "
                            f"{_period_text(*q4_period)} as {_plain(filed_value)}, the "
                            f"derivation gives {_plain(q4)}")
            self.checks.append(f"{prefix}: {filing['accession']} files the fourth quarter "
                               f"as {_plain(filed_value)}, the derivation agrees within "
                               f"{_plain(slack)}")
        return {
            "fiscal_year_period": _period_text(fy_start, fy_end),
            "fiscal_year_role": fy["role"],
            "quarters": {f"q{index}": _period_text(*q)
                         for index, q in enumerate((q1, q2, q3), start=1)},
            "nine_months": nine_basis,
            "q4": {"period": _period_text(*q4_period), "value": _plain(q4),
                   "uncertainty": _plain(uncertainty)},
        }

    def run(self) -> dict[str, Any]:
        if self.annual["form"] != "10-K":
            self.refuse(f"{self.annual['accession']} is a {self.annual['form']}, not a 10-K")
        concept, fy_start, fy_end = self.fiscal_year()
        current = self.year("current", concept, fy_start, fy_end, current=True, prefer=[])
        # The current Q3 10-Q repeats last year's quarters and nine months: the
        # comparatives as the filer presents them now, next to this year's.
        q3_own = next(item["ingest_id"] for item in self.components
                      if item["role"] == "current.q3")
        prior_start, prior_end = self.prior_year(concept, fy_start)
        prior = self.year("prior", concept, prior_start, prior_end, current=False,
                          prefer=[q3_own])
        q4c, q4p = Decimal(current["q4"]["value"]), Decimal(prior["q4"]["value"])
        ec, ep = Decimal(current["q4"]["uncertainty"]), Decimal(prior["q4"]["uncertainty"])
        if q4p - ep <= 0:
            self.refuse("the prior fourth quarter is not positive within its filed precision")
        growth = (q4c / q4p - 1) * 100
        low = ((q4c - ec) / (q4p + ep) - 1) * 100
        high = ((q4c + ec) / (q4p - ep) - 1) * 100
        bound = max(high - growth, growth - low)
        digits = next((d for d in range(MAX_DIGITS, -1, -1)
                       if bound <= Decimal("0.5") * Decimal(1).scaleb(-d)), None)
        if digits is None or bound > MAX_BOUND_PP:
            self.refuse(f"filed precision bounds the growth only to ±{_rounded(bound, 3)} "
                        "percentage points, wider than half a point")
        value = _rounded(growth, digits)
        origin = "sec-fy-minus-9m:" + content_hash({
            "company_ref": self.rows.company_ref, "annual": self.annual["ingest_id"],
            "concept": concept,
            "components": [[item["role"], item["line_id"]] for item in self.components],
        })[:32]
        return {
            "origin_ref": origin,
            "concept": concept,
            "current": current,
            "prior": prior,
            "growth": value,
            "precision": {
                "basis": PRECISION_BASIS,
                "growth_bound_pp": _rounded(bound, 4, ROUND_CEILING),
                "digits": digits,
                "interval_pp": [_rounded(low, 2, ROUND_FLOOR), _rounded(high, 2, ROUND_CEILING)],
            },
            "components": list(self.components),
            "checks": list(self.checks),
        }


def _ordinal_date(value: Any) -> int:
    parsed = _day(value)
    return parsed.toordinal() if parsed is not None else 0


def annual_filing(connection: sqlite3.Connection, *, company_ref: str,
                  accession: str) -> dict[str, Any] | None:
    """The 10-K statement filing Core holds for this accession, or None."""

    row = connection.execute(
        "SELECT * FROM coverage_mission_statement_filings "
        "WHERE company_ref=? AND accession=? AND form='10-K'", (company_ref, accession),
    ).fetchone()
    return None if row is None else {key: row[key] for key in row.keys()}


def derive(connection: sqlite3.Connection, ingest_id: str) -> dict[str, Any]:
    """The derivation for one held 10-K, or ``FyMinus9mRefused`` with its reason."""

    annual, _ = filing_rows(connection, ingest_id)
    return _Derivation(_Rows(connection, str(annual["company_ref"])), annual).run()


def _amount(value: str) -> str:
    return f"USD {value}"


def _statement(entity: str, derivation: Mapping[str, Any], accession: str) -> str:
    current, prior = derivation["current"], derivation["prior"]
    comps = {item["role"]: item for item in derivation["components"]}

    def nine(year: Mapping[str, Any]) -> str:
        basis = year["nine_months"]
        if basis["basis"] == "filed_nine_month_row":
            return f"nine months {_amount(basis['value'])}"
        return "nine months " + " + ".join(_amount(comps[role]["value"]) for role in basis["roles"])

    growth = derivation["growth"]
    direction = "down" if growth.startswith("-") else "up"
    low, high = derivation["precision"]["interval_pp"]
    return (
        f"{entity} fourth quarter {current['q4']['period']}: revenue derived as fiscal year "
        f"{_amount(comps[current['fiscal_year_role']]['value'])} ({current['fiscal_year_period']}, "
        f"10-K {accession}) less {nine(current)} = {_amount(current['q4']['value'])} "
        f"(±{current['q4']['uncertainty']} at filed precision); the comparable quarter "
        f"{prior['q4']['period']} derived the same way is {_amount(prior['q4']['value'])} "
        f"(fiscal year {_amount(comps[prior['fiscal_year_role']]['value'])} less {nine(prior)}, "
        f"±{prior['q4']['uncertainty']}); {direction} {growth.lstrip('-')}% year over year "
        f"(filed precision bounds it to {low}%..{high}%). Derived value (FY − 9M, rule "
        f"{RULE_REF}), not a filed quarter; concept {derivation['concept']}; every component "
        "row is listed in the evidence."
    )


class SecFyMinus9mAuthorityResolver(SecStatementLineAuthorityResolver):
    """The statement-line provenance mode, over the rows of several filings.

    The envelope is the 10-K the fourth quarter belongs to (the filing whose
    period the Claim answers); every other filing a component comes from is
    named in the payload with its own content hash and re-verified here, down
    to its dispatch.
    """

    def payload(self, ingest_id: str) -> dict[str, Any]:
        annual, _ = filing_rows(self.connection, ingest_id)
        derivation = derive(self.connection, ingest_id)
        lines = []
        for item in derivation["components"]:
            _, held = filing_rows(self.connection, item["ingest_id"])
            row = next(line for line in held if line["line_id"] == item["line_id"])
            lines.append({**line_projection(row), "ingest_id": item["ingest_id"],
                          "role": item["role"]})
        period = derivation["current"]["q4"]["period"]
        entity = str(annual["entity_name"])
        return {
            "kind": PAYLOAD_KIND,
            "company_ref": str(annual["company_ref"]),
            "cik": str(annual["cik"]),
            "entity_name": entity,
            "accession": str(annual["accession"]),
            "form": str(annual["form"]),
            "filed": str(annual["filed"]),
            "report_date": str(annual["report_date"]),
            "ingest_id": str(annual["ingest_id"]),
            "dispatch_id": str(annual["dispatch_id"]),
            "governance_ref": str(annual["governance_ref"]),
            "governance_hash": str(annual["governance_hash"]),
            "filing_content_hash": str(annual["content_hash"]),
            "lines": lines,
            "components": derivation["components"],
            "metric_or_aspect": METRIC,
            "period": period,
            "basis": DERIVED_BASIS,
            "value": derivation["growth"],
            "unit": "percent",
            "currency": None,
            "scale": "one",
            "normalized_statement": _statement(entity, derivation, str(annual["accession"])),
            "derivation": {
                "operator": "fy_minus_9m",
                "rule_ref": RULE_REF,
                "origin_ref": derivation["origin_ref"],
                "concept": derivation["concept"],
                "expression": ("q4 = fiscal_year - nine_months; "
                               "growth = (q4_current / q4_prior - 1) * 100"),
                "current": derivation["current"],
                "prior": derivation["prior"],
                "precision": derivation["precision"],
                "checks": derivation["checks"],
            },
        }

    def _recorded_at(self, payload: Mapping[str, Any]) -> str:
        ids = {payload["ingest_id"], *(item["ingest_id"] for item in payload["components"])}
        return max(str(filing_rows(self.connection, item)[0]["recorded_at"]) for item in ids)

    def build_fy_material(self, ingest_id: str) -> dict[str, Any]:
        from .research_verification import validate_source_verification_material

        payload = self.payload(ingest_id)
        filing = self.filing(payload["ingest_id"])
        records = _source_records(filing)
        artifact_ref, artifact_hash = _raw_sink(records)
        origin = payload["derivation"]["origin_ref"]
        when = self._recorded_at(payload)
        base = {
            "schema_version": "0.2",
            "id": "sec-fy-minus-9m-material:" + content_hash({
                "origin_ref": origin, "payload": canonical_json(payload)})[:32],
            "created_at": when,
            "source_envelope_ref": payload["ingest_id"],
            "source_envelope_hash": payload["filing_content_hash"],
            "artifact_ref": artifact_ref,
            "artifact_hash": artifact_hash,
            "source_ref": SEC_STATEMENT_LINE_SOURCE_REF,
            "source_type": "official_filing",
            "operation": SEC_STATEMENT_LINE_OPERATION,
            "provenance_mode": self.provenance_mode,
            "authority_resolution_ref": origin,
            "authority_resolution_hash": content_hash(payload["components"]),
            "source_record_refs": records,
            "next_cursor": None,
            "normalized_payload": payload,
            "normalized_payload_hash": hashlib.sha256(
                canonical_json(payload).encode("utf-8")).hexdigest(),
            "source_schema_hash": content_hash({
                "governance_ref": payload["governance_ref"],
                "governance_hash": payload["governance_hash"],
            }),
            "source_content_hash": payload["filing_content_hash"],
            "source_lineage": [
                SEC_STATEMENT_LINE_SOURCE_REF, payload["dispatch_id"],
                payload["ingest_id"], f"sec:filing:{payload['accession']}", origin,
            ],
            "published_at": None,
            "updated_at": None,
            "as_of": None,
            "retrieved_at": when,
            "completeness": "enumerated",
            "status": "complete",
        }
        base["content_hash"] = content_hash(base)
        return validate_source_verification_material(base)

    def verify_source_material(self, material: Mapping[str, Any]) -> dict[str, Any]:
        from .research_verification import (
            validate_source_verification_material,
            validate_verification_bundle,
        )

        material_wire = validate_source_verification_material(material)
        payload = material_wire.get("normalized_payload")
        if (material_wire.get("provenance_mode") != self.provenance_mode
                or not isinstance(payload, Mapping) or payload.get("kind") != PAYLOAD_KIND):
            raise QuantitativeClaimPromotionError(
                "the FY−9M verifier requires FY−9M statement-line material")
        findings: list[dict[str, Any]] = []

        def check(code: str, observed: Any, expected: Any, path: str, message: str) -> None:
            findings.append(_finding(
                code, ok=observed == expected, path=path,
                expected=canonical_json(expected) if isinstance(expected, (dict, list)) else expected,
                observed=canonical_json(observed) if isinstance(observed, (dict, list)) else observed,
                message=message))

        ingest_id = material_wire["source_envelope_ref"]
        filing, _ = filing_rows(self.connection, ingest_id)
        check("filing_content_hash", material_wire["source_envelope_hash"],
              str(filing["content_hash"]), "material.source_envelope_hash",
              "material binds the exact 10-K filing row Core holds")
        check("source_content_hash", material_wire["source_content_hash"],
              str(filing["content_hash"]), "material.source_content_hash",
              "material binds the 10-K as its source content")
        check("governance_binding",
              [payload.get("governance_ref"), payload.get("governance_hash")],
              [str(filing["governance_ref"]), str(filing["governance_hash"])],
              "material.normalized_payload.governance",
              "material names the approved connector-governance record the parse ran under")
        records = _source_records(filing)
        check("source_record_refs", material_wire["source_record_refs"], records,
              "material.source_record_refs", "material names the 10-K's raw artifacts")
        try:
            artifact_ref, artifact_hash = _raw_sink(records)
        except QuantitativeClaimPromotionError:
            artifact_ref, artifact_hash = None, None
        check("artifact_ref", material_wire["artifact_ref"], artifact_ref,
              "material.artifact_ref", "raw spool artifact ref is the 10-K's own")
        check("artifact_hash", material_wire["artifact_hash"], artifact_hash,
              "material.artifact_hash", "raw spool artifact hash is the digest its ref names")
        filings = {str(ingest_id)} | {
            str(item.get("ingest_id")) for item in payload.get("components") or ()
            if isinstance(item, Mapping)}
        for index, held_id in enumerate(sorted(filings)):
            try:
                held, _ = filing_rows(self.connection, held_id)
            except QuantitativeClaimPromotionError:
                held = None
            dispatch = None if held is None else self.dispatch(held["dispatch_id"])
            check(f"component_filing_dispatch_settled:{index}",
                  None if dispatch is None else dispatch["status"], "succeeded",
                  f"filings[{held_id}].dispatch",
                  "every filing a component comes from was fetched by a settled dispatch")
            check(f"component_filing_company:{index}",
                  None if held is None else held["company_ref"], str(filing["company_ref"]),
                  f"filings[{held_id}].company_ref", "every component filing is the company's")
        try:
            rebuilt = self.payload(str(ingest_id))
        except QuantitativeClaimPromotionError as exc:
            rebuilt = str(exc)
        check("payload_equals_core_derivation", payload, rebuilt,
              "material.normalized_payload",
              "material payload equals the FY−9M derivation this Core makes from its filed rows")
        origin = rebuilt["derivation"]["origin_ref"] if isinstance(rebuilt, Mapping) else None
        check("authority_resolution_ref", material_wire["authority_resolution_ref"], origin,
              "material.authority_resolution_ref", "material names the derivation it rests on")
        check("authority_resolution_hash", material_wire["authority_resolution_hash"],
              content_hash(rebuilt["components"]) if isinstance(rebuilt, Mapping) else None,
              "material.authority_resolution_hash", "material binds the exact component rows")
        check("derivation_rule", (payload.get("derivation") or {}).get("rule_ref"), RULE_REF,
              "material.normalized_payload.derivation.rule_ref",
              "the derivation names the FY−9M rule")
        check("source_ref", material_wire["source_ref"], SEC_STATEMENT_LINE_SOURCE_REF,
              "material.source_ref", "material is SEC filing authority")
        check("source_type", material_wire["source_type"], "official_filing",
              "material.source_type", "material is official filing evidence")
        check("operation", material_wire["operation"], SEC_STATEMENT_LINE_OPERATION,
              "material.operation", "material names the statements operation")
        check("source_lineage", material_wire["source_lineage"],
              [SEC_STATEMENT_LINE_SOURCE_REF, str(filing["dispatch_id"]),
               str(filing["ingest_id"]), f"sec:filing:{filing['accession']}", origin],
              "material.source_lineage",
              "material lineage is source, dispatch, 10-K, accession, derivation")
        verdict = "reject" if any(
            item["status"] == "fail" and item["severity"] == "error" for item in findings
        ) else "pass"
        base = {
            "schema_version": "0.1",
            "id": "sec-fy-minus-9m-source-verification:" + content_hash({
                "subject": material_wire["id"],
                "filing": str(filing["content_hash"]),
                "findings": [item["content_hash"] for item in findings],
            }),
            "created_at": material_wire["retrieved_at"],
            "kind": "source",
            "subject_ref": material_wire["id"],
            "subject_hash": material_wire["content_hash"],
            "verdict": verdict,
            "checkpoint_ref": f"sec:filing:{filing['accession']}",
            "checkpoint_hash": str(filing["content_hash"]),
            "findings": findings,
            "verifier_ref": self.verifier[0],
            "verifier_hash": self.verifier[1],
        }
        base["content_hash"] = content_hash(base)
        return validate_verification_bundle(base)

    def numeric_spec(self, material: Mapping[str, Any]) -> dict[str, Any]:
        """Growth of the two derived quarters; the verifier recomputes it."""

        from .research_verification import validate_numeric_verification_spec

        payload = material["normalized_payload"]
        derivation = payload["derivation"]
        inputs = [
            {
                "name": name,
                "value": derivation[year]["q4"]["value"],
                "unit": "number",
                "currency": None,
                "scale": "one",
                "period": derivation[year]["q4"]["period"],
                "source_material_ref": material["id"],
                "source_material_hash": material["content_hash"],
                "json_pointer": f"/derivation/{year}/q4/value",
                "extractor": "number",
            }
            for name, year in (("current_fourth_quarter", "current"),
                               ("prior_year_fourth_quarter", "prior"))
        ]
        base = {
            "schema_version": "0.1",
            "id": "numeric-spec:sec-fy-minus-9m:" + content_hash({
                "material": material["id"], "material_hash": material["content_hash"]}),
            "created_at": material["created_at"],
            "operator": "growth_percentage",
            "inputs": inputs,
            "output_value": payload["value"],
            "output_unit": "percent",
            "output_currency": None,
            "output_scale": "one",
            "output_period": payload["period"],
            "rounding": {"mode": "half_up", "digits": derivation["precision"]["digits"]},
        }
        base["content_hash"] = content_hash(base)
        return validate_numeric_verification_spec(base)


def build_fy_minus_9m_candidate(
    connection: sqlite3.Connection, *, ingest_id: str, actor_ref: str,
    resolver: SecFyMinus9mAuthorityResolver | None = None,
) -> dict[str, Any]:
    """Every record the derived fourth-quarter growth needs, from Core alone.

    Called by the quarter lane to stage the candidate and by the auto-commit
    rule to rebuild it; the Ledger admits it only when the two are identical.
    """

    from .research_verification import (
        build_candidate_evidence,
        validate_candidate_claim,
        validate_candidate_evidence,
        verify_numeric_spec,
    )

    figures = resolver if resolver is not None else SecFyMinus9mAuthorityResolver(connection)
    if not isinstance(actor_ref, str) or not actor_ref.startswith("automation:"):
        raise QuantitativeClaimPromotionError(
            "a derived number is staged by mission automation, by name")
    material = figures.build_fy_material(str(ingest_id))
    source_verification = figures.verify_source_material(material)
    if source_verification["verdict"] != "pass":
        failed = [item["code"] for item in source_verification["findings"]
                  if item["severity"] == "error" and item["status"] == "fail"]
        raise QuantitativeClaimPromotionError(
            "FY−9M authority verification rejected: " + ", ".join(failed))
    spec = figures.numeric_spec(material)
    numeric_bundle = verify_numeric_spec(
        spec, checkpoint_ref=source_verification["checkpoint_ref"],
        checkpoint_hash=source_verification["checkpoint_hash"],
        source_material=material, source_bundle=source_verification)
    if numeric_bundle["verdict"] != "pass":
        failed = [item["code"] for item in numeric_bundle["findings"]
                  if item["severity"] == "error" and item["status"] == "fail"]
        raise QuantitativeClaimPromotionError(
            "FY−9M numeric verification rejected: " + ", ".join(failed))
    payload = material["normalized_payload"]
    when = material["retrieved_at"]
    identity = content_hash({"origin_ref": material["authority_resolution_ref"],
                             "material_hash": material["content_hash"]})[:32]
    evidence = validate_candidate_evidence(build_candidate_evidence(
        material, source_verification,
        candidate_evidence_ref="candidate-evidence:sec-fy-minus-9m:" + identity,
        actor_ref=actor_ref, created_at=when,
        verification_mode=SEC_STATEMENT_LINE_AUTHORITY_MODE,
    ))
    claim_ref = "candidate-claim:sec-fy-minus-9m:" + identity
    claim = {
        "schema_version": "0.1",
        "id": "candidate-claim-version:" + content_hash(
            {"candidate_claim_ref": claim_ref, "version": 1}),
        "created_at": when,
        "candidate_claim_ref": claim_ref,
        "version": 1,
        "subject_ref": payload["company_ref"],
        "metric_or_aspect": payload["metric_or_aspect"],
        "period": payload["period"],
        "basis": payload["basis"],
        "normalized_statement": payload["normalized_statement"],
        "semantic_verification_status": "unverified",
        "claim_kind": "quantitative",
        "value": payload["value"],
        "unit": payload["unit"],
        "currency": payload["currency"],
        "scale": payload["scale"],
        "candidate_evidence_refs": [{"ref": evidence["id"], "hash": evidence["content_hash"]}],
        "source_verification_ref": source_verification["id"],
        "source_verification_hash": source_verification["content_hash"],
        "numeric_spec_ref": spec["id"],
        "numeric_spec_hash": spec["content_hash"],
        "numeric_verification_ref": numeric_bundle["id"],
        "numeric_verification_hash": numeric_bundle["content_hash"],
        "actor_ref": actor_ref,
        "prior_version_ref": None,
    }
    claim["content_hash"] = content_hash(claim)
    return {
        "material": material,
        "source_verification": source_verification,
        "numeric_spec": spec,
        "numeric_verification": numeric_bundle,
        "evidence": evidence,
        "claim": validate_candidate_claim(claim),
        "resolver": figures,
    }


def stage_fy_minus_9m_candidate(
    connection: sqlite3.Connection, staging: Any, *, ingest_id: str, actor_ref: str,
) -> dict[str, Any]:
    """Stage the derived candidate in the shared staging store; idempotent."""

    bundle = build_fy_minus_9m_candidate(connection, ingest_id=ingest_id, actor_ref=actor_ref)
    staged = staging.stage(
        material=bundle["material"],
        source_verification=bundle["source_verification"],
        evidence=bundle["evidence"],
        claim=bundle["claim"],
        numeric_spec=bundle["numeric_spec"],
        numeric_verification=bundle["numeric_verification"],
        idempotency_key="fy-minus-9m:" + bundle["material"]["authority_resolution_ref"]
        + ":" + bundle["material"]["content_hash"][:16],
        verification_mode=SEC_STATEMENT_LINE_AUTHORITY_MODE,
        statement_resolver=bundle["resolver"],
    )
    return {**bundle, "staging": staged, "write_status": staged["write_status"]}


__all__ = [
    "DERIVED_BASIS",
    "FyMinus9mRefused",
    "METRIC",
    "PAYLOAD_KIND",
    "PRECISION_BASIS",
    "RULE_REF",
    "SecFyMinus9mAuthorityResolver",
    "annual_filing",
    "build_fy_minus_9m_candidate",
    "derive",
    "precision_unit",
    "stage_fy_minus_9m_candidate",
]
