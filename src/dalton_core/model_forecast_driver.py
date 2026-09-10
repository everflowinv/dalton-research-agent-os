"""P13-M2: the driver model -- drivers, assumptions, and the lines that follow.

P13ao produced the model input table: one row per specification line, one
column per quarter, every cell saying whether it was filed or derived and from
which accession. That table is history. A model is what you say about the
quarters that have not happened yet, and this is the object that says it.

Three layers, in one versioned record, because they are only trustworthy
together:

* **drivers** -- what the specification says moves this company, keyed by the
  concept it rests on, carrying the filed history of that concept as cell
  references (concept, period, accession). A driver is not a number; it is the
  thing a number will be assumed about.
* **assumptions** -- one per driver per future quarter, each with a value, a
  ``kind`` (``estimate`` when automation wrote it, ``human`` when a person
  did), a ``because`` in a sentence, and the refs it was computed from.
  Automation may only ever write ``estimate``.
* **results** -- the income chain that falls out of them: revenue, gross
  profit, the operating expense lines, operating income, net income, free cash
  flow. Every result cell names the assumptions and the filed cells it used,
  so any figure in the model can be walked back to a filing.

Four rules hold the thing up.

**Nothing is invented.** A driver whose concept is not in the filings gets no
assumption at all, and every result that depends on it is ``unavailable`` with
a reason. An empty cell is a fact about what we know; a plausible number in
that cell is a lie that is very hard to find later.

**The results replay exactly.** Values are Decimal throughout -- no floats
anywhere near a stored record -- computed under a fixed precision and
quantised at every step, so recomputing a version from the same specification
and the same filings produces the same bytes. That is what makes ``duplicate``
meaningful and what makes a diff between two versions a real diff.

**The roles are a closed table, not a guess.** Which filed concept is revenue,
which is cost of revenue, which is an operating expense, is read from
``CONCEPT_ROLES``. A concept outside the table gets no role, and the results
that would have needed it say so. Guessing that a concept whose name contains
"Revenue" is the top line is how a model comes to add a segment to its own
total.

**Only totals are summed.** ``CONCEPT_ROLES`` deliberately holds no
subtotals -- no ``us-gaap:OperatingExpenses``, no ``us-gaap:CostsAndExpenses``
-- because the one arithmetic error this layer could make and never notice is
adding a total to its own components.

The default generator here carries the trailing rate forward unchanged, which
is a starting point and says so in every ``because`` it writes. Replacing an
assumption is a person's job and a later slice's model call; ``draft_
assumptions`` is the seam for it and refuses anything it cannot verify.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, timezone
from decimal import Decimal, ROUND_HALF_UP, localcontext
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence

from .company_model_inputs import AMBIGUOUS, ESTIMATED, FILED, NOT_FOUND, SHARED
from .model_forecast import (
    DRIVER_FORMULA_HASH,
    DRIVER_FORMULA_REF,
    DRIVER_MODEL_VERSION_PREFIX,
    quarter_after,
)
from .store import (
    DaltonStore, authorization_flag, authorized_flag, canonical_json, content_hash,
)

SCHEMA_VERSION = "0.1"
FORMULA_REF = DRIVER_FORMULA_REF
FORMULA_HASH = DRIVER_FORMULA_HASH
GENERATOR_REF = "rule:trailing-carry-forward:1"
AUTOMATION_ACTOR = "automation:driver-model"

# How many trailing quarters the default generator averages. Four, so a full
# year of seasonality is inside the average and one odd quarter cannot set the
# rate on its own.
TRAILING_QUARTERS = 4
# Two quarter ends this far apart are consecutive quarters. Same window the
# series layer uses for a quarter's length, for the same reason: 13 weeks is
# 91 days and a 52/53-week filer lands either side of it.
QUARTER_GAP_MIN_DAYS = 80
QUARTER_GAP_MAX_DAYS = 100
MAX_FORECAST_PERIODS = 12

# Not a record field: the key a caller stamps on a body to say which version
# it was computed from. ``publish`` reads it, checks it against the head of the
# chain and drops it before the record is hashed.
SOURCE_VERSION_KEY = "source_version_ref"

_SCHEMA_PATH = Path(__file__).with_name("forecast_driver_schema.sql")
_VALUE_QUANT = Decimal("0.00000001")
_RATE_QUANT = Decimal("0.000000000001")
# Wide enough that a company's revenue in units of one dollar times a rate
# carried to twelve places never reaches the context's precision, so the
# quantise below is the only rounding that happens.
_PRECISION = 60

REVENUE = "revenue"
COST_OF_REVENUE = "cost_of_revenue"
OPERATING_EXPENSE = "operating_expense"
INCOME_TAX = "income_tax_expense"
NET_INCOME = "net_income"
OPERATING_CASH_FLOW = "operating_cash_flow"
CAPITAL_EXPENDITURE = "capital_expenditure"

# The closed role table. Components only: a concept that is the sum of other
# concepts in this table is not in it.
CONCEPT_ROLES: dict[str, str] = {
    "us-gaap:Revenues": REVENUE,
    "us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax": REVENUE,
    "us-gaap:RevenueFromContractWithCustomerIncludingAssessedTax": REVENUE,
    "us-gaap:SalesRevenueNet": REVENUE,
    "us-gaap:SalesRevenueServicesNet": REVENUE,
    "us-gaap:CostOfRevenue": COST_OF_REVENUE,
    "us-gaap:CostOfGoodsAndServicesSold": COST_OF_REVENUE,
    "us-gaap:CostOfServices": COST_OF_REVENUE,
    "us-gaap:CostOfGoodsSold": COST_OF_REVENUE,
    # Cognizant and DXC both file cost of revenue with depreciation taken out
    # of it, and file the depreciation as its own income-statement line. Both
    # are components; the pair adds up to what the other filers call cost of
    # revenue, which is why they can both be in a table that holds no totals.
    "us-gaap:CostOfGoodsAndServiceExcludingDepreciationDepletionAndAmortization":
        COST_OF_REVENUE,
    "us-gaap:DepreciationAndAmortization": OPERATING_EXPENSE,
    "us-gaap:DepreciationDepletionAndAmortization": OPERATING_EXPENSE,
    "us-gaap:SellingGeneralAndAdministrativeExpense": OPERATING_EXPENSE,
    "us-gaap:GeneralAndAdministrativeExpense": OPERATING_EXPENSE,
    "us-gaap:SellingAndMarketingExpense": OPERATING_EXPENSE,
    "us-gaap:ResearchAndDevelopmentExpense": OPERATING_EXPENSE,
    "us-gaap:AmortizationOfIntangibleAssets": OPERATING_EXPENSE,
    "us-gaap:RestructuringCharges": OPERATING_EXPENSE,
    "us-gaap:IncomeTaxExpenseBenefit": INCOME_TAX,
    "us-gaap:NetIncomeLoss": NET_INCOME,
    "us-gaap:ProfitLoss": NET_INCOME,
    "us-gaap:NetCashProvidedByUsedInOperatingActivities": OPERATING_CASH_FLOW,
    "us-gaap:NetCashProvidedByUsedInOperatingActivitiesContinuingOperations":
        OPERATING_CASH_FLOW,
    "us-gaap:PaymentsToAcquirePropertyPlantAndEquipment": CAPITAL_EXPENDITURE,
}
ROLES: tuple[str, ...] = (
    REVENUE, COST_OF_REVENUE, OPERATING_EXPENSE, INCOME_TAX, NET_INCOME,
    OPERATING_CASH_FLOW, CAPITAL_EXPENDITURE,
)
# One filed concept may hold each of these roles; several concepts may be
# operating expenses, because that is the one role whose members add up.
SINGLE_ROLES = frozenset(ROLES) - {OPERATING_EXPENSE}
# Which statement a concept has to have been filed on to hold its role. The
# guard that stops the worst arithmetic error available here: depreciation is
# an income-statement line for a filer who reports cost of revenue without it,
# and a *cash flow* line for everyone else. Reading the cash-flow one as an
# operating expense would subtract it a second time from a cost line that
# already contained it, and the model would look entirely reasonable.
ROLE_STATEMENTS: dict[str, str] = {
    REVENUE: "income", COST_OF_REVENUE: "income", OPERATING_EXPENSE: "income",
    INCOME_TAX: "income", NET_INCOME: "income",
    OPERATING_CASH_FLOW: "cash", CAPITAL_EXPENDITURE: "cash",
}

DRIVER_KINDS: tuple[str, ...] = ("revenue", "expense")
DRIVER_STATUSES: tuple[str, ...] = (FILED, SHARED, ESTIMATED, NOT_FOUND, AMBIGUOUS)
FORECASTABLE_STATUSES = frozenset({FILED, SHARED})
# What a cell is. ``estimate`` is what we thought, ``human`` is what a person
# decided, ``actual`` is what the company filed. The three are never merged and
# an ``actual`` never overwrites the ``estimate`` it replaced: reconciliation
# grades the estimate, and the guidance-style work that comes later needs to
# read "what did we think, and what happened" for every quarter that has both.
ASSUMPTION_KINDS: tuple[str, ...] = ("estimate", "human", "actual")
CELL_KINDS: tuple[str, ...] = ("estimate", "actual")
# Why a version exists. A closed vocabulary, because "the model changed" is not
# a reason and a version chain full of it cannot be read back as a history of
# what this system learned and when.
CHANGE_REASONS: tuple[str, ...] = (
    # A filing landed and a quarter this model had estimated became actual.
    "filing_actual",
    # Something happened to a driver -- a contract signed, a price move, a
    # guidance change -- and the assumption moved with it.
    "driver_event",
    # The assumptions were looked at again without new evidence about the
    # world: a rate rebased, a method changed.
    "assumption_review",
    # More or better evidence about the same period: a new filing, a new
    # transcript, a figure extracted from a document.
    "evidence_thicker",
    # A person overrode what the system computed.
    "human_revision",
)
# How many realised quarters a model keeps beside its forecast. Enough to see a
# pattern of misses; the version chain holds the rest.
MAX_REALISED_PERIODS = 8
MEASURES: tuple[str, ...] = (
    # The quarter-on-quarter change carried forward.
    "quarterly_growth",
    # A share of forecast revenue.
    "revenue_share",
    # A share of forecast operating income.
    "operating_income_share",
)
REF_KINDS: tuple[str, ...] = (
    "input_cell", "claim", "figure", "prior_period", "filing", "event",
    "human_decision",
)
CELL_STATUSES: tuple[str, ...] = ("computed", "unavailable")
RESULT_STATUSES: tuple[str, ...] = ("computed", "partial", "unavailable")

_RECORD_FIELDS = frozenset({
    "schema_version", "id", "created_at", "model_ref", "version",
    "prior_version_ref", "change_reason", "evidence_refs", "decision", "company_ref",
    "spec_ref", "spec_hash", "inputs_hash", "unit", "currency",
    "history_periods", "realised_periods", "forecast_periods", "statements",
    "formula_ref", "formula_hash", "generator_ref", "drivers", "assumptions",
    "results", "mission_version_ref", "actor_ref", "body_hash", "content_hash",
})
# What the version chain is about. Two records with the same body are the same
# model, whatever mission asked for them, whenever they were built, and
# whatever evidence prompted the attempt -- so a tick that finds nothing new is
# a duplicate rather than a version saying the same thing again.
_BODY_EXCLUDED = frozenset({
    "id", "created_at", "version", "prior_version_ref", "change_reason",
    "evidence_refs", "decision", "mission_version_ref", "body_hash",
    "content_hash",
})
_DRIVER_FIELDS = frozenset({
    "ref", "kind", "label", "concept", "unit", "statement", "status", "role",
    "spec_rows", "note", "history",
})
_CELL_FIELDS = frozenset({
    "concept", "period_start", "period_end", "value", "basis", "accessions",
})
_ASSUMPTION_FIELDS = frozenset({
    "ref", "driver_ref", "period", "measure", "value", "unit", "kind",
    "because", "refs", "provenance", "superseded_by",
})
_REF_FIELDS = frozenset({"kind", "ref", "concept", "period_end", "accession"})
_PROVENANCE_FIELDS = frozenset({"rule_ref", "work_order_ref", "decided_by"})
_RESULT_FIELDS = frozenset({
    "ref", "role", "label", "unit", "formula", "driver_ref", "status",
    "reason", "cells",
})
_RESULT_CELL_FIELDS = frozenset({
    "ref", "period", "kind", "status", "value", "reason", "superseded_by",
    "assumption_refs", "input_cell_refs", "result_refs",
})
_PERIOD_FIELDS = frozenset({"start", "end", "calendar", "kind"})
_RESULT_REF_FIELDS = frozenset({"ref", "period_end"})


_UNSET = object()


class ForecastModelError(RuntimeError):
    """Base error for the driver model."""


class ForecastModelValidationError(ForecastModelError, ValueError):
    """A record does not satisfy the closed contract."""


class ForecastModelConflict(ForecastModelError):
    """A request conflicts with immutable authority."""


class ForecastModelNotFound(ForecastModelError, LookupError):
    """No such forecast model version."""


class ForecastModelUnavailable(ForecastModelError):
    """This company cannot be modelled from what is held, and why."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ForecastModelValidationError(f"{name} must be non-empty text")
    return value.strip()


def _optional_text(value: Any, name: str) -> str | None:
    return None if value is None else _text(value, name)


def _sha256(value: Any, name: str) -> str:
    value = _text(value, name)
    if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
        raise ForecastModelValidationError(f"{name} must be a lowercase SHA-256")
    return value


def _closed(value: Any, fields: frozenset[str], name: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ForecastModelValidationError(f"{name} must be an object")
    wire = dict(value)
    if set(wire) != fields:
        raise ForecastModelValidationError(
            f"{name} has an invalid closed shape; "
            f"missing={sorted(fields - set(wire))}, unknown={sorted(set(wire) - fields)}"
        )
    return wire


def _one_of(value: Any, allowed: Sequence[str], name: str) -> str:
    if value not in allowed:
        raise ForecastModelValidationError(f"{name} must be one of {', '.join(allowed)}")
    return str(value)


def _decimal(value: Any, name: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (str, int, Decimal)):
        raise ForecastModelValidationError(f"{name} must be a decimal string")
    try:
        parsed = Decimal(str(value))
    except Exception as exc:  # noqa: BLE001 - the message is the point
        raise ForecastModelValidationError(f"{name} must be a decimal string") from exc
    if not parsed.is_finite():
        raise ForecastModelValidationError(f"{name} must be finite")
    return parsed


def _iso_date(value: Any, name: str) -> str:
    value = _text(value, name)
    try:
        date.fromisoformat(value)
    except ValueError as exc:
        raise ForecastModelValidationError(f"{name} must be YYYY-MM-DD") from exc
    return value


def company_slug(company_ref: str) -> str:
    """A stable, collision-free name for one company inside a ref.

    Not the last colon-separated segment. Live already holds
    ``company:sec-cik:001688568`` beside ``company:sec-cik:0001467373`` -- one
    of them is missing a digit -- and a ref shape that is not a CIK at all is a
    matter of time. Two companies whose refs ended in the same segment would
    write into each other's version chain, and the damage would be invisible.
    """

    return content_hash({"company_ref": _text(company_ref, "company_ref")})[:32]


def _plain(value: Decimal) -> str:
    return format(value.quantize(_VALUE_QUANT, ROUND_HALF_UP), "f")


def _rate(value: Decimal) -> str:
    return format(value.quantize(_RATE_QUANT, ROUND_HALF_UP), "f")


def _period(value: Any, name: str) -> dict[str, str]:
    wire = _closed(value, _PERIOD_FIELDS, name)
    out = {
        "start": _iso_date(wire["start"], f"{name}.start"),
        "end": _iso_date(wire["end"], f"{name}.end"),
        "calendar": _text(wire["calendar"], f"{name}.calendar"),
        "kind": _text(wire["kind"], f"{name}.kind"),
    }
    if out["kind"] != "quarter":
        raise ForecastModelValidationError(f"{name}.kind must be quarter")
    if not out["start"] < out["end"]:
        raise ForecastModelValidationError(f"{name}.start must precede {name}.end")
    return out


def _days(start: str, end: str) -> int:
    return (date.fromisoformat(end) - date.fromisoformat(start)).days


# -- layer one: drivers ----------------------------------------------------


def _cells_of(line: Mapping[str, Any], concept: str) -> list[dict[str, Any]]:
    cells = []
    for end, cell in (line.get("cells") or {}).items():
        cells.append({
            "concept": concept,
            "period_start": cell.get("period_start"),
            "period_end": str(end),
            "value": str(cell.get("value")),
            "basis": str(cell.get("basis")),
            "accessions": [str(item) for item in (cell.get("source_accessions") or [])],
        })
    return sorted(cells, key=lambda item: item["period_end"])


def build_drivers(table: Mapping[str, Any]) -> list[dict[str, Any]]:
    """One driver per concept the specification rests on, plus the rows that rest on nothing.

    Keyed by ``basis_concept`` rather than by specification row, because the
    filing is keyed that way: when IBM's specification splits one filed cost
    line into four, there is one history and four rows drawing on it. The
    driver carries the total, names the rows that must add up to it, and the
    split stays unfilled -- which is exactly what the input table said.
    """

    filed = {str(item["concept"]): item for item in (table.get("filed_lines") or [])}
    drivers: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for row in table.get("rows") or []:
        kind = "revenue" if row.get("kind") == "revenue_driver" else "expense"
        concept = row.get("basis_concept")
        if not concept:
            ref = f"row:{row['ref']}"
            drivers[ref] = {
                "ref": ref, "kind": kind, "label": str(row.get("label") or row["ref"]),
                "concept": None, "statement": None,
                "unit": row.get("unit"), "status": ESTIMATED,
                "role": None, "spec_rows": [str(row["ref"])],
                "note": "the specification names no filed counterpart for this line",
                "history": [],
            }
            order.append(ref)
            continue
        concept = str(concept)
        ref = f"concept:{concept}"
        entry = drivers.get(ref)
        if entry is None:
            line = filed.get(concept) or {}
            cells = _cells_of(line, concept) if line.get("status") == FILED else []
            units = {str(cell.get("unit")) for cell in (line.get("cells") or {}).values()
                     if cell.get("unit")}
            statement = line.get("statement")
            statement = str(statement) if isinstance(statement, str) else None
            role = CONCEPT_ROLES.get(concept)
            if role is not None and statement != ROLE_STATEMENTS[role]:
                role = None
            entry = {
                "ref": ref, "kind": kind,
                "label": str(line.get("label") or row.get("label") or concept),
                "concept": concept, "statement": statement,
                # One unit or none. A line whose cells disagree about their
                # unit is not a series, and calling it one is how a figure in
                # thousands ends up added to a figure in dollars.
                "unit": units.pop() if len(units) == 1 else None,
                "status": str(line.get("status") or NOT_FOUND),
                "role": role,
                "spec_rows": [], "note": None, "history": cells,
            }
            drivers[ref] = entry
            order.append(ref)
        entry["spec_rows"].append(str(row["ref"]))
    for ref in order:
        entry = drivers[ref]
        entry["spec_rows"] = sorted(set(entry["spec_rows"]))
        if entry["concept"] is None:
            continue
        if entry["status"] == FILED and len(entry["spec_rows"]) > 1:
            entry["status"] = SHARED
            entry["note"] = (
                f"{len(entry['spec_rows'])} specification rows split this filed "
                "line; the total is forecast and the split is not filed"
            )
        elif entry["status"] == NOT_FOUND:
            entry["note"] = "this concept is not in the statements held"
        elif entry["status"] == AMBIGUOUS:
            entry["note"] = "this concept appears in more than one statement"
        if entry["status"] in FORECASTABLE_STATUSES and entry["role"] is None:
            entry["note"] = (
                (entry["note"] + "; ") if entry["note"] else ""
            ) + "this concept holds no frozen model role, so nothing is computed from it"
    return [drivers[ref] for ref in order]


def quarterly_history(driver: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The driver's duration cells. A balance is an instant and is not a flow."""

    return [cell for cell in (driver.get("history") or []) if cell.get("period_start")]


def role_drivers(drivers: Sequence[Mapping[str, Any]], role: str) -> list[Mapping[str, Any]]:
    return [item for item in drivers
            if item.get("role") == role and item.get("status") in FORECASTABLE_STATUSES]


def revenue_anchor(drivers: Sequence[Mapping[str, Any]]) -> Mapping[str, Any]:
    """The one filed concept this model calls revenue.

    Refused rather than chosen when two concepts claim the role. Picking the
    larger of them would be a guess that reads as a fact everywhere downstream,
    and every expense line in the model is a share of whatever this returns.
    """

    candidates = [item for item in role_drivers(drivers, REVENUE)
                  if quarterly_history(item)]
    if not candidates:
        raise ForecastModelUnavailable(
            "no revenue driver rests on a filed concept with quarterly history"
        )
    if len(candidates) > 1:
        names = ", ".join(sorted(str(item["concept"]) for item in candidates))
        raise ForecastModelUnavailable(
            f"more than one filed concept claims the revenue role: {names}"
        )
    return candidates[0]


def forecast_periods(
    anchor: Mapping[str, Any], count: int,
) -> list[dict[str, str]]:
    """The quarters after the anchor's last filed quarter."""

    if isinstance(count, bool) or not isinstance(count, int) or count < 1:
        raise ForecastModelValidationError("forecast_quarters must be a positive integer")
    count = min(int(count), MAX_FORECAST_PERIODS)
    cells = quarterly_history(anchor)
    if not cells:
        raise ForecastModelUnavailable("the revenue driver has no filed quarter to start from")
    last = cells[-1]
    period = {
        "start": _iso_date(last["period_start"], "period_start"),
        "end": _iso_date(last["period_end"], "period_end"),
        "calendar": "company:fiscal", "kind": "quarter",
    }
    out: list[dict[str, str]] = []
    for _ in range(count):
        period = quarter_after(period)
        out.append(dict(period))
    return out


# -- layer two: assumptions -------------------------------------------------


def _cell_ref(cell: Mapping[str, Any]) -> dict[str, Any]:
    accessions = cell.get("accessions") or []
    return {
        "kind": "input_cell", "ref": None, "concept": str(cell["concept"]),
        "period_end": str(cell["period_end"]),
        "accession": str(accessions[0]) if accessions else None,
    }


def trailing_growth(cells: Sequence[Mapping[str, Any]]) -> dict[str, Any] | None:
    """The average quarter-on-quarter change of the trailing quarters.

    Only pairs whose ends are one quarter apart count. A company whose history
    comes from 10-Qs has a hole where every fourth quarter should be, and
    treating the two figures either side of the hole as consecutive would put
    two quarters of growth into a one-quarter rate.
    """

    rates: list[tuple[Decimal, Mapping[str, Any], Mapping[str, Any]]] = []
    for prior, current in zip(cells, cells[1:]):
        gap = _days(str(prior["period_end"]), str(current["period_end"]))
        if not QUARTER_GAP_MIN_DAYS <= gap <= QUARTER_GAP_MAX_DAYS:
            continue
        base = _decimal(prior["value"], "history value")
        if base == 0:
            continue
        rate = (_decimal(current["value"], "history value") - base) / base
        rates.append((rate.quantize(_RATE_QUANT, ROUND_HALF_UP), prior, current))
    if not rates:
        return None
    used = rates[-TRAILING_QUARTERS:]
    mean = (sum((item[0] for item in used), Decimal(0)) / Decimal(len(used)))
    refs: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for _, prior, current in used:
        for cell in (prior, current):
            key = (str(cell["concept"]), str(cell["period_end"]))
            if key not in seen:
                seen.add(key)
                refs.append(_cell_ref(cell))
    return {
        "value": mean.quantize(_RATE_QUANT, ROUND_HALF_UP),
        "count": len(used),
        # The window is the whole span the changes were measured across, which
        # starts at the *earlier* quarter of the first pair. Naming only the
        # later one would describe a window a quarter shorter than the one that
        # was actually averaged.
        "first_period": str(used[0][1]["period_end"]),
        "last_period": str(used[-1][2]["period_end"]),
        "refs": refs,
    }


def trailing_share(
    cells: Sequence[Mapping[str, Any]], base_cells: Sequence[Mapping[str, Any]],
) -> dict[str, Any] | None:
    """The average share of the base line over the trailing quarters.

    Refused outright when the base changes sign inside the window. A company
    that lost money in one quarter and made money in the next has a tax rate of
    minus something and plus something, and their average is a number with no
    meaning at all -- it is not "the tax rate", it is an artefact of how far
    apart the loss and the profit happened to be. The quarters come back
    unassumed and the lines that needed them say they are unavailable, which is
    a hole a person can look at rather than a rate nobody can defend.
    """

    base_by_period = {str(cell["period_end"]): cell for cell in base_cells}
    shares: list[tuple[Decimal, Mapping[str, Any], Mapping[str, Any]]] = []
    for cell in cells:
        base = base_by_period.get(str(cell["period_end"]))
        if base is None:
            continue
        divisor = _decimal(base["value"], "history value")
        if divisor == 0:
            continue
        share = _decimal(cell["value"], "history value") / divisor
        shares.append((share.quantize(_RATE_QUANT, ROUND_HALF_UP), cell, base))
    if not shares:
        return None
    used = shares[-TRAILING_QUARTERS:]
    signs = {_decimal(base["value"], "history value") > 0 for _, _, base in used}
    if len(signs) > 1:
        return None
    mean = (sum((item[0] for item in used), Decimal(0)) / Decimal(len(used)))
    refs: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for _, cell, base in used:
        for item in (cell, base):
            key = (str(item["concept"]), str(item["period_end"]))
            if key not in seen:
                seen.add(key)
                refs.append(_cell_ref(item))
    return {
        "value": mean.quantize(_RATE_QUANT, ROUND_HALF_UP),
        "count": len(used),
        "first_period": str(used[0][1]["period_end"]),
        "last_period": str(used[-1][1]["period_end"]),
        "refs": refs,
    }


def _percent(value: Decimal) -> str:
    return format((value * Decimal(100)).quantize(Decimal("0.01"), ROUND_HALF_UP), "f")


def assumption_ref(driver_ref: str, period_end: str, kind: str) -> str:
    """One name per driver, quarter and kind.

    The kind is in the name because an estimate and the actual that replaced it
    live in the same record: the estimate is what reconciliation grades and
    what the guidance-style work reads, so it is never overwritten.
    """

    return f"assumption:{driver_ref}@{period_end}:{kind}"


def _assumption(
    *, driver_ref: str, period: Mapping[str, str], measure: str, value: Decimal,
    unit: str, because: str, refs: Sequence[Mapping[str, Any]], decided_by: str,
    kind: str = "estimate", rule_ref: str | None = GENERATOR_REF,
    work_order_ref: str | None = None,
) -> dict[str, Any]:
    return {
        "ref": assumption_ref(driver_ref, str(period["end"]), kind),
        "driver_ref": driver_ref,
        "period": dict(period),
        "measure": measure,
        "value": _rate(value),
        "unit": unit,
        "kind": kind,
        "because": because,
        "refs": [dict(item) for item in refs],
        "provenance": {
            "rule_ref": rule_ref, "work_order_ref": work_order_ref,
            "decided_by": decided_by,
        },
        "superseded_by": None,
    }


def default_assumptions(
    drivers: Sequence[Mapping[str, Any]],
    periods: Sequence[Mapping[str, str]],
    *,
    decided_by: str = AUTOMATION_ACTOR,
) -> list[dict[str, Any]]:
    """Trailing rates carried forward, one assumption per driver per quarter.

    Deliberately the dullest possible starting point, and it says so in every
    sentence it writes: this is what the last four quarters did, held flat. It
    is not a view. What makes it useful is that it is *complete and refutable*
    -- every quarter of every driver has a row a person can disagree with,
    with the filings it came from attached, rather than a blank a person has to
    notice is missing.
    """

    anchor = revenue_anchor(drivers)
    revenue_cells = quarterly_history(anchor)
    out: list[dict[str, Any]] = []
    with localcontext() as ctx:
        ctx.prec = _PRECISION
        for driver in drivers:
            if driver.get("status") not in FORECASTABLE_STATUSES:
                continue
            role = driver.get("role")
            if role is None:
                continue
            cells = quarterly_history(driver)
            if not cells:
                continue
            if role == REVENUE:
                trailing = trailing_growth(cells)
                if trailing is None:
                    continue
                because = (
                    f"the average of the {trailing['count']} quarter-on-quarter "
                    f"changes filed between {trailing['first_period']} and "
                    f"{trailing['last_period']} ({_percent(trailing['value'])}%), "
                    "carried forward unchanged; an average of quarters carries "
                    "no seasonality, so a quarter unlike the ones averaged will "
                    "be wrong by however much it is unlike them"
                )
                for period in periods:
                    out.append(_assumption(
                        driver_ref=str(driver["ref"]), period=period,
                        measure="quarterly_growth", value=trailing["value"],
                        unit="ratio", because=because, refs=trailing["refs"],
                        decided_by=decided_by,
                    ))
                continue
            base_cells = revenue_cells
            measure = "revenue_share"
            base_name = "revenue"
            if role in (INCOME_TAX, NET_INCOME):
                # Tax and the bottom line are a share of what the business
                # earned, not of what it sold; a share of revenue would move
                # with the margin and hide the thing being forecast.
                measure = "operating_income_share"
                base_name = "operating income"
                base_cells = _operating_income_history(drivers, revenue_cells)
                if not base_cells:
                    continue
            trailing = trailing_share(cells, base_cells)
            if trailing is None:
                continue
            because = (
                f"the average share of {base_name} over the {trailing['count']} "
                f"quarters filed between {trailing['first_period']} and "
                f"{trailing['last_period']} ({_percent(trailing['value'])}%), "
                "carried forward unchanged"
            )
            for period in periods:
                out.append(_assumption(
                    driver_ref=str(driver["ref"]), period=period,
                    measure=measure, value=trailing["value"], unit="ratio",
                    because=because, refs=trailing["refs"], decided_by=decided_by,
                ))
    return sorted(out, key=lambda item: (item["driver_ref"], item["period"]["end"]))


def _operating_income_history(
    drivers: Sequence[Mapping[str, Any]], revenue_cells: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Historical operating income, from the same chain the forecast uses.

    Not read from a filed operating-income line, because the model's operating
    income is revenue less the cost and expense lines the specification chose,
    and a ratio taken against a different definition would not replay.
    """

    cost = [item for item in role_drivers(drivers, COST_OF_REVENUE)]
    opex = [item for item in role_drivers(drivers, OPERATING_EXPENSE)]
    out: list[dict[str, Any]] = []
    for cell in revenue_cells:
        end = str(cell["period_end"])
        total = _decimal(cell["value"], "history value")
        complete = True
        for driver in cost + opex:
            match = next((item for item in quarterly_history(driver)
                          if str(item["period_end"]) == end), None)
            if match is None:
                complete = False
                break
            total -= _decimal(match["value"], "history value")
        if not complete:
            continue
        out.append({
            "concept": "derived:operating-income", "period_start": cell["period_start"],
            "period_end": end, "value": _plain(total), "basis": "derived_from_model",
            "accessions": list(cell.get("accessions") or []),
        })
    return out


class AssumptionRefused(ForecastModelError):
    """A drafted assumption was not accepted, and why."""


def draft_assumptions(
    drivers: Sequence[Mapping[str, Any]],
    periods: Sequence[Mapping[str, str]],
    *,
    drafter: Any,
    decided_by: str,
    work_order_ref: str | None = None,
) -> list[dict[str, Any]]:
    """The seam for a model-drafted assumption pass. Not used in this slice.

    ``drafter`` is any callable taking ``(drivers, periods)`` and returning
    assumption-shaped mappings. Every one of them is checked here before it can
    become part of a record, and a draft that fails any check is refused whole
    rather than repaired -- a drafted assumption with its unverifiable parts
    stripped out is no longer the assumption that was drafted.

    Three checks, and they are the reason this exists as a seam rather than as
    a later edit to ``default_assumptions``:

    * every draft names a driver in this model that has filed history, so a
      model cannot assume something about a line the filings do not have;
    * every draft's ``refs`` point at cells that are actually in that driver's
      history, so "because bookings grew" cannot cite a quarter nobody filed;
    * every draft is ``kind='estimate'`` with a ``because``. A model may never
      write a ``human`` assumption, whatever it says about itself.
    """

    known = {str(item["ref"]): item for item in drivers
             if item.get("status") in FORECASTABLE_STATUSES}
    wanted = {str(period["end"]) for period in periods}
    out: list[dict[str, Any]] = []
    for index, draft in enumerate(drafter(drivers, periods) or []):
        if not isinstance(draft, Mapping):
            raise AssumptionRefused(f"draft[{index}] is not an object")
        driver = known.get(str(draft.get("driver_ref")))
        if driver is None:
            raise AssumptionRefused(
                f"draft[{index}] names {draft.get('driver_ref')!r}, which is not a "
                "driver of this model with filed history")
        period = _period(draft.get("period"), f"draft[{index}].period")
        if period["end"] not in wanted:
            raise AssumptionRefused(
                f"draft[{index}] is for {period['end']}, which is not a forecast quarter")
        if draft.get("kind") != "estimate":
            raise AssumptionRefused(f"draft[{index}] must be an estimate")
        history = {str(cell["period_end"]) for cell in driver.get("history") or []}
        for ref in draft.get("refs") or []:
            if not isinstance(ref, Mapping) or ref.get("kind") != "input_cell":
                continue
            if str(ref.get("period_end")) not in history:
                raise AssumptionRefused(
                    f"draft[{index}] cites {ref.get('period_end')}, which this driver "
                    "has no filed cell for")
        out.append(_assumption(
            driver_ref=str(draft["driver_ref"]), period=period,
            measure=_one_of(draft.get("measure"), MEASURES, f"draft[{index}].measure"),
            value=_decimal(draft.get("value"), f"draft[{index}].value"),
            unit=_text(draft.get("unit"), f"draft[{index}].unit"),
            because=_text(draft.get("because"), f"draft[{index}].because"),
            refs=list(draft.get("refs") or []), decided_by=decided_by,
            rule_ref=None, work_order_ref=work_order_ref,
        ))
    return sorted(out, key=lambda item: (item["driver_ref"], item["period"]["end"]))


# -- layer three: results ---------------------------------------------------


def cell_ref(result_ref: str, period_end: str, kind: str) -> str:
    return f"{result_ref}@{period_end}:{kind}"


def _result(
    ref: str, role: str | None, label: str, unit: str, formula: str,
    *, driver_ref: str | None = None,
) -> dict[str, Any]:
    return {"ref": ref, "role": role, "label": label, "unit": unit,
            "formula": formula, "driver_ref": driver_ref,
            "status": "unavailable", "reason": None, "cells": []}


def _computed_cell(
    result_ref: str, period: Mapping[str, str], value: Decimal, *,
    kind: str = "estimate",
    assumptions: Sequence[str] = (), inputs: Sequence[Mapping[str, Any]] = (),
    results: Sequence[Mapping[str, str]] = (),
) -> dict[str, Any]:
    return {
        "ref": cell_ref(result_ref, str(period["end"]), kind),
        "period": dict(period), "kind": kind, "status": "computed",
        "value": _plain(value), "reason": None, "superseded_by": None,
        "assumption_refs": sorted(set(assumptions)),
        "input_cell_refs": [dict(item) for item in inputs],
        "result_refs": [dict(item) for item in results],
    }


def _unavailable_cell(
    result_ref: str, period: Mapping[str, str], reason: str, *,
    kind: str = "estimate",
) -> dict[str, Any]:
    return {
        "ref": cell_ref(result_ref, str(period["end"]), kind),
        "period": dict(period), "kind": kind, "status": "unavailable",
        "value": None, "reason": reason, "superseded_by": None,
        "assumption_refs": [], "input_cell_refs": [], "result_refs": [],
    }


def _finish(result: dict[str, Any]) -> dict[str, Any]:
    """The line's status, read off the cells that still stand.

    A superseded estimate is not a hole in the line -- it is a cell that was
    answered by a filing -- so it does not count against it either way.
    """

    live = [cell for cell in result["cells"] if not cell.get("superseded_by")]
    computed = [cell for cell in live if cell["status"] == "computed"]
    if not live or not computed:
        result["status"] = "unavailable"
        result["reason"] = next(
            (cell["reason"] for cell in live if cell["reason"]), result["reason"])
    elif len(computed) == len(live):
        result["status"] = "computed"
        result["reason"] = None
    else:
        result["status"] = "partial"
        result["reason"] = next(
            (cell["reason"] for cell in live if cell["reason"]), None)
    return result


def chain_base(
    anchor: Mapping[str, Any], prior: Mapping[str, Any] | None, first_end: str,
) -> dict[str, Any]:
    """Where the revenue chain starts, and why it is usually not the last filing.

    A model's revenue line is a chain: each quarter is the one before it times
    a growth assumption. When a quarter this model estimated is later filed,
    the estimate for the quarter *after* it must still rest on what we
    estimated, not on what the company reported -- otherwise revising one
    assumption would silently rebase the whole forecast onto the print, and a
    reader comparing two versions could not tell which change was the decision
    and which was the side effect.

    Rebasing onto an actual is a perfectly good thing to decide. It is a
    decision, so it arrives as a revision, not as arithmetic nobody chose.
    """

    if prior is not None:
        cells = [
            cell for line in (prior.get("results") or [])
            if str(line.get("ref")) == "result:revenue"
            for cell in (line.get("cells") or [])
            if cell.get("kind") == "estimate" and cell.get("status") == "computed"
            and str(cell["period"]["end"]) < str(first_end)
        ]
        if cells:
            cell = max(cells, key=lambda item: str(item["period"]["end"]))
            return {"value": str(cell["value"]), "input_cell": None,
                    "period_end": str(cell["period"]["end"])}
    filed = quarterly_history(anchor)[-1]
    return {"value": str(filed["value"]), "input_cell": dict(filed),
            "period_end": str(filed["period_end"])}


def compute_results(
    drivers: Sequence[Mapping[str, Any]],
    assumptions: Sequence[Mapping[str, Any]],
    periods: Sequence[Mapping[str, str]],
    *,
    statements: Mapping[str, str] | None = None,
    base: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """The income chain, and the cash chain when the specification asks for it.

    Everything is a function of the assumptions and the quarter the chain
    starts from, and every cell carries the refs it used. A missing input is
    never worked around: the cell is ``unavailable`` and says which driver or
    assumption was not there.
    """

    statements = dict(statements or {})
    by_driver: dict[str, dict[str, Mapping[str, Any]]] = {}
    for item in assumptions:
        if item.get("kind") == "actual" or item.get("superseded_by"):
            continue
        by_driver.setdefault(str(item["driver_ref"]), {})[str(item["period"]["end"])] = item
    unit = "USD"
    results: list[dict[str, Any]] = []

    with localcontext() as ctx:
        ctx.prec = _PRECISION
        try:
            anchor = revenue_anchor(drivers)
        except ForecastModelUnavailable as exc:
            revenue = _result("result:revenue", REVENUE, "Revenue", unit,
                              "revenue[k] = revenue[k-1] * (1 + growth[k])")
            for period in periods:
                revenue["cells"].append(
                    _unavailable_cell("result:revenue", period, str(exc)))
            return [_finish(revenue)]

        revenue = _result("result:revenue", REVENUE, "Revenue", unit,
                          "revenue[k] = revenue[k-1] * (1 + growth[k])",
                          driver_ref=str(anchor["ref"]))
        start = dict(base) if base is not None else chain_base(
            anchor, None, str(periods[0]["end"]) if periods else "9999-12-31")
        base_cell = start.get("input_cell")
        running = _decimal(start["value"], "base value")
        revenue_values: dict[str, Decimal] = {}
        anchor_assumptions = by_driver.get(str(anchor["ref"]), {})
        for index, period in enumerate(periods):
            assumption = anchor_assumptions.get(str(period["end"]))
            if assumption is None:
                revenue["cells"].append(_unavailable_cell(
                    "result:revenue", period,
                    f"no growth assumption for {anchor['concept']} in this quarter"))
                break
            rate = _decimal(assumption["value"], "assumption value")
            running = (running * (Decimal(1) + rate)).quantize(_VALUE_QUANT, ROUND_HALF_UP)
            revenue_values[str(period["end"])] = running
            if index:
                inputs, prior_end = [], str(periods[index - 1]["end"])
            elif base_cell is not None:
                inputs, prior_end = [_cell_ref(base_cell)], None
            else:
                inputs, prior_end = [], str(start["period_end"])
            revenue["cells"].append(_computed_cell(
                "result:revenue", period, running,
                assumptions=[str(assumption["ref"])], inputs=inputs,
                results=[] if prior_end is None else [
                    {"ref": "result:revenue", "period_end": prior_end}],
            ))
        results.append(_finish(revenue))

        def share_line(
            driver: Mapping[str, Any], ref: str, label: str, role: str,
            base_values: Mapping[str, Decimal], base_ref: str, base_label: str,
        ) -> tuple[dict[str, Any], dict[str, Decimal]]:
            line = _result(ref, role, label, unit,
                           f"{ref.split(':')[-1]}[k] = {base_label}[k] * share[k]",
                           driver_ref=str(driver["ref"]))
            values: dict[str, Decimal] = {}
            driver_assumptions = by_driver.get(str(driver["ref"]), {})
            for period in periods:
                end = str(period["end"])
                assumption = driver_assumptions.get(end)
                base = base_values.get(end)
                if assumption is None:
                    # Two ways to get here and the reader should look at the
                    # same place for both: too little filed history to average,
                    # or a base that changed sign inside the window, whose mean
                    # would be an artefact rather than a rate.
                    line["cells"].append(_unavailable_cell(
                        ref, period,
                        f"no share assumption for {driver['concept']} in this "
                        "quarter; its trailing history gives no usable rate"))
                    continue
                if base is None:
                    line["cells"].append(_unavailable_cell(
                        ref, period, f"{base_label} is not available for this quarter"))
                    continue
                value = (base * _decimal(assumption["value"], "assumption value")).quantize(
                    _VALUE_QUANT, ROUND_HALF_UP)
                values[end] = value
                line["cells"].append(_computed_cell(
                    ref, period, value, assumptions=[str(assumption["ref"])],
                    results=[{"ref": base_ref, "period_end": end}]))
            return _finish(line), values

        # Cost of revenue and gross profit.
        cost_values: dict[str, Decimal] = {}
        cost_drivers = role_drivers(drivers, COST_OF_REVENUE)
        if len(cost_drivers) != 1:
            cost = _result("result:cost_of_revenue", COST_OF_REVENUE,
                           "Cost of revenue", unit,
                           "cost_of_revenue[k] = revenue[k] * share[k]")
            reason = (
                "the specification binds no filed cost-of-revenue concept"
                if not cost_drivers else
                "more than one filed concept claims the cost-of-revenue role: "
                + ", ".join(sorted(str(item["concept"]) for item in cost_drivers))
            )
            for period in periods:
                cost["cells"].append(
                    _unavailable_cell("result:cost_of_revenue", period, reason))
            results.append(_finish(cost))
        else:
            cost, cost_values = share_line(
                cost_drivers[0], "result:cost_of_revenue", "Cost of revenue",
                COST_OF_REVENUE, revenue_values, "result:revenue", "revenue")
            results.append(cost)

        gross = _result("result:gross_profit", None, "Gross profit", unit,
                        "gross_profit[k] = revenue[k] - cost_of_revenue[k]")
        gross_values: dict[str, Decimal] = {}
        for period in periods:
            end = str(period["end"])
            if end in revenue_values and end in cost_values:
                value = revenue_values[end] - cost_values[end]
                gross_values[end] = value
                gross["cells"].append(_computed_cell(
                    "result:gross_profit", period, value, results=[
                        {"ref": "result:revenue", "period_end": end},
                        {"ref": "result:cost_of_revenue", "period_end": end}]))
            else:
                gross["cells"].append(_unavailable_cell(
                    "result:gross_profit", period,
                    "revenue or cost of revenue is not available for this quarter"))
        results.append(_finish(gross))

        # Operating expenses, one line each, and operating income.
        opex_values: dict[str, dict[str, Decimal]] = {}
        for driver in sorted(role_drivers(drivers, OPERATING_EXPENSE),
                             key=lambda item: str(item["concept"])):
            ref = f"result:operating_expense:{driver['concept']}"
            line, values = share_line(
                driver, ref, str(driver.get("label") or driver["concept"]),
                OPERATING_EXPENSE, revenue_values, "result:revenue", "revenue")
            results.append(line)
            opex_values[ref] = values

        operating = _result(
            "result:operating_income", None, "Operating income", unit,
            "operating_income[k] = gross_profit[k] - sum(operating_expense[k])")
        operating_values: dict[str, Decimal] = {}
        for period in periods:
            end = str(period["end"])
            if end not in gross_values:
                operating["cells"].append(_unavailable_cell(
                    "result:operating_income", period,
                    "gross profit is not available for this quarter"))
                continue
            missing = [ref for ref, values in opex_values.items() if end not in values]
            if missing:
                operating["cells"].append(_unavailable_cell(
                    "result:operating_income", period,
                    f"{len(missing)} operating expense lines are not available "
                    "for this quarter"))
                continue
            value = gross_values[end] - sum(
                (values[end] for values in opex_values.values()), Decimal(0))
            operating_values[end] = value
            operating["cells"].append(_computed_cell(
                "result:operating_income", period, value,
                results=[{"ref": "result:gross_profit", "period_end": end}]
                + [{"ref": ref, "period_end": end} for ref in sorted(opex_values)]))
        results.append(_finish(operating))

        # Net income: through tax when the specification models tax, else as a
        # share of operating income. The precedence is part of the formula.
        tax_drivers = role_drivers(drivers, INCOME_TAX)
        net_drivers = role_drivers(drivers, NET_INCOME)
        if len(tax_drivers) == 1:
            net = _result("result:net_income", NET_INCOME, "Net income", unit,
                          "net_income[k] = operating_income[k] - income_tax[k]")
            tax, tax_values = share_line(
                tax_drivers[0], "result:income_tax_expense", "Income tax", INCOME_TAX,
                operating_values, "result:operating_income", "operating_income")
            results.append(tax)
            for period in periods:
                end = str(period["end"])
                if end in operating_values and end in tax_values:
                    net["cells"].append(_computed_cell(
                        "result:net_income", period,
                        operating_values[end] - tax_values[end],
                        results=[
                            {"ref": "result:operating_income", "period_end": end},
                            {"ref": "result:income_tax_expense", "period_end": end}]))
                else:
                    net["cells"].append(_unavailable_cell(
                        "result:net_income", period,
                        "operating income or income tax is not available for this quarter"))
            results.append(_finish(net))
        elif len(net_drivers) == 1:
            net, _ = share_line(
                net_drivers[0], "result:net_income", "Net income", NET_INCOME,
                operating_values, "result:operating_income", "operating_income")
            results.append(net)
        else:
            net = _result("result:net_income", NET_INCOME, "Net income", unit,
                          "net_income[k] = operating_income[k] * net_income_share[k]")
            reason = (
                "the specification binds no filed income-tax or net-income concept"
                if not tax_drivers and not net_drivers else
                "more than one filed concept claims the income-tax or net-income role"
            )
            for period in periods:
                net["cells"].append(
                    _unavailable_cell("result:net_income", period, reason))
            results.append(_finish(net))

        # Cash. Only when the specification says this company's cash flow
        # statement is worth forecasting.
        cash_importance = str(statements.get("cash") or "not_material")
        fcf = _result("result:free_cash_flow", None, "Free cash flow", unit,
                      "free_cash_flow[k] = operating_cash_flow[k] - capital_expenditure[k]")
        if cash_importance == "not_material":
            for period in periods:
                fcf["cells"].append(_unavailable_cell(
                    "result:free_cash_flow", period,
                    "the specification marks the cash flow statement not_material"))
            results.append(_finish(fcf))
        else:
            ocf_drivers = role_drivers(drivers, OPERATING_CASH_FLOW)
            capex_drivers = role_drivers(drivers, CAPITAL_EXPENDITURE)
            ocf_values: dict[str, Decimal] = {}
            capex_values: dict[str, Decimal] = {}
            if len(ocf_drivers) == 1:
                line, ocf_values = share_line(
                    ocf_drivers[0], "result:operating_cash_flow", "Operating cash flow",
                    OPERATING_CASH_FLOW, revenue_values, "result:revenue", "revenue")
                results.append(line)
            if len(capex_drivers) == 1:
                line, capex_values = share_line(
                    capex_drivers[0], "result:capital_expenditure",
                    "Capital expenditure", CAPITAL_EXPENDITURE, revenue_values,
                    "result:revenue", "revenue")
                results.append(line)
            reason = None
            if len(ocf_drivers) != 1:
                reason = "the specification binds no single filed operating-cash-flow concept"
            elif len(capex_drivers) != 1:
                reason = "the specification binds no single filed capital-expenditure concept"
            for period in periods:
                end = str(period["end"])
                if reason is not None:
                    fcf["cells"].append(
                        _unavailable_cell("result:free_cash_flow", period, reason))
                elif end in ocf_values and end in capex_values:
                    fcf["cells"].append(_computed_cell(
                        "result:free_cash_flow", period,
                        ocf_values[end] - capex_values[end],
                        results=[
                            {"ref": "result:operating_cash_flow", "period_end": end},
                            {"ref": "result:capital_expenditure", "period_end": end}]))
                else:
                    fcf["cells"].append(_unavailable_cell(
                        "result:free_cash_flow", period,
                        "operating cash flow or capital expenditure is not "
                        "available for this quarter"))
            results.append(_finish(fcf))
    return results


# -- the record -------------------------------------------------------------


def statement_importances(spec: Mapping[str, Any]) -> dict[str, str]:
    return {
        str(item["statement"]): str(item.get("importance"))
        for item in (spec.get("forecast_statements") or [])
        if isinstance(item, Mapping) and item.get("statement")
    }


def filing_refs(drivers: Sequence[Mapping[str, Any]],
                period_ends: Sequence[str] | None = None) -> list[dict[str, Any]]:
    """The filings a set of cells rests on, as evidence refs.

    A version has to name what made it. For the first model and for an
    actualisation that is the accessions themselves; for a revision it is
    whatever the caller cites -- a Claim, an extracted figure, an event.
    """

    wanted = None if period_ends is None else {str(item) for item in period_ends}
    seen: set[tuple[str, str]] = set()
    refs: list[dict[str, Any]] = []
    for driver in drivers:
        for cell in driver.get("history") or []:
            if wanted is not None and str(cell["period_end"]) not in wanted:
                continue
            for accession in cell.get("accessions") or []:
                key = (str(accession), str(cell["period_end"]))
                if key in seen:
                    continue
                seen.add(key)
                refs.append({
                    "kind": "filing", "ref": None,
                    "concept": str(cell["concept"]),
                    "period_end": str(cell["period_end"]),
                    "accession": str(accession),
                })
    return sorted(refs, key=lambda item: (item["accession"], item["period_end"],
                                          item["concept"]))


def build_forecast_model(
    spec: Mapping[str, Any],
    table: Mapping[str, Any],
    *,
    actor_ref: str = AUTOMATION_ACTOR,
    mission_version_ref: str | None = None,
    assumptions: Sequence[Mapping[str, Any]] | None = None,
    generator_ref: str | None = GENERATOR_REF,
    change_reason: str = "evidence_thicker",
    evidence_refs: Sequence[Mapping[str, Any]] | None = None,
    decision: str | None = None,
) -> dict[str, Any]:
    """One company's first driver model, ready to be published as a version.

    Only the first: once a model exists, a new version is something a caller
    *decides* -- ``actualize_model`` when a filing settles a quarter this model
    estimated, ``revise_assumptions`` when something happened and someone
    decided what it means. Nothing in this module rebuilds a model because the
    ledger moved. A system that quietly re-forecast every time a document
    arrived would have no history of what it thought and when, which is the
    thing the version chain is for.

    Raises ``ForecastModelUnavailable`` when there is nothing to model -- no
    filed revenue concept, or two of them -- because a model whose top line is
    a guess is not worth versioning.
    """

    company_ref = _text(table.get("company_ref"), "company_ref")
    spec_ref = _text(table.get("spec_ref") or spec.get("spec_id"), "spec_ref")
    horizon = spec.get("horizon") or {}
    wanted = horizon.get("forecast_quarters")
    if isinstance(wanted, bool) or not isinstance(wanted, int) or wanted < 1:
        wanted = 4
    drivers = build_drivers(table)
    anchor = revenue_anchor(drivers)
    periods = forecast_periods(anchor, wanted)
    if assumptions is None:
        assumptions = default_assumptions(drivers, periods, decided_by=actor_ref)
    statements = statement_importances(spec)
    results = compute_results(drivers, assumptions, periods, statements=statements)
    unit = str(anchor.get("unit") or "usd")
    if evidence_refs is None:
        evidence_refs = filing_refs(drivers, table.get("periods"))
    return {
        # A first model expects to be the first: publishing it against a
        # company that already has one is a lost update, not a first model.
        SOURCE_VERSION_KEY: None,
        "schema_version": SCHEMA_VERSION,
        "model_ref": f"forecast-model:{company_ref}",
        "company_ref": company_ref,
        "spec_ref": spec_ref,
        "spec_hash": _sha256(spec.get("content_hash"), "spec.content_hash"),
        "inputs_hash": content_hash(json.loads(canonical_json(table))),
        "unit": unit,
        "currency": "USD",
        "history_periods": [str(item) for item in (table.get("periods") or [])],
        "realised_periods": [],
        "forecast_periods": [dict(item) for item in periods],
        "statements": statements,
        "formula_ref": FORMULA_REF,
        "formula_hash": FORMULA_HASH,
        "generator_ref": generator_ref,
        "drivers": drivers,
        "assumptions": [dict(item) for item in assumptions],
        "results": results,
        "change_reason": _one_of(change_reason, CHANGE_REASONS, "change_reason"),
        "evidence_refs": [dict(item) for item in evidence_refs],
        "decision": decision,
        "mission_version_ref": mission_version_ref,
        "actor_ref": actor_ref,
    }


# -- versioning: the two entry points a caller may use ----------------------
#
# Neither of these decides anything. ``actualize_model`` writes down what a
# filing said about a quarter this model had estimated, and touches no future
# period; ``revise_assumptions`` writes down what a caller decided, with the
# evidence that caller is citing. What a filing or an event *means* for the
# quarters still ahead is a judgement, and this layer does not make judgements.


def realised_ends(prior: Mapping[str, Any], table: Mapping[str, Any]) -> list[str]:
    """Forecast quarters of this model that the filings now cover."""

    filed = {str(item) for item in (table.get("periods") or [])}
    return [str(period["end"]) for period in (prior.get("forecast_periods") or [])
            if str(period["end"]) in filed]


def _driver_cell(drivers: Sequence[Mapping[str, Any]], driver_ref: str,
                 period_end: str) -> Mapping[str, Any] | None:
    for driver in drivers:
        if str(driver["ref"]) != driver_ref:
            continue
        for cell in quarterly_history(driver):
            if str(cell["period_end"]) == period_end:
                return cell
    return None


def _prior_quarter_cell(drivers: Sequence[Mapping[str, Any]], driver_ref: str,
                        period_end: str) -> Mapping[str, Any] | None:
    for driver in drivers:
        if str(driver["ref"]) != driver_ref:
            continue
        cells = quarterly_history(driver)
        for prior, current in zip(cells, cells[1:]):
            if str(current["period_end"]) != period_end:
                continue
            gap = _days(str(prior["period_end"]), period_end)
            if QUARTER_GAP_MIN_DAYS <= gap <= QUARTER_GAP_MAX_DAYS:
                return prior
    return None


def actualize_model(
    prior: Mapping[str, Any],
    table: Mapping[str, Any],
    *,
    evidence_refs: Sequence[Mapping[str, Any]] | None = None,
    actor_ref: str | None = None,
) -> dict[str, Any] | None:
    """Write the filed answer beside the estimate, and change nothing else.

    The one near-mechanical version in this layer. A quarter this model
    forecast has been filed; the estimate stays exactly where it is, marked
    ``superseded_by`` the actual that answered it, and the actual arrives with
    the accession it came from. Future quarters are not touched, not rebased
    and not re-estimated -- whether a print changes the view of next year is a
    judgement, and it belongs to whoever makes it, not to this function.

    Returns ``None`` when no forecast quarter has been filed yet.
    """

    ends = realised_ends(prior, table)
    if not ends:
        return None
    if str(table.get("spec_ref")) != str(prior.get("spec_ref")):
        raise ForecastModelUnavailable(
            "the specification changed, so this is a new model rather than an "
            "actualisation of the old one")
    drivers = build_drivers(table)
    known = {str(item["ref"]) for item in drivers}
    for item in prior.get("drivers") or []:
        if str(item["ref"]) not in known:
            raise ForecastModelUnavailable(
                f"driver {item['ref']} is no longer in the input table")
    realised = set(ends)
    periods_by_end = {str(item["end"]): dict(item)
                      for item in (prior.get("forecast_periods") or [])}

    with localcontext() as ctx:
        ctx.prec = _PRECISION
        # The results first: an actual is a filed figure, so it rests on cells
        # and on other actuals, never on an assumption.
        actual_values: dict[str, dict[str, Decimal]] = {}
        results: list[dict[str, Any]] = []
        by_ref = {str(item["ref"]): item for item in (prior.get("results") or [])}
        for line in prior.get("results") or []:
            ref = str(line["ref"])
            driver_ref = line.get("driver_ref")
            cells: list[dict[str, Any]] = []
            for cell in line.get("cells") or []:
                end = str(cell["period"]["end"])
                if end not in realised or cell.get("kind") != "estimate":
                    cells.append(dict(cell))
                    continue
                filed = (None if not driver_ref
                         else _driver_cell(drivers, str(driver_ref), end))
                if filed is None:
                    cells.append(dict(cell))
                    continue
                value = _decimal(filed["value"], "filed value")
                actual = _computed_cell(
                    ref, periods_by_end[end], value, kind="actual",
                    inputs=[_cell_ref(filed)])
                actual_values.setdefault(end, {})[ref] = value
                cells.append({**dict(cell), "superseded_by": actual["ref"]})
                cells.append(actual)
            line = {**dict(line), "cells": cells}
            results.append(line)

        # Then the lines that are differences of other lines: an actual gross
        # profit is the actual revenue less the actual cost, and it exists only
        # when both of those do.
        opex_refs = sorted(ref for ref in by_ref
                           if ref.startswith("result:operating_expense:"))
        derived: list[tuple[str, list[str], list[str]]] = [
            ("result:gross_profit", ["result:revenue"], ["result:cost_of_revenue"]),
            ("result:operating_income", ["result:gross_profit"], opex_refs),
        ]
        if "result:income_tax_expense" in by_ref:
            derived.append(("result:net_income", ["result:operating_income"],
                            ["result:income_tax_expense"]))
        derived.append(("result:free_cash_flow", ["result:operating_cash_flow"],
                        ["result:capital_expenditure"]))
        for ref, plus, minus in derived:
            line = next((item for item in results if str(item["ref"]) == ref), None)
            if line is None:
                continue
            cells = []
            for cell in line["cells"]:
                end = str(cell["period"]["end"])
                have = actual_values.get(end, {})
                if (end not in realised or cell.get("kind") != "estimate"
                        or cell.get("superseded_by")
                        or any(item not in have for item in plus + minus)):
                    cells.append(cell)
                    continue
                value = (sum((have[item] for item in plus), Decimal(0))
                         - sum((have[item] for item in minus), Decimal(0)))
                actual = _computed_cell(
                    ref, periods_by_end[end], value, kind="actual",
                    results=[{"ref": item, "period_end": end}
                             for item in plus + minus])
                have[ref] = value
                cells.append({**cell, "superseded_by": actual["ref"]})
                cells.append(actual)
            line["cells"] = cells
        for line in results:
            line["cells"] = sorted(
                line["cells"],
                key=lambda cell: (str(cell["period"]["end"]),
                                  0 if cell["kind"] == "estimate" else 1))
            _finish(line)

        # And the assumptions: what the rate turned out to be, beside what we
        # said it would be.
        assumptions: list[dict[str, Any]] = []
        for item in prior.get("assumptions") or []:
            end = str(item["period"]["end"])
            if end not in realised or item.get("kind") == "actual" or item.get(
                    "superseded_by"):
                assumptions.append(dict(item))
                continue
            driver_ref = str(item["driver_ref"])
            filed = _driver_cell(drivers, driver_ref, end)
            actual = None
            if filed is not None:
                value = _decimal(filed["value"], "filed value")
                refs = [_cell_ref(filed)]
                rate: Decimal | None = None
                if str(item["measure"]) == "quarterly_growth":
                    base = _prior_quarter_cell(drivers, driver_ref, end)
                    if base is not None:
                        divisor = _decimal(base["value"], "filed value")
                        if divisor != 0:
                            rate = (value - divisor) / divisor
                            refs.append(_cell_ref(base))
                else:
                    base_ref = ("result:operating_income"
                                if str(item["measure"]) == "operating_income_share"
                                else "result:revenue")
                    divisor = actual_values.get(end, {}).get(base_ref)
                    if divisor is not None and divisor != 0:
                        rate = value / divisor
                if rate is not None:
                    actual = _assumption(
                        driver_ref=driver_ref, period=periods_by_end[end],
                        measure=str(item["measure"]),
                        value=rate.quantize(_RATE_QUANT, ROUND_HALF_UP),
                        unit=str(item["unit"]), kind="actual",
                        because=(
                            f"what the filings reported for the quarter ended {end}"),
                        refs=refs,
                        decided_by=str(actor_ref or prior.get("actor_ref")),
                        rule_ref=None)
            assumptions.append({**dict(item),
                                "superseded_by": None if actual is None else actual["ref"]})
            if actual is not None:
                assumptions.append(actual)

    keep = (list(prior.get("realised_periods") or [])
            + [periods_by_end[end] for end in ends])[-MAX_REALISED_PERIODS:]
    forecast = [dict(item) for item in (prior.get("forecast_periods") or [])
                if str(item["end"]) not in realised]
    if evidence_refs is None:
        evidence_refs = filing_refs(drivers, ends)
    body = {
        # The version this was derived from, checked at publish time. Without
        # it, actualising an old version after someone revised a newer one
        # would append a version silently reverting the revision.
        SOURCE_VERSION_KEY: str(prior["id"]),
        **{key: value for key, value in prior.items()
           if key in _RECORD_FIELDS and key not in _BODY_EXCLUDED},
        "history_periods": [str(item) for item in (table.get("periods") or [])],
        "realised_periods": keep,
        "forecast_periods": forecast,
        "inputs_hash": content_hash(json.loads(canonical_json(table))),
        "drivers": drivers,
        "assumptions": sorted(assumptions, key=lambda item: (
            item["driver_ref"], item["period"]["end"], item["kind"])),
        "results": results,
        "actor_ref": str(actor_ref or prior["actor_ref"]),
    }
    body["change_reason"] = "filing_actual"
    body["evidence_refs"] = [dict(item) for item in evidence_refs]
    body["decision"] = None
    body["mission_version_ref"] = prior.get("mission_version_ref")
    return body


def revise_assumptions(
    prior: Mapping[str, Any],
    changes: Sequence[Mapping[str, Any]],
    *,
    change_reason: str,
    evidence_refs: Sequence[Mapping[str, Any]],
    actor_ref: str,
    decision: str | None = None,
    mission_version_ref: str | None = None,
) -> dict[str, Any]:
    """Move one or more assumptions, and recompute what depends on them.

    The caller supplies the reason and the evidence; this function supplies the
    arithmetic and nothing else. A revision with no evidence refs is refused,
    because a forecast that moved for no citable reason is the thing this whole
    layer exists to make impossible. A ``driver_event`` revision must also say
    what was decided -- Wave 3's event layer will put one of the five decision
    words there, and until then a caller says it in a sentence.

    Realised quarters are not revisable: what was filed was filed.
    """

    change_reason = _one_of(change_reason, CHANGE_REASONS, "change_reason")
    actor_ref = _text(actor_ref, "actor_ref")
    if not actor_ref.startswith(("human:", "automation:")):
        raise ForecastModelValidationError("actor_ref must use a principal namespace")
    refs = [_normalize_ref(item, f"evidence_refs[{index}]")
            for index, item in enumerate(evidence_refs or [])]
    if not refs:
        raise ForecastModelValidationError(
            "a revision must cite the evidence it rests on")
    if change_reason == "driver_event" and not (decision or "").strip():
        raise ForecastModelValidationError(
            "a driver_event revision must say what was decided")
    if not changes:
        raise ForecastModelValidationError("a revision must change something")

    periods_by_end = {str(item["end"]): dict(item)
                      for item in (prior.get("forecast_periods") or [])}
    realised = {str(item["end"]) for item in (prior.get("realised_periods") or [])}
    kind = "human" if actor_ref.startswith("human:") else "estimate"
    assumptions = [dict(item) for item in (prior.get("assumptions") or [])]
    index = {(str(item["driver_ref"]), str(item["period"]["end"])): position
             for position, item in enumerate(assumptions)
             if item.get("kind") in ("estimate", "human") and not item.get("superseded_by")}
    for position, change in enumerate(changes):
        driver_ref = _text(change.get("driver") or change.get("driver_ref"),
                           f"changes[{position}].driver")
        end = _iso_date(change.get("period") if not isinstance(change.get("period"), Mapping)
                        else change["period"]["end"], f"changes[{position}].period")
        if end in realised:
            raise ForecastModelValidationError(
                f"changes[{position}] revises {end}, which the filings already answered")
        if end not in periods_by_end:
            raise ForecastModelValidationError(
                f"changes[{position}] revises {end}, which this model does not forecast")
        held = index.get((driver_ref, end))
        if held is None:
            raise ForecastModelValidationError(
                f"changes[{position}] names no assumption of this model")
        existing = assumptions[held]
        change_refs = [_normalize_ref(item, f"changes[{position}].refs[{position2}]")
                       for position2, item in enumerate(change.get("refs") or refs)]
        assumptions[held] = _assumption(
            driver_ref=driver_ref, period=periods_by_end[end],
            measure=str(existing["measure"]),
            value=_decimal(change.get("value"), f"changes[{position}].value"),
            unit=str(existing["unit"]), kind=kind,
            because=_text(change.get("because"), f"changes[{position}].because"),
            refs=change_refs, decided_by=actor_ref, rule_ref=None,
            work_order_ref=change.get("work_order_ref"))

    forecast = [periods_by_end[end] for end in sorted(periods_by_end)]
    drivers = prior.get("drivers") or []
    recomputed = compute_results(
        drivers,
        [item for item in assumptions
         if str(item["period"]["end"]) in periods_by_end],
        forecast, statements=prior.get("statements") or {},
        base=chain_base(revenue_anchor(drivers), prior, str(forecast[0]["end"])))
    # Everything that is not a quarter being recomputed, not merely the
    # quarters still listed as realised. The realised list is capped, and a
    # cell whose quarter has aged out of it is still a cell: dropping it would
    # delete the estimate and the actual beside it, which is the one pair the
    # whole layer exists to keep.
    carried: dict[str, list[dict[str, Any]]] = {}
    for line in prior.get("results") or []:
        kept = [dict(cell) for cell in (line.get("cells") or [])
                if str(cell["period"]["end"]) not in periods_by_end]
        if kept:
            carried[str(line["ref"])] = kept
    results = []
    for line in recomputed:
        cells = carried.pop(str(line["ref"]), []) + list(line["cells"])
        line["cells"] = sorted(cells, key=lambda cell: (
            str(cell["period"]["end"]), 0 if cell["kind"] == "estimate" else 1))
        results.append(_finish(line))
    # A line the recomputation no longer produces but which holds settled
    # quarters keeps them. Dropping it would lose an estimate and the actual
    # beside it because the forecast ahead of it stopped being computable.
    for line in prior.get("results") or []:
        kept = carried.pop(str(line["ref"]), None)
        if kept:
            results.append(_finish({**dict(line), "cells": kept}))

    return {
        SOURCE_VERSION_KEY: str(prior["id"]),
        **{key: value for key, value in prior.items()
           if key in _RECORD_FIELDS and key not in _BODY_EXCLUDED},
        "assumptions": sorted(assumptions, key=lambda item: (
            item["driver_ref"], item["period"]["end"], item["kind"])),
        "results": results,
        "actor_ref": actor_ref,
        "change_reason": change_reason,
        "evidence_refs": refs,
        "decision": None if decision is None else _text(decision, "decision"),
        "mission_version_ref": (mission_version_ref
                                if mission_version_ref is not None
                                else prior.get("mission_version_ref")),
    }


def replay_cell(
    versions: Sequence[Mapping[str, Any]], result_ref: str, period_end: str,
) -> list[dict[str, Any]]:
    """What every version of this model said about one quarter of one line.

    The question the version chain exists to answer: what did we estimate for
    the fourth quarter, when did we say it, and what did it turn out to be.
    """

    out: list[dict[str, Any]] = []
    for record in versions:
        line = next((item for item in (record.get("results") or [])
                     if str(item["ref"]) == result_ref), None)
        if line is None:
            continue
        for cell in line.get("cells") or []:
            if str(cell["period"]["end"]) != str(period_end):
                continue
            out.append({
                "version": record.get("version"),
                "version_ref": record.get("id"),
                "change_reason": record.get("change_reason"),
                "kind": cell["kind"],
                "status": cell["status"],
                "value": cell["value"],
                "superseded_by": cell.get("superseded_by"),
            })
    return out


def model_readiness(record: Mapping[str, Any]) -> dict[str, Any]:
    """What this model has and what it is still missing, counted plainly.

    The same refusal the input table makes: no score. "Eighty percent modelled"
    invites a reader to accept a model with no cost line in it.
    """

    drivers = list(record.get("drivers") or [])
    assumed = {str(item["driver_ref"]) for item in (record.get("assumptions") or [])}
    results = list(record.get("results") or [])
    return {
        "forecast_quarters": len(record.get("forecast_periods") or []),
        "history_quarters": len(record.get("history_periods") or []),
        "drivers": len(drivers),
        "drivers_with_history": sum(1 for item in drivers if item.get("history")),
        "drivers_with_assumptions": len(assumed),
        "drivers_without_history": sorted(
            str(item["ref"]) for item in drivers if not item.get("history")),
        "drivers_without_a_role": sorted(
            str(item["ref"]) for item in drivers
            if item.get("role") is None and item.get("status") in FORECASTABLE_STATUSES),
        "results_computed": sorted(
            str(item["ref"]) for item in results if item.get("status") == "computed"),
        "results_partial": sorted(
            str(item["ref"]) for item in results if item.get("status") == "partial"),
        "results_unavailable": sorted(
            str(item["ref"]) for item in results if item.get("status") == "unavailable"),
        "assumption_kinds": {
            kind: sum(1 for item in (record.get("assumptions") or [])
                      if item.get("kind") == kind)
            for kind in ASSUMPTION_KINDS
        },
        "realised_quarters": len(record.get("realised_periods") or []),
        "actual_cells": sum(
            1 for line in results for cell in (line.get("cells") or [])
            if cell.get("kind") == "actual"),
        "superseded_estimates": sum(
            1 for line in results for cell in (line.get("cells") or [])
            if cell.get("superseded_by")),
    }


def _normalize_driver(value: Any, name: str) -> dict[str, Any]:
    wire = _closed(value, _DRIVER_FIELDS, name)
    wire["ref"] = _text(wire["ref"], f"{name}.ref")
    wire["kind"] = _one_of(wire["kind"], DRIVER_KINDS, f"{name}.kind")
    wire["label"] = _text(wire["label"], f"{name}.label")
    wire["concept"] = _optional_text(wire["concept"], f"{name}.concept")
    wire["statement"] = _optional_text(wire["statement"], f"{name}.statement")
    wire["unit"] = _optional_text(wire["unit"], f"{name}.unit")
    wire["status"] = _one_of(wire["status"], DRIVER_STATUSES, f"{name}.status")
    if wire["role"] is not None:
        wire["role"] = _one_of(wire["role"], ROLES, f"{name}.role")
    wire["note"] = _optional_text(wire["note"], f"{name}.note")
    if not isinstance(wire["spec_rows"], list) or not wire["spec_rows"]:
        raise ForecastModelValidationError(f"{name}.spec_rows must name a specification row")
    wire["spec_rows"] = [_text(item, f"{name}.spec_rows[]") for item in wire["spec_rows"]]
    if not isinstance(wire["history"], list):
        raise ForecastModelValidationError(f"{name}.history must be a list")
    cells = []
    for index, cell in enumerate(wire["history"]):
        item = _closed(cell, _CELL_FIELDS, f"{name}.history[{index}]")
        item["concept"] = _text(item["concept"], f"{name}.history[{index}].concept")
        item["period_start"] = (
            None if item["period_start"] is None
            else _iso_date(item["period_start"], f"{name}.history[{index}].period_start"))
        item["period_end"] = _iso_date(item["period_end"], f"{name}.history[{index}].period_end")
        item["value"] = format(_decimal(item["value"], f"{name}.history[{index}].value"), "f")
        item["basis"] = _text(item["basis"], f"{name}.history[{index}].basis")
        item["accessions"] = [
            _text(entry, f"{name}.history[{index}].accessions[]")
            for entry in (item["accessions"] or [])]
        cells.append(item)
    if wire["concept"] is None and cells:
        raise ForecastModelValidationError(
            f"{name} has no filed concept and cannot carry filed history")
    wire["history"] = cells
    return wire


def _normalize_ref(value: Any, name: str) -> dict[str, Any]:
    wire = _closed(value, _REF_FIELDS, name)
    wire["kind"] = _one_of(wire["kind"], REF_KINDS, f"{name}.kind")
    wire["ref"] = _optional_text(wire["ref"], f"{name}.ref")
    wire["concept"] = _optional_text(wire["concept"], f"{name}.concept")
    wire["period_end"] = (
        None if wire["period_end"] is None
        else _iso_date(wire["period_end"], f"{name}.period_end"))
    wire["accession"] = _optional_text(wire["accession"], f"{name}.accession")
    if wire["kind"] == "input_cell" and (wire["concept"] is None or wire["period_end"] is None):
        raise ForecastModelValidationError(f"{name} must name a concept and a period")
    if wire["kind"] in ("claim", "figure") and wire["ref"] is None:
        raise ForecastModelValidationError(f"{name} must name the thing it cites")
    return wire


def _normalize_assumption(value: Any, name: str, *, driver_refs: set[str]) -> dict[str, Any]:
    wire = _closed(value, _ASSUMPTION_FIELDS, name)
    wire["ref"] = _text(wire["ref"], f"{name}.ref")
    wire["driver_ref"] = _text(wire["driver_ref"], f"{name}.driver_ref")
    if wire["driver_ref"] not in driver_refs:
        raise ForecastModelValidationError(
            f"{name}.driver_ref names no driver of this model")
    wire["period"] = _period(wire["period"], f"{name}.period")
    wire["measure"] = _one_of(wire["measure"], MEASURES, f"{name}.measure")
    wire["value"] = format(_decimal(wire["value"], f"{name}.value"), "f")
    wire["unit"] = _text(wire["unit"], f"{name}.unit")
    wire["kind"] = _one_of(wire["kind"], ASSUMPTION_KINDS, f"{name}.kind")
    if wire["ref"] != assumption_ref(wire["driver_ref"], wire["period"]["end"],
                                     wire["kind"]):
        raise ForecastModelValidationError(
            f"{name}.ref must name its driver, quarter and kind")
    wire["because"] = _text(wire["because"], f"{name}.because")
    if not isinstance(wire["refs"], list):
        raise ForecastModelValidationError(f"{name}.refs must be a list")
    wire["refs"] = [_normalize_ref(item, f"{name}.refs[{index}]")
                    for index, item in enumerate(wire["refs"])]
    provenance = _closed(wire["provenance"], _PROVENANCE_FIELDS, f"{name}.provenance")
    provenance["rule_ref"] = _optional_text(provenance["rule_ref"], f"{name}.provenance.rule_ref")
    provenance["work_order_ref"] = _optional_text(
        provenance["work_order_ref"], f"{name}.provenance.work_order_ref")
    provenance["decided_by"] = _text(provenance["decided_by"], f"{name}.provenance.decided_by")
    if not provenance["decided_by"].startswith(("human:", "automation:")):
        raise ForecastModelValidationError(
            f"{name}.provenance.decided_by must use a principal namespace")
    if wire["kind"] == "human" and not provenance["decided_by"].startswith("human:"):
        raise ForecastModelValidationError(
            f"{name} is a human assumption and must name the person who decided it")
    # An assumption with no refs is allowed exactly once: a pure carry-forward
    # that says in its own sentence that it is one. Anything else with an empty
    # refs list is a number nobody has to defend.
    if not wire["refs"] and "carried forward" not in wire["because"]:
        raise ForecastModelValidationError(
            f"{name} cites nothing, so its because must say it is carried forward")
    # An actual is not an opinion: it is a filed figure, and it says which
    # filing it came from or it is not an actual.
    if wire["kind"] == "actual" and not any(
        item["kind"] == "input_cell" and item["accession"] for item in wire["refs"]
    ):
        raise ForecastModelValidationError(
            f"{name} is an actual and must cite the filing it came from")
    wire["superseded_by"] = _optional_text(wire["superseded_by"], f"{name}.superseded_by")
    if wire["superseded_by"] is not None and wire["kind"] == "actual":
        raise ForecastModelValidationError(
            f"{name} is an actual and cannot have been superseded")
    wire["provenance"] = provenance
    return wire


def _normalize_result(value: Any, name: str, *, assumption_refs: set[str],
                      driver_refs: set[str]) -> dict[str, Any]:
    wire = _closed(value, _RESULT_FIELDS, name)
    wire["ref"] = _text(wire["ref"], f"{name}.ref")
    wire["driver_ref"] = _optional_text(wire["driver_ref"], f"{name}.driver_ref")
    if wire["driver_ref"] is not None and wire["driver_ref"] not in driver_refs:
        raise ForecastModelValidationError(
            f"{name}.driver_ref names no driver of this model")
    if wire["role"] is not None:
        wire["role"] = _one_of(wire["role"], ROLES, f"{name}.role")
    wire["label"] = _text(wire["label"], f"{name}.label")
    wire["unit"] = _text(wire["unit"], f"{name}.unit")
    wire["formula"] = _text(wire["formula"], f"{name}.formula")
    wire["status"] = _one_of(wire["status"], RESULT_STATUSES, f"{name}.status")
    wire["reason"] = _optional_text(wire["reason"], f"{name}.reason")
    if not isinstance(wire["cells"], list):
        raise ForecastModelValidationError(f"{name}.cells must be a list")
    cells = []
    for index, cell in enumerate(wire["cells"]):
        item = _closed(cell, _RESULT_CELL_FIELDS, f"{name}.cells[{index}]")
        item["ref"] = _text(item["ref"], f"{name}.cells[{index}].ref")
        item["period"] = _period(item["period"], f"{name}.cells[{index}].period")
        item["kind"] = _one_of(item["kind"], CELL_KINDS, f"{name}.cells[{index}].kind")
        if item["ref"] != cell_ref(wire["ref"], item["period"]["end"], item["kind"]):
            raise ForecastModelValidationError(
                f"{name}.cells[{index}].ref must name its line, quarter and kind")
        item["superseded_by"] = _optional_text(
            item["superseded_by"], f"{name}.cells[{index}].superseded_by")
        item["status"] = _one_of(item["status"], CELL_STATUSES, f"{name}.cells[{index}].status")
        if item["kind"] == "actual":
            if item["superseded_by"] is not None:
                raise ForecastModelValidationError(
                    f"{name}.cells[{index}] is an actual and cannot have been superseded")
            if item["status"] == "computed" and not (
                any(ref.get("accession") for ref in (item["input_cell_refs"] or []))
                or item["result_refs"]
            ):
                raise ForecastModelValidationError(
                    f"{name}.cells[{index}] is an actual and must cite a filing or "
                    "the actuals it was computed from")
        if item["status"] == "computed":
            item["value"] = format(
                _decimal(item["value"], f"{name}.cells[{index}].value"), "f")
            if item["reason"] is not None:
                raise ForecastModelValidationError(
                    f"{name}.cells[{index}] is computed and carries no reason")
        else:
            if item["value"] is not None:
                raise ForecastModelValidationError(
                    f"{name}.cells[{index}] is unavailable and carries no value")
            item["reason"] = _text(item["reason"], f"{name}.cells[{index}].reason")
        item["assumption_refs"] = [
            _text(entry, f"{name}.cells[{index}].assumption_refs[]")
            for entry in (item["assumption_refs"] or [])]
        for entry in item["assumption_refs"]:
            if entry not in assumption_refs:
                raise ForecastModelValidationError(
                    f"{name}.cells[{index}] cites an assumption this model does not carry")
        item["input_cell_refs"] = [
            _normalize_ref(entry, f"{name}.cells[{index}].input_cell_refs[{position}]")
            for position, entry in enumerate(item["input_cell_refs"] or [])]
        item["result_refs"] = [
            _closed(entry, _RESULT_REF_FIELDS,
                    f"{name}.cells[{index}].result_refs[{position}]")
            for position, entry in enumerate(item["result_refs"] or [])]
        cells.append(item)
    seen = {item["ref"] for item in cells}
    for item in cells:
        if item["superseded_by"] is not None and item["superseded_by"] not in seen:
            raise ForecastModelValidationError(
                f"{name} supersedes a cell that is not in this line")
    if len(seen) != len(cells):
        raise ForecastModelValidationError(f"{name} carries the same cell twice")
    wire["cells"] = cells
    return wire


def validate_forecast_model(value: Mapping[str, Any]) -> dict[str, Any]:
    """The whole record, checked against its closed shape and its own hashes."""

    wire = _closed(value, _RECORD_FIELDS, "forecast model")
    if wire["schema_version"] != SCHEMA_VERSION:
        raise ForecastModelValidationError("forecast model schema_version is not 0.1")
    for field in ("id", "created_at", "model_ref", "company_ref", "spec_ref",
                  "unit", "currency", "actor_ref"):
        wire[field] = _text(wire[field], field)
    wire["change_reason"] = _one_of(wire["change_reason"], CHANGE_REASONS, "change_reason")
    wire["decision"] = _optional_text(wire["decision"], "decision")
    if wire["change_reason"] == "driver_event" and wire["decision"] is None:
        raise ForecastModelValidationError(
            "a driver_event version must say what was decided")
    if not isinstance(wire["evidence_refs"], list) or not wire["evidence_refs"]:
        raise ForecastModelValidationError(
            "a version must name the evidence that triggered it")
    wire["evidence_refs"] = [
        _normalize_ref(item, f"evidence_refs[{index}]")
        for index, item in enumerate(wire["evidence_refs"])]
    if not isinstance(wire["statements"], Mapping):
        raise ForecastModelValidationError("statements must be an object")
    wire["statements"] = {
        _text(key, "statements key"): _text(value, f"statements[{key}]")
        for key, value in wire["statements"].items()}
    if not wire["id"].startswith(DRIVER_MODEL_VERSION_PREFIX):
        raise ForecastModelValidationError(
            f"forecast model id must start with {DRIVER_MODEL_VERSION_PREFIX}")
    if wire["model_ref"] != f"forecast-model:{wire['company_ref']}":
        raise ForecastModelValidationError("model_ref must name this company")
    if type(wire["version"]) is not int or wire["version"] < 1:
        raise ForecastModelValidationError("version must be a positive integer")
    wire["prior_version_ref"] = _optional_text(wire["prior_version_ref"], "prior_version_ref")
    if (wire["version"] == 1) != (wire["prior_version_ref"] is None):
        raise ForecastModelValidationError(
            "only the first version has no prior version, and it must have none")
    wire["spec_hash"] = _sha256(wire["spec_hash"], "spec_hash")
    wire["inputs_hash"] = _sha256(wire["inputs_hash"], "inputs_hash")
    wire["mission_version_ref"] = _optional_text(
        wire["mission_version_ref"], "mission_version_ref")
    if not wire["actor_ref"].startswith(("human:", "automation:")):
        raise ForecastModelValidationError("actor_ref must use a principal namespace")
    wire["generator_ref"] = _optional_text(wire["generator_ref"], "generator_ref")
    if wire["formula_ref"] != FORMULA_REF or wire["formula_hash"] != FORMULA_HASH:
        raise ForecastModelValidationError("forecast model must bind the frozen formula")
    wire["history_periods"] = [
        _iso_date(item, "history_periods[]") for item in (wire["history_periods"] or [])]
    for field in ("realised_periods", "forecast_periods"):
        if not isinstance(wire[field], list):
            raise ForecastModelValidationError(f"{field} must be a list")
        wire[field] = [_period(item, f"{field}[{index}]")
                       for index, item in enumerate(wire[field])]
    # A model with nothing ahead of it and nothing behind it is not a model.
    # One with only realised quarters is: it is a model whose horizon ran out,
    # and it is still the record of what it estimated and what happened.
    if not wire["forecast_periods"] and not wire["realised_periods"]:
        raise ForecastModelValidationError("a model must carry at least one quarter")
    if len(wire["forecast_periods"]) > MAX_FORECAST_PERIODS:
        raise ForecastModelValidationError(
            f"a model may forecast at most {MAX_FORECAST_PERIODS} quarters")
    if len(wire["realised_periods"]) > MAX_REALISED_PERIODS:
        raise ForecastModelValidationError(
            f"a model may carry at most {MAX_REALISED_PERIODS} realised quarters")
    overlap = ({item["end"] for item in wire["realised_periods"]}
               & {item["end"] for item in wire["forecast_periods"]})
    if overlap:
        raise ForecastModelValidationError(
            "a quarter cannot be both realised and forecast")

    if not isinstance(wire["drivers"], list) or not wire["drivers"]:
        raise ForecastModelValidationError("a model must carry at least one driver")
    drivers = [_normalize_driver(item, f"drivers[{index}]")
               for index, item in enumerate(wire["drivers"])]
    driver_refs = {item["ref"] for item in drivers}
    if len(driver_refs) != len(drivers):
        raise ForecastModelValidationError("driver refs must be unique")
    wire["drivers"] = drivers

    if not isinstance(wire["assumptions"], list):
        raise ForecastModelValidationError("assumptions must be a list")
    assumptions = [
        _normalize_assumption(item, f"assumptions[{index}]", driver_refs=driver_refs)
        for index, item in enumerate(wire["assumptions"])]
    assumption_refs = {item["ref"] for item in assumptions}
    if len(assumption_refs) != len(assumptions):
        raise ForecastModelValidationError("assumption refs must be unique")
    # The rule the whole layer exists for: automation may write an estimate and
    # a filed actual, and nothing else. A record an automation principal
    # published that carries a ``human`` assumption is a record claiming a
    # person decided something. An ``actual`` is not a judgement -- it is a
    # figure with an accession on it, checked above -- so automation writing
    # one is automation writing down what a filing said.
    if wire["actor_ref"].startswith("automation:"):
        for item in assumptions:
            if item["kind"] == "human":
                raise ForecastModelValidationError(
                    "automation may not write a human assumption")
    for item in assumptions:
        if item["superseded_by"] is not None and item["superseded_by"] not in assumption_refs:
            raise ForecastModelValidationError(
                "an assumption supersedes something this model does not carry")
    wire["assumptions"] = assumptions

    if not isinstance(wire["results"], list):
        raise ForecastModelValidationError("results must be a list")
    wire["results"] = [
        _normalize_result(item, f"results[{index}]", assumption_refs=assumption_refs,
                          driver_refs=driver_refs)
        for index, item in enumerate(wire["results"])]
    result_refs = {item["ref"] for item in wire["results"]}
    if len(result_refs) != len(wire["results"]):
        raise ForecastModelValidationError("result refs must be unique")

    body = {key: value for key, value in wire.items() if key not in _BODY_EXCLUDED}
    expected_body = content_hash(body)
    if _sha256(wire["body_hash"], "body_hash") != expected_body:
        raise ForecastModelValidationError("forecast model body_hash is invalid")
    expected = _sha256(wire["content_hash"], "content_hash")
    base = {key: value for key, value in wire.items() if key != "content_hash"}
    if content_hash(base) != expected:
        raise ForecastModelValidationError("forecast model content_hash is invalid")
    return wire


def body_hash(body: Mapping[str, Any]) -> str:
    """What makes two models the same model: everything but when and who asked."""

    return content_hash({key: value for key, value in body.items()
                         if key not in _BODY_EXCLUDED and key in _RECORD_FIELDS})


class ForecastModelAuthority:
    """Append-only ForecastModelVersions, one chain per company."""

    _authorized = authorized_flag()

    def __init__(self, store: DaltonStore):
        self.store = store
        self.connection = store.connection
        self._authorization_flag = authorization_flag(
            self.connection, "dalton_forecast_model_authorized")
        self.connection.executescript(_SCHEMA_PATH.read_text(encoding="utf-8"))

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Cursor]:
        if self._authorized:
            raise RuntimeError("ForecastModelAuthority operation cannot be nested")
        self._authorized = True
        try:
            with self.store._transaction() as cur:
                yield cur
        finally:
            self._authorized = False

    def publish(self, body: Mapping[str, Any]) -> dict[str, Any]:
        """Store one model version, or say it is the one already stored.

        The body carries its own ``change_reason``, ``evidence_refs`` and, for
        an event-driven revision, the ``decision`` that was taken: a version
        that cannot say what made it exist is refused rather than stored, and
        a body identical to the stored one is a ``duplicate`` however good the
        reason. The version chain is the record of how the view of a company
        moved, and a new version every tick saying exactly what the last one
        said would bury the two that did not.
        """

        body = dict(body)
        source = body.pop(SOURCE_VERSION_KEY, _UNSET)
        for field in _BODY_EXCLUDED - {"mission_version_ref", "change_reason",
                                       "evidence_refs", "decision"}:
            body.pop(field, None)
        company_ref = _text(body.get("company_ref"), "company_ref")
        model_ref = f"forecast-model:{company_ref}"
        body["model_ref"] = model_ref
        change_reason = _one_of(body.get("change_reason"), CHANGE_REASONS, "change_reason")
        if not body.get("evidence_refs"):
            raise ForecastModelValidationError(
                "a version must name the evidence that triggered it")
        digest = body_hash(body)
        latest = self.connection.execute(
            "SELECT * FROM forecast_model_versions WHERE model_ref=? "
            "ORDER BY version_number DESC LIMIT 1", (model_ref,),
        ).fetchone()
        if latest is not None and latest["body_hash"] == digest:
            return {**self.model(latest["version_id"]), "status": "duplicate"}
        # Lost update. A caller computed this from some version; if the chain
        # has moved since, appending it would quietly undo whatever moved it --
        # and the record would carry the *caller's* change_reason for a change
        # nobody made. The caller has to recompute against the new head.
        head = None if latest is None else str(latest["version_id"])
        if source is not _UNSET and source != head:
            raise ForecastModelConflict(
                f"this model is now at {head or 'no version'}, and this body was "
                f"computed from {source or 'no version'}"
            )
        version = 1 if latest is None else int(latest["version_number"]) + 1
        prior = None if latest is None else latest["version_id"]
        version_id = f"{DRIVER_MODEL_VERSION_PREFIX}{company_slug(company_ref)}:{version}"
        record = {
            **body,
            "id": version_id,
            "created_at": _now(),
            "version": version,
            "prior_version_ref": prior,
            "change_reason": change_reason,
            "body_hash": digest,
        }
        record.setdefault("decision", None)
        record["content_hash"] = content_hash(record)
        wire = validate_forecast_model(record)
        with self._transaction() as cur:
            if cur.execute(
                "SELECT 1 FROM forecast_model_versions WHERE version_id=?", (version_id,)
            ).fetchone():
                raise ForecastModelConflict("forecast model version id already exists")
            cur.execute(
                "INSERT INTO forecast_model_versions"
                "(version_id,model_ref,version_number,prior_version_id,company_ref,"
                "spec_ref,inputs_hash,body_hash,record_json,content_hash,actor_ref,"
                "created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    version_id, model_ref, version, prior, company_ref,
                    wire["spec_ref"], wire["inputs_hash"], digest,
                    canonical_json(wire), wire["content_hash"], wire["actor_ref"],
                    wire["created_at"],
                ),
            )
        # Read back through the same path a reader would take, so a record that
        # cannot be read is a failed write rather than a stored surprise.
        stored = self.model(version_id)
        if stored["content_hash"] != wire["content_hash"]:
            raise ForecastModelConflict("forecast model did not read back as written")
        return {**stored, "status": "fresh"}

    def model(self, version_ref: str) -> dict[str, Any]:
        version_ref = _text(version_ref, "version_ref")
        row = self.connection.execute(
            "SELECT * FROM forecast_model_versions WHERE version_id=?", (version_ref,)
        ).fetchone()
        if row is None:
            raise ForecastModelNotFound("forecast model version was not found")
        wire = validate_forecast_model(json.loads(row["record_json"]))
        if (
            wire["id"] != row["version_id"]
            or wire["model_ref"] != row["model_ref"]
            or wire["version"] != row["version_number"]
            or wire["prior_version_ref"] != row["prior_version_id"]
            or wire["company_ref"] != row["company_ref"]
            or wire["body_hash"] != row["body_hash"]
            or wire["content_hash"] != row["content_hash"]
        ):
            raise ForecastModelConflict("forecast model authority drifted")
        return wire

    def latest(self, company_ref: str) -> dict[str, Any] | None:
        company_ref = _text(company_ref, "company_ref")
        row = self.connection.execute(
            "SELECT version_id FROM forecast_model_versions WHERE company_ref=? "
            "ORDER BY version_number DESC LIMIT 1", (company_ref,),
        ).fetchone()
        return None if row is None else self.model(row["version_id"])

    def versions(self, company_ref: str) -> list[dict[str, Any]]:
        company_ref = _text(company_ref, "company_ref")
        rows = self.connection.execute(
            "SELECT version_id FROM forecast_model_versions WHERE company_ref=? "
            "ORDER BY version_number", (company_ref,),
        ).fetchall()
        return [self.model(row["version_id"]) for row in rows]

    def companies(self) -> list[str]:
        return [str(row["company_ref"]) for row in self.connection.execute(
            "SELECT DISTINCT company_ref FROM forecast_model_versions "
            "ORDER BY company_ref").fetchall()]


__all__ = [
    "AUTOMATION_ACTOR",
    "ASSUMPTION_KINDS",
    "CELL_KINDS",
    "CHANGE_REASONS",
    "CONCEPT_ROLES",
    "MAX_REALISED_PERIODS",
    "actualize_model",
    "assumption_ref",
    "cell_ref",
    "chain_base",
    "filing_refs",
    "realised_ends",
    "replay_cell",
    "revise_assumptions",
    "statement_importances",
    "FORMULA_HASH",
    "FORMULA_REF",
    "GENERATOR_REF",
    "MEASURES",
    "ROLES",
    "ROLE_STATEMENTS",
    "SCHEMA_VERSION",
    "TRAILING_QUARTERS",
    "AssumptionRefused",
    "ForecastModelAuthority",
    "ForecastModelConflict",
    "ForecastModelError",
    "ForecastModelNotFound",
    "ForecastModelUnavailable",
    "ForecastModelValidationError",
    "SOURCE_VERSION_KEY",
    "body_hash",
    "build_drivers",
    "company_slug",
    "build_forecast_model",
    "compute_results",
    "default_assumptions",
    "draft_assumptions",
    "forecast_periods",
    "model_readiness",
    "quarterly_history",
    "revenue_anchor",
    "role_drivers",
    "trailing_growth",
    "trailing_share",
    "validate_forecast_model",
]
