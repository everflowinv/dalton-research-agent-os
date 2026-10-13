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
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP, localcontext
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence

from .claim_index_authority import MARKET_PROXY
from .company_model_inputs import (
    AMBIGUOUS, CASH_FLOW_ROLE_CONCEPTS, ESTIMATED, FILED, INCOMPLETE, NET_INCOME_CONCEPTS,
    NOT_FOUND,
    SHARED, _model_unit, cash_quarter_windows_are_unique,
)
from .driver_template import COST_DRIVER_TEMPLATES
from .model_forecast import (
    DRIVER_FORMULA_HASH,
    DRIVER_FORMULA_REF,
    DRIVER_MODEL_VERSION_PREFIX,
    quarter_after,
)
from .store import (
    DaltonStore, authorization_flag, authorized_flag, canonical_json, content_hash,
)

SCHEMA_VERSION = "0.2"
LEGACY_SCHEMA_VERSION = "0.1"
STRUCTURED_SCHEMA_VERSION = "0.3"
CASH_FLOW_COMPANION_VALIDATION_CONTRACT_HASH = content_hash({
    "contract": "cash-flow-companion-pre-persistence-validation:0.2",
})
STRUCTURED_CASH_SCHEMA_VERSION = "0.4"
STRUCTURED_SCHEMA_VERSIONS = frozenset({
    STRUCTURED_SCHEMA_VERSION, STRUCTURED_CASH_SCHEMA_VERSION,
})


def is_structured_schema(value: Any) -> bool:
    return value in STRUCTURED_SCHEMA_VERSIONS


def _filing_units_match(source: Any, model: Any, *, structured: bool) -> bool:
    """Compare the stored SEC unit with the unit carried by model history."""

    if not structured:
        return str(source) == str(model)
    return _model_unit(source).casefold() == _model_unit(model).casefold()


def _cumulative_source_pair_matches(
    candidates: Sequence[Mapping[str, Any]],
    filings: Mapping[str, Mapping[str, Any]],
    cell: Mapping[str, Any],
) -> bool:
    """Find an exact cumulative pair without depending on statement-row order."""

    try:
        cell_start = date.fromisoformat(str(cell["period_start"]))
        cell_end = str(cell["period_end"])
        cell_value = _decimal(cell.get("value"), "derived cumulative value")
    except (KeyError, TypeError, ValueError, ForecastModelValidationError):
        return False
    cell_accessions = set(map(str, cell.get("accessions") or []))
    for earlier in candidates:
        for later in candidates:
            if earlier is later:
                continue
            try:
                adjacent = (
                    date.fromisoformat(str(earlier["period_end"]))
                    + timedelta(days=1) == cell_start
                )
                earlier_value = _decimal(
                    earlier.get("value"), "earlier cumulative value")
                later_value = _decimal(
                    later.get("value"), "later cumulative value")
                pair_accessions = {
                    str(filings[str(row["ingest_id"])]["accession"])
                    for row in (earlier, later)
                }
            except (KeyError, TypeError, ValueError, ForecastModelValidationError):
                continue
            if (
                adjacent
                and earlier.get("period_start") == later.get("period_start")
                and str(later.get("period_end")) == cell_end
                and pair_accessions == cell_accessions
                and later_value - earlier_value == cell_value
            ):
                return True
    return False


FORMULA_REF = DRIVER_FORMULA_REF
FORMULA_HASH = DRIVER_FORMULA_HASH
STRUCTURE_FORMULA_REF = "formula:company-financial-statement-dag:0.1"
STRUCTURE_EXECUTOR_CONTRACT = {
    "schema_version": "0.1",
    "ref": STRUCTURE_FORMULA_REF,
    "operators": ["sum", "divide"],
    "missing_input": "unavailable",
    "zero_denominator": "unavailable",
    "forecast_leaves": ["quarterly_growth", "share_of_line", "unavailable"],
}
CASH_FLOW_COMPANION_REF = "formula:cash-flow-companion:0.1"
CASH_FLOW_COMPANION_CONTRACT = {
    "schema_version": "cash-flow-companion-0.1",
    "authority_ref": CASH_FLOW_COMPANION_REF,
    "forecast_methods": ["share_of_line", "unavailable"],
    "lines": ["operating_cash_flow", "capital_expenditure"],
    "free_cash_flow_terms": [
        {"role": "operating_cash_flow", "coefficient": "1"},
        {"role": "capital_expenditure", "coefficient": "-1"},
    ],
    "missing_input": "unavailable",
}
GENERATOR_REF = "rule:trailing-carry-forward:1"
STRUCTURE_GENERATOR_REF = "rule:structured-positive-trailing-carry-forward:2"
# A 0.6 (coverage) structure: every reported line has a method, and every
# assumption names its source in the order company guidance > consensus >
# analyst judgement > trailing history (a company's preliminary 8-K actual,
# when one exists for a forecast quarter, outranks all four).
# :4 (2026-10-09, P0): one-off items are taken out of the base before growth
# and ratio methods; operating costs may not outgrow revenue by more than
# OPEX_EXCESS_PP for more than OPEX_EXCESS_QUARTERS without an analyst
# assumption; revenue guidance ranges bound the forecast; operating cash flow
# is net income + D&A + working capital when those lines exist, and a
# share-of-revenue cash flow that contradicts the model's losses is
# unavailable.  A model built under :3 is rebuilt once (``pending_action``).
COVERAGE_GENERATOR_REF = "rule:structured-coverage-priority:6"
STRUCTURE_COVERAGE_EXECUTOR_CONTRACT = {
    "schema_version": "0.2",
    "ref": STRUCTURE_FORMULA_REF,
    "operators": ["sum", "divide"],
    "missing_input": "unavailable",
    "zero_denominator": "unavailable",
    "forecast_leaves": [
        "quarterly_growth", "share_of_line", "carry_forward_mean", "zero_one_off",
        "rate_times_balance", "hold_last", "roll_forward", "unavailable",
    ],
    "rate_times_balance_base": "the base line's forecast, or its last filed value "
                               "when the base is the rolled cash balance",
    "cash_roll": "cash[k] = cash[k-1] + free_cash_flow[k] - share_repurchase[k] "
                 "- dividends_paid[k] - acquisitions[k]",
}
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
MIN_FORECAST_PERIODS = 8
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
for _cash_role, _cash_concepts in CASH_FLOW_ROLE_CONCEPTS.items():
    for _cash_concept in _cash_concepts:
        CONCEPT_ROLES[_cash_concept] = _cash_role
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

DRIVER_KINDS: tuple[str, ...] = ("revenue", "expense", "cash_flow")
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
    "quarterly_growth", "year_over_year_growth", "seasonal_factor", "reported_actual", "mechanism_amount", "economic_driver_level",
    # A share of forecast revenue.
    "revenue_share",
    # A share of forecast operating income.
    "operating_income_share",
    # The base is frozen on the structure-backed driver rather than inferred
    # from this token.
    "share_of_line",
    # Coverage (structure 0.6) leaves: an amount held at the trailing mean, a
    # one-off held at zero, a rate on a balance-sheet line, a balance held.
    "carry_forward_mean", "zero_one_off", "rate_times_balance", "hold_last",
)
# What an assumption may stand on. ``market_proxy`` is W4's addition and the
# Chem retrospective (§7.2) is why: a spread, a list price or a futures
# continuation is a real public number that is *not* this company's realised
# figure, and an assumption that files one beside a filed cell has quietly
# asserted the company earned it. It is admitted as evidence -- refusing it
# would mean forecasting a commodity producer with nothing but its own lagging
# disclosure -- but it is admitted with a rule: see ``PROXY_GAP_REQUIRED``.
REF_KINDS: tuple[str, ...] = (
    "input_cell", "claim", "figure", "prior_period", "filing", "event",
    "human_decision", "market_proxy", "doc_passage",
)
#: The ref kinds that may not be cited without saying how far the quantity sits
#: from what the company realises. One word, and it is the whole point of the
#: kind: "MDI--benzene spread, which is a chemical margin and not a plant cash
#: margin -- it excludes utilities, freight and the discount on contract
#: volumes" is a sentence a reader can argue with, and a bare spread is not.
PROXY_GAP_REQUIRED: frozenset[str] = frozenset({MARKET_PROXY})
MAX_PROXY_GAP_CHARS = 400
CELL_STATUSES: tuple[str, ...] = ("computed", "unavailable")
RESULT_STATUSES: tuple[str, ...] = ("computed", "partial", "unavailable")

_RECORD_FIELDS = frozenset({
    "schema_version", "id", "created_at", "model_ref", "version",
    "prior_version_ref", "change_reason", "evidence_refs", "decision", "company_ref",
    "spec_ref", "spec_hash", "inputs_hash", "unit", "currency",
    "history_periods", "realised_periods", "forecast_periods", "statements",
    "formula_ref", "formula_hash", "generator_ref", "drivers", "assumptions",
    "results", "mission_version_ref", "actor_ref", "body_hash", "content_hash",
    "forecast_policy", "execution_contract",
})
_STRUCTURE_RECORD_FIELDS = frozenset({
    "financial_statement_structure", "financial_statement_structure_replay",
    "forecast_structure_binding",
})
_CASH_COMPANION_RECORD_FIELDS = frozenset({"cash_flow_companion"})
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
_DRIVER_OPTIONAL_FIELDS = frozenset({"cost_driver_slots", "statement_identity"})
_STRUCTURE_DRIVER_FIELDS = frozenset({
    "structure_line_ref", "forecast_method", "forecast_base_ref",
})
_CELL_FIELDS = frozenset({
    "concept", "period_start", "period_end", "value", "basis", "accessions",
})
_CELL_OPTIONAL_FIELDS = frozenset({"source_forms", "derived_from"})
_DERIVED_OPERAND_FIELDS = frozenset({
    "period_start", "period_end", "value", "unit", "accession", "form",
})
_ASSUMPTION_FIELDS = frozenset({
    "ref", "driver_ref", "period", "measure", "value", "unit", "kind",
    "because", "refs", "provenance", "superseded_by", "outside_band",
})
_OUTSIDE_BAND_FIELDS = frozenset({"reason"})
_REF_FIELDS = frozenset({"kind", "ref", "concept", "period_end", "accession"})
# Present only where it means something -- see ``_closed``.
_REF_OPTIONAL_FIELDS = frozenset({"proxy_gap"})
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


class ForecastDriverExecutionRefused(ForecastModelValidationError):
    """A replayed driver contract that cannot yet be published."""
    def __init__(self, message: str, *, driver_report: Mapping[str, Any], contract_hash: str):
        import copy
        super().__init__(message)
        self.driver_report = copy.deepcopy(dict(driver_report))
        self.contract_hash = contract_hash


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


def _closed(
    value: Any, fields: frozenset[str], name: str,
    *, optional: frozenset[str] = frozenset(),
) -> dict[str, Any]:
    """The shape, exactly, plus any of ``optional`` that happen to be present.

    ``optional`` exists for one field and is deliberately narrow. ``proxy_gap``
    means something only on a ``market_proxy`` ref, and normalising it to
    ``None`` on every other ref would change the body bytes -- and therefore
    the content hash -- of every model already recorded, producing a version
    chain step in which nothing about the company changed. A field that is
    absent where it has no meaning keeps those chains still.
    """

    if not isinstance(value, Mapping):
        raise ForecastModelValidationError(f"{name} must be an object")
    wire = dict(value)
    present = set(wire)
    if present - optional != fields:
        raise ForecastModelValidationError(
            f"{name} has an invalid closed shape; "
            f"missing={sorted(fields - present)}, "
            f"unknown={sorted(present - fields - optional)}"
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
            **({"source_forms": [str(item) for item in cell["source_forms"]]}
               if cell.get("source_forms") else {}),
            **({"derived_from": [dict(item) for item in cell["derived_from"]]}
               if cell.get("derived_from") else {}),
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

    from .driver_template import COST_REGISTRY_HASH, COST_REGISTRY_REF, cost_slot_ids

    cost_rows = [row for row in (table.get("rows") or ())
                 if "cost_driver_slot" in row]
    metadata = table.get("cost_driver_template")
    if cost_rows:
        if not isinstance(metadata, Mapping):
            raise ForecastModelValidationError(
                "cost-bound inputs carry no cost template metadata")
        if (metadata.get("registry_ref") != COST_REGISTRY_REF
                or metadata.get("registry_hash") != COST_REGISTRY_HASH):
            raise ForecastModelValidationError(
                "cost-bound inputs carry stale cost template metadata")
        allowed = set(cost_slot_ids(metadata.get("classification")))
        for row in cost_rows:
            slot = row.get("cost_driver_slot")
            if slot is not None and slot not in allowed:
                raise ForecastModelValidationError(
                    f"expense row {row.get('ref')} binds {slot!r} outside its cost template")
            if slot is None and not row.get("cost_driver_unbound_reason"):
                raise ForecastModelValidationError(
                    f"expense row {row.get('ref')} is unbound without a reason")
    elif metadata is not None:
        raise ForecastModelValidationError(
            "cost template metadata has no classified expense rows")

    filed = {str(item["concept"]): item for item in (table.get("filed_lines") or [])}
    drivers: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for cash in table.get("cash_flow_inputs") or []:
        if cash.get("status") != FILED:
            continue
        concept = str(cash["concept"])
        ref = f"concept:{concept}"
        quarters = list((cash.get("series") or {}).get("quarters") or [])
        drivers[ref] = {
            "ref": ref, "kind": "cash_flow", "label": concept,
            "concept": concept, "statement": "cash", "unit": cash.get("unit"),
            "status": FILED, "role": str(cash["role"]),
            "spec_rows": [f"cash-flow:{cash['role']}"], "note": None,
            "history": [{
                "concept": concept, "period_start": item.get("period_start"),
                "period_end": str(item["period_end"]), "value": str(item["value"]),
                "basis": str(item["basis"]),
                "accessions": [str(ref) for ref in item.get("source_accessions") or []],
                "source_forms": [str(form) for form in item.get("source_forms") or []],
                **({"derived_from": [dict(operand)
                                     for operand in item["derived_from"]]}
                   if item.get("derived_from") else {}),
            } for item in quarters],
        }
        order.append(ref)
    for row in table.get("rows") or []:
        kind = (
            "revenue"
            if row.get("kind") in ("revenue_driver", "revenue_anchor")
            else "expense"
        )
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
            if row.get("cost_driver_slot"):
                drivers[ref]["cost_driver_slots"] = [str(row["cost_driver_slot"])]
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
            # Net income is a result, never summed into a cost line, and the
            # exact concept is the same fact on either statement; a parser that
            # files it only under cash flow (Amazon) must not lose the role.
            if role is not None and statement != ROLE_STATEMENTS[role] and not (
                    role == NET_INCOME and statement == "cash"
                    and concept in NET_INCOME_CONCEPTS):
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
                "_cost_driver_slots": [],
            }
            drivers[ref] = entry
            order.append(ref)
        entry["spec_rows"].append(str(row["ref"]))
        if row.get("cost_driver_slot"):
            entry["_cost_driver_slots"].append(str(row["cost_driver_slot"]))
    for ref in order:
        entry = drivers[ref]
        entry["spec_rows"] = sorted(set(entry["spec_rows"]))
        cost_slots = sorted(set(entry.pop("_cost_driver_slots", [])))
        if cost_slots:
            entry["cost_driver_slots"] = cost_slots
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


def build_structure_drivers(
    table: Mapping[str, Any], structure: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Build forecast leaves from the exact company statement structure.

    The legacy driver builder starts from the specification's revenue and
    expense rows.  A company statement DAG also contains filed non-operating,
    tax, attribution and per-share inputs.  Those leaves must come from the
    structure's explicit concept and forecast method; a role never silently
    chooses a method or a base.
    """

    from .company_financial_statement_structure import (
        _units, _TYPED_NOTE_STRUCTURE_VERSIONS,
    )
    filed = {str(item.get("concept")): item for item in (table.get("filed_lines") or [])}
    drivers: list[dict[str, Any]] = []
    concepts: set[str] = set()
    for line in structure.get("lines") or []:
        if line.get("kind") != "filed":
            continue
        concept = str(line.get("concept") or "")
        source = filed.get(concept)
        if not concept or source is None or source.get("status") != FILED:
            raise ForecastModelUnavailable(
                f"statement line {line.get('ref')} has no exact filed concept")
        if concept in concepts:
            raise ForecastModelUnavailable(
                f"filed concept {concept} appears in more than one statement line")
        concepts.add(concept)
        cells = _cells_of(source, concept)
        # Typed source validation can prove a unit from held cumulative facts.
        # Those facts are not quarters and must never enter this driver's history.
        units = _units(source, include_duration=(
            structure.get("schema_version") in _TYPED_NOTE_STRUCTURE_VERSIONS))
        unit = str(line.get("unit") or "").casefold()
        if units != {unit}:
            raise ForecastModelUnavailable(
                f"statement line {line.get('ref')} unit differs from its filed concept")
        drivers.append({
            "ref": f"concept:{concept}",
            "kind": "statement_line",
            "label": str(line.get("label") or line.get("ref")),
            "concept": concept,
            "unit": unit,
            "statement": str(line.get("statement")),
            "status": FILED,
            "role": str(line.get("role")),
            "spec_rows": [str(line.get("ref"))],
            "note": None,
            "history": cells,
            "structure_line_ref": str(line.get("ref")),
            "forecast_method": str(line.get("forecast_method")),
            "forecast_base_ref": line.get("forecast_base_ref"),
        })
    if not drivers:
        raise ForecastModelUnavailable("statement structure has no filed forecast leaves")
    return drivers


def _structure_historical_values(
    drivers: Sequence[Mapping[str, Any]], structure: Mapping[str, Any],
) -> tuple[dict[str, dict[str, Decimal]], dict[str, dict[str, list[dict[str, Any]]]]]:
    """Replay line values and their filed-cell refs without filling a term."""

    by_line = {str(item.get("structure_line_ref")): item for item in drivers}
    values: dict[str, dict[str, Decimal]] = {}
    refs: dict[str, dict[str, list[dict[str, Any]]]] = {}
    for line_ref, driver in by_line.items():
        values[line_ref] = {}
        refs[line_ref] = {}
        # A balance-sheet leaf is a series of instants, not quarters.
        history = (driver.get("history") or []) if driver.get("statement") == "balance" \
            else quarterly_history(driver)
        for cell in history:
            end = str(cell["period_end"])
            values[line_ref][end] = _decimal(cell["value"], "history value")
            refs[line_ref][end] = [_cell_ref(cell)]
    pending = {str(item["output_ref"]): item for item in (structure.get("formulas") or [])}
    while pending:
        progressed = False
        for output, formula in list(pending.items()):
            dependencies = (
                [str(item["line_ref"]) for item in formula["terms"]]
                if formula.get("operator") == "sum"
                else [str(formula["numerator_ref"]), str(formula["denominator_ref"])]
            )
            if any(ref not in values for ref in dependencies):
                continue
            ends = set(values[dependencies[0]])
            for ref in dependencies[1:]:
                ends &= set(values[ref])
            values[output], refs[output] = {}, {}
            for end in sorted(ends):
                if formula.get("operator") == "sum":
                    value = sum(
                        (_decimal(term["coefficient"], "coefficient")
                         * values[str(term["line_ref"])][end]
                         for term in formula["terms"]), Decimal(0))
                else:
                    divisor = values[str(formula["denominator_ref"])][end]
                    if divisor == 0:
                        continue
                    value = values[str(formula["numerator_ref"])][end] / divisor
                values[output][end] = value
                combined: list[dict[str, Any]] = []
                seen: set[tuple[Any, ...]] = set()
                for ref in dependencies:
                    for item in refs[ref][end]:
                        key = tuple(item.get(name) for name in
                                    ("kind", "concept", "period_end", "accession"))
                        if key not in seen:
                            seen.add(key)
                            combined.append(dict(item))
                refs[output][end] = combined
            composition = structure.get("historical_income_composition")
            if composition is not None and composition["output_ref"] == output:
                from .filed_period_composition import historical_replay
                exact = historical_replay(composition, formula, values[output])
                if exact is not None:
                    values[output], source_refs = exact
                    for end, held_refs in source_refs.items():
                        refs[output][end].extend(held_refs)
            del pending[output]
            progressed = True
        if not progressed:
            raise ForecastModelUnavailable("statement formula graph cannot be replayed")
    return values, refs


def structure_historical_values(
    drivers: Sequence[Mapping[str, Any]], structure: Mapping[str, Any],
) -> tuple[dict[str, dict[str, Decimal]], dict[str, dict[str, list[dict[str, Any]]]]]:
    """Public pure replay used by model projections and the forecast builder."""

    return _structure_historical_values(drivers, structure)


def default_structure_assumptions(
    drivers: Sequence[Mapping[str, Any]], periods: Sequence[Mapping[str, Any]],
    structure: Mapping[str, Any], *, decided_by: str = AUTOMATION_ACTOR,
) -> list[dict[str, Any]]:
    """Generate assumptions only for methods named by the validated structure."""

    if _is_coverage(structure):
        from .forecast_coverage_assumptions import coverage_structure_assumptions
        return coverage_structure_assumptions(drivers, periods, structure, decided_by=decided_by)
    historical, historical_refs = _structure_historical_values(drivers, structure)
    by_line = {str(item.get("structure_line_ref")): item for item in drivers}
    assumptions: list[dict[str, Any]] = []
    for line in structure.get("lines") or []:
        if line.get("kind") != "filed":
            continue
        line_ref = str(line["ref"])
        driver = by_line.get(line_ref)
        if driver is None:
            continue
        method = str(line.get("forecast_method"))
        rate: dict[str, Any] | None = None
        measure = method
        if method == "quarterly_growth":
            rate = positive_trailing_growth(quarterly_history(driver))
        elif method == "share_of_line":
            base_ref = str(line.get("forecast_base_ref"))
            samples: list[tuple[Decimal, str]] = []
            for end, value in historical.get(line_ref, {}).items():
                base = historical.get(base_ref, {}).get(end)
                if base is not None and base != 0:
                    samples.append(((value / base).quantize(_RATE_QUANT, ROUND_HALF_UP), end))
            if samples:
                used = samples[-TRAILING_QUARTERS:]
                signs = {historical[base_ref][end] > 0 for _value, end in used}
                if len(signs) > 1:
                    continue
                mean = sum((value for value, _end in used), Decimal(0)) / Decimal(len(used))
                evidence: list[dict[str, Any]] = []
                seen: set[tuple[Any, ...]] = set()
                for _value, end in used:
                    for source in (historical_refs.get(line_ref, {}).get(end, [])
                                   + historical_refs.get(base_ref, {}).get(end, [])):
                        key = tuple(source.get(name) for name in
                                    ("kind", "concept", "period_end", "accession"))
                        if key not in seen:
                            seen.add(key)
                            evidence.append(dict(source))
                rate = {"value": mean, "refs": evidence,
                        "last_period": used[-1][1]}
        elif method == "unavailable":
            continue
        else:
            raise ForecastModelUnavailable(
                f"filed statement line {line_ref} has invalid forecast method {method}")
        if rate is None:
            continue
        for period in periods:
            assumptions.append(_assumption(
                driver_ref=str(driver["ref"]), period=period, measure=measure,
                value=_decimal(rate["value"], "assumption value"), unit="ratio",
                kind="estimate",
                because=(f"the trailing filed history through {rate.get('last_period')} "
                         f"for structure line {line_ref} is carried forward unchanged"),
                refs=[dict(item) for item in rate.get("refs") or []],
                decided_by=decided_by,
            ))
    return assumptions


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


def forecast_policy(anchor: Mapping[str, Any], sources: Mapping[str, Any] | None = None) -> dict[str, Any]:
    sources = sources or {}
    cells = quarterly_history(anchor)
    if not cells:
        raise ForecastModelUnavailable("the revenue driver has no filed quarter to start from")
    last_end = str(cells[-1]["period_end"])
    preliminary = sources.get("preliminary") or {}
    latest = max(last_end, str(sources.get("latest_actual_end") or last_end),
                 str(preliminary.get("period_end") or last_end))
    return {"rule_ref": "rule:rolling-forecast-horizon:1", "latest_actual_end": latest,
            "fiscal_year_end": sources.get("fiscal_year_end"),
            "preliminary": dict(preliminary) if preliminary else None}


def forecast_periods(anchor: Mapping[str, Any], count: int, *,
                     policy: Mapping[str, Any] | None = None) -> list[dict[str, str]]:
    """Consecutive unreported quarters, at least eight and through FY+1."""
    if isinstance(count, bool) or not isinstance(count, int) or count < MIN_FORECAST_PERIODS:
        raise ForecastModelValidationError("forecast_quarters must be at least 8")
    count = min(count, MAX_FORECAST_PERIODS)
    policy = policy or forecast_policy(anchor)
    latest = _iso_date(policy["latest_actual_end"], "latest_actual_end")
    period = {"start": latest, "end": latest, "calendar": "company:fiscal", "kind": "quarter"}
    target = None
    if policy.get("fiscal_year_end"):
        fiscal = date.fromisoformat(str(policy["fiscal_year_end"]))
        current = date.fromisoformat(latest)
        fy = date(current.year, fiscal.month, min(fiscal.day, 28) if fiscal.month == 2 else fiscal.day)
        if fy < current:
            fy = fy.replace(year=fy.year + 1)
        target = fy.replace(year=fy.year + 1).isoformat()
    out = []
    while len(out) < count or (target is not None and out[-1]["end"] < target):
        if len(out) >= MAX_FORECAST_PERIODS:
            raise ForecastModelUnavailable("fiscal calendar requires more than twelve forecast quarters")
        period = quarter_after(period)
        out.append(dict(period))
    return out


def validate_new_forecast_rules(record: Mapping[str, Any]) -> None:
    """Write boundary only: historical published records remain readable."""
    periods = record.get("forecast_periods") or []
    if len(periods) < MIN_FORECAST_PERIODS:
        raise ForecastModelValidationError("new models must forecast at least 8 quarters")
    anchor = revenue_anchor(record.get("drivers") or [])
    policy = record.get("forecast_policy") or forecast_policy(anchor)
    known_end = max([str(c["period_end"]) for d in record.get("drivers") or []
                    for c in quarterly_history(d)] + list(record.get("history_periods") or []))
    known_end = max([known_end] + [str(p["end"]) for p in record.get("realised_periods") or []])
    preliminary = policy.get("preliminary") or {}
    known_end = max(known_end, str(preliminary.get("period_end") or known_end))
    if policy["latest_actual_end"] != known_end:
        raise ForecastModelValidationError("forecast policy must equal the latest known reported history/preliminary actual")
    expected = forecast_periods(anchor, len(periods), policy=policy)
    if periods != expected:
        raise ForecastModelValidationError("forecast periods must follow the latest reported actual and cover FY+1")
    from .forecast_seasonality import seasonal_profile, RULE_SEASONAL_FACTOR, RULE_YEAR_OVER_YEAR
    for driver in record.get("drivers") or []:
        profile = seasonal_profile(quarterly_history(driver))
        if not profile or not profile["significant"]:
            continue
        if driver.get("forecast_method") == "quarterly_growth":
            raise ForecastModelValidationError(f"seasonal line {driver['ref']} cannot use average quarterly growth")
        if driver.get("role") == REVENUE and not is_structured_schema(record.get("schema_version")):
            for item in record.get("assumptions") or []:
                if item.get("driver_ref") == driver["ref"] and item.get("kind") == "estimate" and (
                    (item.get("provenance") or {}).get("rule_ref") not in {RULE_SEASONAL_FACTOR, RULE_YEAR_OVER_YEAR}):
                    raise ForecastModelValidationError("seasonal revenue requires a seasonal or year-over-year method")


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


def positive_trailing_growth(
    cells: Sequence[Mapping[str, Any]],
) -> dict[str, Any] | None:
    """Structured multiplicative growth, only over a positive trailing window."""

    # Keep only the latest contiguous suffix.  A gap closes the historical
    # window; it must not cause an older pair to be pulled in as a substitute.
    window: list[Mapping[str, Any]] = []
    for prior, current in zip(cells, cells[1:]):
        gap = _days(str(prior["period_end"]), str(current["period_end"]))
        if QUARTER_GAP_MIN_DAYS <= gap <= QUARTER_GAP_MAX_DAYS:
            if not window:
                window.append(prior)
            window.append(current)
        else:
            window = []
    window = window[-(TRAILING_QUARTERS + 1):]
    if len(window) < 2 or any(
        _decimal(cell["value"], "history value") <= 0 for cell in window
    ):
        return None
    return trailing_growth(window)


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


def market_proxy_ref(
    ref: str, *, proxy_gap: str, concept: str | None = None,
    period_end: str | None = None,
) -> dict[str, Any]:
    """One citation of a public quantity, with its distance from the company.

    A constructor rather than a dict literal at each call site, because the
    field that makes this kind admissible is the one a hurried caller leaves
    out, and a keyword-only parameter cannot be left out.
    """

    return {
        "kind": MARKET_PROXY, "ref": ref, "concept": concept,
        "period_end": period_end, "accession": None, "proxy_gap": proxy_gap,
    }


def proxy_gaps(assumption: Mapping[str, Any]) -> list[str]:
    """Every market-proxy distance one assumption stands on, in ref order."""

    return [str(item.get("proxy_gap") or "")
            for item in (assumption.get("refs") or ())
            if isinstance(item, Mapping) and item.get("kind") == MARKET_PROXY]


def _assumption(
    *, driver_ref: str, period: Mapping[str, str], measure: str, value: Decimal,
    unit: str, because: str, refs: Sequence[Mapping[str, Any]], decided_by: str,
    kind: str = "estimate", rule_ref: str | None = GENERATOR_REF,
    work_order_ref: str | None = None,
    outside_band: Mapping[str, Any] | None = None,
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
        # P17b: an assumption outside the range this company has actually filed
        # is allowed and is not allowed to be silent. ``None`` says the value
        # was not claimed to be outside anything; a mapping with a ``reason``
        # says it is, and why. The economic-invariant gate refuses a value
        # outside the filed band that carries neither.
        "outside_band": None if outside_band is None else dict(outside_band),
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
                from .forecast_seasonality import seasonal_profile, year_ago, RULE_YEAR_OVER_YEAR
                profile = seasonal_profile(cells)
                if profile and profile["significant"]:
                    values = {str(c["period_end"]): _decimal(c["value"], "history value") for c in cells}
                    prev = values[str(cells[-1]["period_end"])]
                    for period in periods:
                        prior = year_ago(str(period["end"]), values)
                        if prior is None or prev == 0:
                            continue
                        level = values[prior] * (1 + profile["year_over_year_growth"])
                        out.append(_assumption(driver_ref=str(driver["ref"]), period=period,
                            measure="quarterly_growth", value=level / prev - 1, unit="ratio",
                            because=f"same fiscal quarter {prior} × year-over-year growth; seasonal distribution retained",
                            refs=[_cell_ref(c) for c in profile["rows"]], decided_by=decided_by,
                            rule_ref=RULE_YEAR_OVER_YEAR))
                        values[str(period["end"])] = level
                        prev = level
                    continue
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
        preliminary = [a for a in assumptions if a.get("driver_ref") == anchor["ref"]
                       and a.get("measure") == "reported_actual" and not a.get("superseded_by")
                       and a["period"]["end"] < str(periods[0]["end"])] if periods else []
        if preliminary:
            actual = max(preliminary, key=lambda a: a["period"]["end"])
            start = {"value": actual["value"], "period_end": actual["period"]["end"], "input_cell": None,
                     "actual_assumption": actual}
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
            running = (rate if assumption.get("measure") == "mechanism_amount" else running * (Decimal(1) + rate)).quantize(_VALUE_QUANT, ROUND_HALF_UP)
            revenue_values[str(period["end"])] = running
            if index:
                inputs, prior_end = [], str(periods[index - 1]["end"])
            elif base_cell is not None:
                inputs, prior_end = [_cell_ref(base_cell)], None
            elif start.get("actual_assumption"):
                inputs, prior_end = list(start["actual_assumption"]["refs"]), None
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


def _structure_result_refs(structure: Mapping[str, Any]) -> dict[str, str]:
    """Stable result refs: familiar role refs when unique, line refs otherwise."""

    counts: dict[str, int] = {}
    for line in structure.get("lines") or []:
        role = str(line.get("role"))
        counts[role] = counts.get(role, 0) + 1
    refs: dict[str, str] = {}
    for line in structure.get("lines") or []:
        line_ref, role = str(line["ref"]), str(line["role"])
        refs[line_ref] = (
            f"result:{role}" if counts[role] == 1
            else f"result:{role}:{line_ref}"
        )
    return refs


def structure_result_refs(structure: Mapping[str, Any]) -> dict[str, str]:
    """Public deterministic line-to-result identity for annual projections."""

    return _structure_result_refs(structure)


def compute_structure_results(
    drivers: Sequence[Mapping[str, Any]],
    assumptions: Sequence[Mapping[str, Any]],
    periods: Sequence[Mapping[str, Any]],
    structure: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Execute the validated company formula DAG over forecast leaves.

    Every dependency is explicit in the structure.  A missing assumption,
    missing result or zero denominator produces an unavailable cell; it is
    never interpreted as a zero-valued term.
    """

    lines = {str(item["ref"]): dict(item) for item in (structure.get("lines") or [])}
    formulas = {str(item["output_ref"]): dict(item)
                for item in (structure.get("formulas") or [])}
    result_refs = _structure_result_refs(structure)
    drivers_by_line = {str(item.get("structure_line_ref")): item for item in drivers}
    assumptions_by_driver_period = {
        (str(item["driver_ref"]), str(item["period"]["end"])): item
        for item in assumptions if not item.get("superseded_by")
    }
    results: dict[str, dict[str, Any]] = {}
    values: dict[str, dict[str, Decimal]] = {}
    pending = dict(lines)
    preliminary_ends = [str(a["period"]["end"]) for a in assumptions
                        if a.get("measure") == "reported_actual"
                        and (a.get("provenance") or {}).get("rule_ref") == "rule:preliminary-actual-8k:1"
                        and not a.get("superseded_by")]
    preliminary_rolling = bool(periods and preliminary_ends and
        quarter_after({"end":max(preliminary_ends),"calendar":"company:fiscal"})["end"] == periods[0]["end"])
    prior_growth_values: dict[str, Decimal] = {}
    prior_growth_refs: dict[str, dict[str, Any]] = {}
    for line_ref, driver in drivers_by_line.items():
        history = quarterly_history(driver)
        if history:
            prior_growth_values[line_ref] = _decimal(history[-1]["value"], "history value")
            prior_growth_refs[line_ref] = _cell_ref(history[-1])

    for actual in assumptions:
        if actual.get("measure") != "reported_actual" or actual.get("superseded_by"):
            continue
        driver = next((d for d in drivers if d["ref"] == actual["driver_ref"]), None)
        if driver is not None:
            line_ref = str(driver.get("structure_line_ref"))
            prior_growth_values[line_ref] = _decimal(actual["value"], "preliminary actual")
            prior_growth_refs[line_ref] = {"kind": "assumption", "ref": actual["ref"],
                                          "period_end": actual["period"]["end"]}

    # The last filed value of every leaf, for methods that hold a level and
    # for a rate whose balance is the cash rolled after the cash flow.
    last_filed: dict[str, tuple[Decimal, dict[str, Any]]] = {}
    for line_ref, driver in drivers_by_line.items():
        history = (driver.get("history") or []) if driver.get("statement") == "balance" \
            else quarterly_history(driver)
        if history:
            last_filed[line_ref] = (_decimal(history[-1]["value"], "history value"),
                                    _cell_ref(history[-1]))

    while pending:
        progressed = False
        for line_ref, line in list(pending.items()):
            method = str(line.get("forecast_method"))
            formula = formulas.get(line_ref)
            dependencies: list[str] = []
            if method == "share_of_line":
                dependencies = [str(line.get("forecast_base_ref"))]
            elif method == "rate_times_balance":
                base_line = lines.get(str(line.get("forecast_base_ref"))) or {}
                if base_line.get("forecast_method") != "roll_forward":
                    dependencies = [str(line.get("forecast_base_ref"))]
            elif method == "formula":
                if formula is None:
                    raise ForecastModelUnavailable(
                        f"derived statement line {line_ref} has no formula")
                dependencies = (
                    [str(item["line_ref"]) for item in formula["terms"]]
                    if formula.get("operator") == "sum"
                    else [str(formula["numerator_ref"]),
                          str(formula["denominator_ref"])]
                )
            if any(ref not in results for ref in dependencies):
                continue
            ref = result_refs[line_ref]
            driver = drivers_by_line.get(line_ref)
            driver_ref = None if driver is None else str(driver["ref"])
            if method == "quarterly_growth":
                formula_text = f"{line_ref}[k] = {line_ref}[k-1] * (1 + growth[k])"
            elif method == "year_over_year_growth":
                formula_text = f"{line_ref}[k] = {line_ref}[k-4] * (1 + year_over_year_growth[k])"
            elif method == "seasonal_factor":
                formula_text = f"{line_ref}[k] = trend level[k] * fiscal-quarter seasonal factor[k]"
            elif method == "share_of_line":
                formula_text = f"{line_ref}[k] = {line['forecast_base_ref']}[k] * share[k]"
            elif method == "rate_times_balance":
                formula_text = (f"{line_ref}[k] = {line['forecast_base_ref']}[k] * rate[k]"
                                if dependencies else
                                f"{line_ref}[k] = {line['forecast_base_ref']}[last filed] * rate[k]")
            elif method == "carry_forward_mean":
                formula_text = f"{line_ref}[k] = trailing four-quarter mean (carried)"
            elif method == "zero_one_off":
                formula_text = f"{line_ref}[k] = 0 (one-off)"
            elif method == "hold_last":
                formula_text = f"{line_ref}[k] = {line_ref}[last filed] (held)"
            elif method == "roll_forward":
                formula_text = (f"{line_ref}[k] = {line_ref}[k-1] + free cash flow[k] "
                                "- buybacks[k] - dividends[k] - acquisitions[k]")
            elif method == "formula" and formula is not None:
                formula_text = canonical_json({
                    key: formula[key] for key in formula
                    if key in {"operator", "terms", "numerator_ref", "denominator_ref"}
                })
            else:
                formula_text = f"{line_ref}[k] unavailable by company structure"
            result = _result(
                ref, str(line.get("role")), str(line.get("label")),
                str(line.get("unit")), formula_text, driver_ref=driver_ref)
            values[line_ref] = {}
            for period in periods:
                end = str(period["end"])
                explicit = assumptions_by_driver_period.get((driver_ref, end))
                if explicit is not None and explicit.get("measure") == "mechanism_amount":
                    value = _decimal(explicit["value"], "mechanism amount")
                    values[line_ref][end] = value
                    result["formula"] = str(explicit["because"])
                    result["cells"].append(_computed_cell(ref, period, value, assumptions=[explicit["ref"]]))
                    continue
                if method == "unavailable":
                    result["cells"].append(_unavailable_cell(
                        ref, period,
                        f"statement line {line_ref} is explicitly unavailable for forecast"))
                    continue
                if method == "roll_forward":
                    # Rolled by ``roll_cash_balance`` once the cash flow is
                    # known; until then the cell says so.
                    result["cells"].append(_unavailable_cell(
                        ref, period, "the cash balance is rolled from the cash flow"))
                    continue
                if method in {"year_over_year_growth", "seasonal_factor"}:
                    from .forecast_seasonality import year_ago
                    assumption = assumptions_by_driver_period.get((driver_ref, end))
                    history = {str(c["period_end"]): c for c in quarterly_history(driver or {})}
                    available = {**{key: _decimal(c["value"], "history value") for key, c in history.items()}, **values[line_ref]}
                    prior = year_ago(end, available)
                    if assumption is None or (method == "year_over_year_growth" and prior is None):
                        result["cells"].append(_unavailable_cell(ref, period, "same-season history or assumption unavailable"))
                        continue
                    amount = _decimal(assumption["value"], "assumption value")
                    if method == "year_over_year_growth":
                        amount = available[prior] * (1 + amount)
                    values[line_ref][end] = amount
                    result["cells"].append(_computed_cell(ref, period, amount,
                        assumptions=[str(assumption["ref"])],
                        inputs=[_cell_ref(history[prior])] if prior in history else [],
                        results=[{"ref": ref, "period_end": prior}] if prior and prior not in history else []))
                    continue
                if method in {"carry_forward_mean", "zero_one_off", "hold_last",
                              "rate_times_balance"}:
                    assumption = (None if driver is None else
                                  assumptions_by_driver_period.get((driver_ref, end)))
                    if assumption is None:
                        result["cells"].append(_unavailable_cell(
                            ref, period,
                            f"statement line {line_ref} has no forecast assumption for this quarter"))
                        continue
                    amount = _decimal(assumption["value"], "assumption value")
                    inputs_used: list[dict[str, Any]] = []
                    results_used: list[dict[str, str]] = []
                    if method == "rate_times_balance":
                        base_ref = str(line.get("forecast_base_ref"))
                        if dependencies:
                            base = values[base_ref].get(end)
                            results_used = [{"ref": result_refs[base_ref], "period_end": end}]
                        else:
                            held = last_filed.get(base_ref)
                            base = None if held is None else held[0]
                            inputs_used = [] if held is None else [held[1]]
                        if base is None:
                            result["cells"].append(_unavailable_cell(
                                ref, period,
                                f"forecast base {base_ref} is unavailable for this quarter"))
                            continue
                        amount = base * amount
                    values[line_ref][end] = amount
                    result["cells"].append(_computed_cell(
                        ref, period, amount, assumptions=[str(assumption["ref"])],
                        inputs=inputs_used, results=results_used))
                    continue
                if method in {"quarterly_growth", "share_of_line"}:
                    if driver is None:
                        result["cells"].append(_unavailable_cell(
                            ref, period, f"statement line {line_ref} has no filed driver"))
                        continue
                    assumption = assumptions_by_driver_period.get((driver_ref, end))
                    if assumption is None:
                        if method == "quarterly_growth":
                            # A later growth rate cannot jump over a missing
                            # quarter and pretend the stale value was k-1.
                            prior_growth_values.pop(line_ref, None)
                            prior_growth_refs.pop(line_ref, None)
                        result["cells"].append(_unavailable_cell(
                            ref, period,
                            f"statement line {line_ref} has no forecast assumption for this quarter"))
                        continue
                    rate = _decimal(assumption["value"], "assumption value")
                    if method == "quarterly_growth":
                        base = prior_growth_values.get(line_ref)
                        prior_result = prior_growth_refs.get(line_ref)
                        prior_end = (None if prior_result is None
                                     else prior_result.get("period_end"))
                        steps = 1
                        # An 8-K can advance the company reporting boundary
                        # without reporting every leaf. Advance an initial
                        # filed leaf through the intervening quarters rather
                        # than relabel one growth step as a later quarter.
                        if (base is not None and isinstance(prior_end, str)
                                and prior_result.get("kind") in {"input_cell", "assumption"}
                                and end == periods[0]["end"] and preliminary_rolling):
                            cursor = {"end": prior_end, "start": prior_end,
                                      "calendar": "company:fiscal", "kind": "quarter"}
                            steps = 0
                            while cursor["end"] < end and steps < 12:
                                cursor = quarter_after(cursor)
                                steps += 1
                            if cursor["end"] != end:
                                steps = 0
                        if (base is None or not isinstance(prior_end, str)
                                or not steps or (steps == 1 and not
                                QUARTER_GAP_MIN_DAYS <= _days(prior_end, end) <=
                                QUARTER_GAP_MAX_DAYS)):
                            prior_growth_values.pop(line_ref, None)
                            prior_growth_refs.pop(line_ref, None)
                            result["cells"].append(_unavailable_cell(
                                ref, period,
                                f"statement line {line_ref} has no adjacent prior quarter value"))
                            continue
                        value = base * (Decimal(1) + rate) ** steps
                        if steps > 1:
                            result["formula"] = f"{line_ref}[k] = prior known level * (1 + growth[k]) ^ elapsed fiscal quarters"
                        result_refs_for_cell = []
                        inputs = []
                        if prior_result and prior_result.get("kind") == "input_cell":
                            inputs = [prior_result]
                        elif prior_result and prior_result.get("kind") == "assumption":
                            # The level is a separately source-bound 8-K actual.
                            inputs = [dict(item) for actual in assumptions if actual["ref"] == prior_result["ref"] for item in actual["refs"]]
                        elif prior_result:
                            result_refs_for_cell = [prior_result]
                        prior_growth_values[line_ref] = value
                        prior_growth_refs[line_ref] = {"ref": ref, "period_end": end}
                    else:
                        base_ref = dependencies[0]
                        base = values[base_ref].get(end)
                        if base is None:
                            result["cells"].append(_unavailable_cell(
                                ref, period,
                                f"forecast base {base_ref} is unavailable for this quarter"))
                            continue
                        value = base * rate
                        inputs = []
                        result_refs_for_cell = [{"ref": result_refs[base_ref],
                                                 "period_end": end}]
                    if (line.get("role") == "diluted_weighted_average_shares"
                            and value <= 0):
                        result["cells"].append(_unavailable_cell(
                            ref, period,
                            "forecast diluted weighted-average shares are not positive"))
                        if method == "quarterly_growth":
                            prior_growth_values.pop(line_ref, None)
                            prior_growth_refs.pop(line_ref, None)
                        continue
                    values[line_ref][end] = value
                    result["cells"].append(_computed_cell(
                        ref, period, value,
                        assumptions=[str(assumption["ref"])], inputs=inputs,
                        results=result_refs_for_cell))
                    continue
                assert formula is not None
                missing = [dependency for dependency in dependencies
                           if end not in values[dependency]]
                if missing:
                    result["cells"].append(_unavailable_cell(
                        ref, period, "formula terms unavailable for this quarter: "
                        + ", ".join(missing)))
                    continue
                if formula["operator"] == "sum":
                    value = sum(
                        (_decimal(term["coefficient"], "coefficient")
                         * values[str(term["line_ref"])][end]
                         for term in formula["terms"]), Decimal(0))
                else:
                    denominator = values[str(formula["denominator_ref"])][end]
                    if denominator <= 0:
                        result["cells"].append(_unavailable_cell(
                            ref, period,
                            "formula denominator is not positive for this quarter"))
                        continue
                    value = (values[str(formula["numerator_ref"])][end]
                             / denominator)
                values[line_ref][end] = value
                result["cells"].append(_computed_cell(
                    ref, period, value,
                    results=[{"ref": result_refs[dependency], "period_end": end}
                             for dependency in dependencies]))
            results[line_ref] = _finish(result)
            del pending[line_ref]
            progressed = True
        if not progressed:
            raise ForecastModelUnavailable("statement forecast dependency graph contains a cycle")
    return [results[str(line["ref"])] for line in (structure.get("lines") or [])]


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


def preliminary_level_assumptions(drivers: Sequence[Mapping[str, Any]], sources: Mapping[str, Any] | None,
                                  actor_ref: str) -> list[dict[str, Any]]:
    preliminary = (sources or {}).get("preliminary") or {}
    if not preliminary.get("accession") or not preliminary.get("period_end"):
        return []
    out = []
    for driver in drivers:
        metric = "revenue" if driver.get("role") == REVENUE else "shares_diluted" if driver.get("role") == "diluted_weighted_average_shares" else None
        value = (preliminary.get("values") or {}).get(metric)
        history = quarterly_history(driver)
        if value is None or not history:
            continue
        period = quarter_after({"end": history[-1]["period_end"], "calendar": "company:fiscal"})
        if period["end"] != preliminary["period_end"]:
            continue
        out.append(_assumption(driver_ref=driver["ref"], period=period, measure="reported_actual",
            value=_decimal(value, "preliminary actual"), unit=str(driver.get("unit") or "usd"),
            kind="actual", because="8-K preliminary actual anchors the forecast",
            refs=[{"kind": "filing", "ref": preliminary["accession"], "concept": None,
                   "period_end": preliminary["period_end"], "accession": preliminary["accession"]}],
            decided_by=actor_ref, rule_ref="rule:preliminary-actual-8k:1"))
    return out


def _new_forecast_model(body: dict[str, Any], *, spec: Mapping[str, Any] | None = None,
                        table: Mapping[str, Any] | None = None,
                        assumption_sources: Mapping[str, Any] | None = None) -> dict[str, Any]:
    if spec is None and body.get("execution_contract"):
        held = body["execution_contract"]
        spec = {**held["specification"], "fcf_definition": held.get("fcf_definition")}
    if spec is not None:
        from .forecast_financial_execution import execute_financial_model
        effective = dict(spec)
        if not effective.get("fcf_definition") and (assumption_sources or {}).get("fcf_definition"):
            effective["fcf_definition"] = assumption_sources["fcf_definition"]
        if table is not None and is_structured_schema(body.get("schema_version")):
            from .forecast_financial_execution import additional_drivers
            held_refs = {d["ref"] for d in body["drivers"]}
            body["drivers"].extend(d for d in additional_drivers(effective, table, body["drivers"]) if d["ref"] not in held_refs)
        if (assumption_sources or {}).get("source_accuracy") is not None:
            effective["source_accuracy"] = assumption_sources["source_accuracy"]
        execute_financial_model(body, effective)
    validate_new_forecast_rules(body)
    return body


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
    assumption_sources: Mapping[str, Any] | None = None,
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
        wanted = MIN_FORECAST_PERIODS
    drivers = build_drivers(table)
    anchor = revenue_anchor(drivers)
    policy = forecast_policy(anchor, assumption_sources)
    periods = forecast_periods(anchor, max(wanted, MIN_FORECAST_PERIODS), policy=policy)
    if assumptions is None:
        assumptions = [*default_assumptions(drivers, periods, decided_by=actor_ref),
                       *preliminary_level_assumptions(drivers, assumption_sources, actor_ref)]
    statements = statement_importances(spec)
    results = compute_results(drivers, assumptions, periods, statements=statements)
    unit = str(anchor.get("unit") or "usd")
    if evidence_refs is None:
        evidence_refs = filing_refs(drivers, table.get("periods"))
    return _new_forecast_model({
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
        "forecast_policy": policy,
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
    }, spec=spec, table=table, assumption_sources=assumption_sources)


def _is_coverage(structure: Mapping[str, Any]) -> bool:
    from .company_financial_statement_structure import is_coverage_structure

    return is_coverage_structure(structure.get("schema_version"))


def structure_formula_hash(
    structure: Mapping[str, Any], binding: Mapping[str, Any],
) -> str:
    return content_hash({
        "executor_contract": (STRUCTURE_COVERAGE_EXECUTOR_CONTRACT
                              if _is_coverage(structure) else STRUCTURE_EXECUTOR_CONTRACT),
        "structure_hash": structure.get("content_hash"),
        "binding_hash": binding.get("content_hash"),
    })


def build_cash_flow_companion(
    spec: Mapping[str, Any], table: Mapping[str, Any], structure: Mapping[str, Any],
) -> dict[str, Any]:
    """Bind the existing cash-flow foundation beside, not inside, the income DAG."""

    definition = spec.get("cash_flow_companion")
    if spec.get("schema_version") != "0.4" or not isinstance(definition, Mapping):
        raise ForecastModelUnavailable(
            "cash-flow companion needs a versioned company specification")
    if definition.get("schema_version") not in {"0.1", "0.2"}:
        raise ForecastModelUnavailable("cash-flow companion specification is unsupported")
    definition_lines = definition.get("lines")
    if not isinstance(definition_lines, list) or len(definition_lines) != 2:
        raise ForecastModelUnavailable("cash-flow companion specification is incomplete")
    by_definition = {
        str(item.get("role")): item for item in definition_lines
        if isinstance(item, Mapping)
    }
    if set(by_definition) != {OPERATING_CASH_FLOW, CAPITAL_EXPENDITURE}:
        raise ForecastModelUnavailable("cash-flow companion specification roles differ")
    result_refs = _structure_result_refs(structure)
    structure_lines = {
        str(item.get("ref")): item for item in (structure.get("lines") or [])
        if isinstance(item, Mapping) and item.get("ref")
    }
    revenue_lines = [item for item in structure_lines.values()
                     if item.get("role") == REVENUE]
    if len(revenue_lines) != 1:
        raise ForecastModelUnavailable(
            "cash-flow companion needs one exact structured reporting currency")
    reporting_unit = str(revenue_lines[0].get("unit") or "").casefold()
    raw_inputs = list(table.get("cash_flow_inputs") or [])
    by_role: dict[str, Mapping[str, Any]] = {}
    for item in raw_inputs:
        if not isinstance(item, Mapping):
            raise ForecastModelUnavailable("cash-flow input is not an object")
        role = str(item.get("role") or "")
        if role not in {OPERATING_CASH_FLOW, CAPITAL_EXPENDITURE} or role in by_role:
            raise ForecastModelUnavailable("cash-flow inputs have invalid or duplicate roles")
        by_role[role] = item
    importance = statement_importances(spec).get("cash", "not_material")
    lines: list[dict[str, Any]] = []
    for role in (OPERATING_CASH_FLOW, CAPITAL_EXPENDITURE):
        declared = by_definition[role]
        source = by_role.get(role)
        declared_concept = declared.get("concept")
        method = str(declared.get("forecast_method") or "")
        base_ref = declared.get("forecast_base_ref")
        source_bound = (
            source is not None and source.get("status") in {FILED, INCOMPLETE}
            and str(source.get("concept")) == str(declared_concept)
        )
        if source_bound:
            if not cash_quarter_windows_are_unique(
                    list((source.get("series") or {}).get("quarters") or [])):
                raise ForecastModelUnavailable(
                    f"cash-flow companion {role} has duplicate or overlapping quarters")
            source_unit = str(source.get("unit") or "").casefold()
            if source_unit != reporting_unit:
                raise ForecastModelUnavailable(
                    f"cash-flow companion {role} source and reporting units differ")
            if method == "share_of_line":
                base_unit = str(
                    (structure_lines.get(str(base_ref)) or {}).get("unit") or ""
                ).casefold()
                if source_unit != base_unit:
                    raise ForecastModelUnavailable(
                        f"cash-flow companion {role} source and base units differ")
        forecastable = (
            source_bound and source.get("status") == FILED
            and method == "share_of_line" and base_ref in result_refs
        )
        source_status = (
            "filed" if source_bound and source.get("status") == FILED else
            "incomplete" if source_bound else "unavailable"
        )
        lines.append({
            "ref": f"cash-flow:{role.replace('_', '-')}",
            "result_ref": f"result:{role}",
            "role": role,
            "status": source_status,
            "concept": str(declared_concept) if declared_concept is not None else None,
            "unit": str(source["unit"]).casefold() if source_bound else None,
            "source": dict(source) if source_bound else None,
            "source_hash": content_hash(source) if source_bound else None,
            "reason": (
                None if forecastable else
                "the specification marks the cash flow statement not_material"
                if importance == "not_material" else
                str(declared.get("because") or (source or {}).get("reason") or
                    f"no supported exact {role} forecast basis is available")
            ),
            "forecast_method": "share_of_line" if forecastable else "unavailable",
            "forecast_base_ref": str(base_ref) if forecastable else None,
            "forecast_base_result_ref": (
                result_refs.get(str(base_ref)) if forecastable else None),
        })
    body = {
        "schema_version": "cash-flow-companion-0.1",
        "authority_ref": CASH_FLOW_COMPANION_REF,
        "company_ref": str(table.get("company_ref") or ""),
        "spec_ref": str(table.get("spec_ref") or spec.get("spec_id") or ""),
        "spec_hash": str(spec.get("content_hash") or ""),
        "financial_input_hash": content_hash({"cash_flow_inputs": raw_inputs}),
        "income_structure_hash": str(structure.get("content_hash") or ""),
        "cash_statement_importance": importance,
        "reporting_unit": reporting_unit,
        "specification": json.loads(canonical_json(definition)),
        "specification_hash": content_hash(definition),
        "lines": lines,
        "formula": {
            "output_ref": "result:free_cash_flow",
            "operator": "sum",
            "terms": [
                {"result_ref": "result:operating_cash_flow", "coefficient": "1"},
                {"result_ref": "result:capital_expenditure", "coefficient": "-1"},
            ],
        },
    }
    wire = validate_cash_flow_companion({**body, "content_hash": content_hash(body)})
    derived_lines = [item for item in wire["lines"]
                     if item["forecast_method"] == "share_of_line"
                     and (structure_lines.get(str(item["forecast_base_ref"])) or {}).get("kind") == "derived"]
    if derived_lines and definition.get("schema_version") == "0.2":
        from .cash_flow_derived_base import (
            derived_base_is_forecastable, derived_trailing_share,
        )
        income_drivers = build_structure_drivers(table, structure)
        historical, historical_refs = _structure_historical_values(income_drivers, structure)
        cash_drivers = {str(item["role"]): item
                        for item in build_cash_flow_companion_drivers(table, wire)}
        for line in derived_lines:
            base_ref = str(line["forecast_base_ref"])
            if not derived_base_is_forecastable(structure, base_ref):
                raise ForecastModelUnavailable(
                    f"cash-flow companion {line['role']} derived income base is not executable")
            from .cash_flow_ratio_applicability import refuse_if_inapplicable
            if any(str(c.get('unit') or '').casefold()!=line['unit']
                   for c in line['source']['series']['quarters']):
                raise ForecastModelUnavailable('cash ratio source quarter units differ')
            refuse_if_inapplicable(quarterly_history(cash_drivers[str(line["role"])]),
                historical.get(base_ref, {}), historical_refs.get(base_ref, {}), income_drivers,
                role=line['role'], base_ref=base_ref, input_hash=content_hash(table), unit=line['unit'])
            if derived_trailing_share(
                    quarterly_history(cash_drivers[str(line["role"])]),
                    historical.get(base_ref, {}), historical_refs.get(base_ref, {}),
                    income_drivers, quarters=TRAILING_QUARTERS, rate_quant=_RATE_QUANT) is None:
                targets = sorted(str(cell["period_end"]) for cell in
                                 quarterly_history(cash_drivers[str(line["role"])]))[-TRAILING_QUARTERS:]
                from .cash_flow_filed_tie_base import derive as filed_tie_basis
                tie = next((f.get("tie_out_concept") for f in structure.get("formulas") or []
                            if f.get("output_ref") == base_ref), None)
                sources = [f for f in table.get("filed_lines") or [] if f.get("concept") == tie]
                if len(sources) != 1:
                    raise ForecastModelUnavailable(
                        f"cash-flow companion {line['role']} derived income base lacks "
                        "a complete same-duration source-bound trailing window; "
                        f"target_periods={targets}; no earlier window substituted")
                try:
                    line["historical_base"] = filed_tie_basis(
                        structure, base_ref, line["source"], sources[0], income_drivers)
                except ForecastModelUnavailable as exc:
                    from .cash_flow_ratio_applicability import CashRatioUnavailable
                    if isinstance(exc, CashRatioUnavailable):raise
                    raise ForecastModelUnavailable(
                        f"cash-flow companion {line['role']} derived income base lacks a complete same-duration "
                        f"trailing window; independent filed tie unavailable: {exc}") from exc
        if any(line.get("historical_base") is not None for line in wire["lines"]):
            for line in wire["lines"]:
                line.setdefault("historical_base", None)
            wire["schema_version"] = "cash-flow-companion-0.2"
            wire["content_hash"] = content_hash({k:v for k,v in wire.items() if k != "content_hash"})
            wire = validate_cash_flow_companion(wire)
    return wire


def validate_cash_flow_companion(value: Mapping[str, Any]) -> dict[str, Any]:
    fields = {
        "schema_version", "authority_ref", "company_ref", "spec_ref", "spec_hash",
        "financial_input_hash", "income_structure_hash", "cash_statement_importance",
        "reporting_unit",
        "specification", "specification_hash", "lines", "formula", "content_hash",
    }
    if not isinstance(value, Mapping) or set(value) != fields:
        raise ForecastModelValidationError("cash-flow companion has an invalid closed shape")
    wire = dict(value)
    if (wire["schema_version"] not in {"cash-flow-companion-0.1", "cash-flow-companion-0.2"}
            or wire["authority_ref"] != CASH_FLOW_COMPANION_REF):
        raise ForecastModelValidationError("cash-flow companion contract is unsupported")
    for field in ("company_ref", "spec_ref", "reporting_unit"):
        wire[field] = _text(wire[field], f"cash_flow_companion.{field}")
    for field in ("spec_hash", "financial_input_hash", "income_structure_hash",
                  "specification_hash", "content_hash"):
        wire[field] = _sha256(wire[field], f"cash_flow_companion.{field}")
    wire["cash_statement_importance"] = _one_of(
        wire["cash_statement_importance"],
        ("required", "supporting", "not_material"),
        "cash_flow_companion.cash_statement_importance")
    if (not isinstance(wire["specification"], Mapping)
            or content_hash(wire["specification"]) != wire["specification_hash"]):
        raise ForecastModelValidationError(
            "cash-flow companion specification hash differs")
    wire["specification"] = dict(wire["specification"])
    if set(wire["specification"]) != {"schema_version", "lines", "formula"} or (
        wire["specification"].get("schema_version") not in {"0.1", "0.2"}
    ):
        raise ForecastModelValidationError(
            "cash-flow companion specification has an invalid closed shape")
    declared_lines = wire["specification"].get("lines")
    if not isinstance(declared_lines, list) or len(declared_lines) != 2:
        raise ForecastModelValidationError("cash-flow companion specification is incomplete")
    if not isinstance(wire["lines"], list) or len(wire["lines"]) != 2:
        raise ForecastModelValidationError("cash-flow companion must carry its two source lines")
    line_fields = {
        "ref", "result_ref", "role", "status", "concept", "unit", "source",
        "source_hash",
        "reason", "forecast_method", "forecast_base_ref", "forecast_base_result_ref",
    }
    if wire["schema_version"] == "cash-flow-companion-0.2":
        line_fields.add("historical_base")
    normalized_lines = []
    for index, item in enumerate(wire["lines"]):
        if not isinstance(item, Mapping) or set(item) != line_fields:
            raise ForecastModelValidationError(
                f"cash_flow_companion.lines[{index}] has an invalid closed shape")
        line = dict(item)
        if line.get("historical_base") is not None:
            from .cash_flow_filed_tie_base import RULE_REF as FILED_TIE_RULE
            basis = line["historical_base"]
            expected = {"schema_version", "rule_ref", "source_basis", "base_ref", "tie_out_concept",
                        "source", "source_hash", "driver", "driver_hash", "comparisons",
                        "target_periods", "cash_window_periods", "missing_formula_periods", "trailing", "content_hash"}
            if (not isinstance(basis, Mapping) or set(basis) != expected
                    or basis["schema_version"] != "0.1" or basis["rule_ref"] != FILED_TIE_RULE
                    or basis["source_basis"] != "independent_reported_tie"
                    or basis["base_ref"] != line["forecast_base_ref"]
                    or content_hash(basis["source"]) != basis["source_hash"]
                    or content_hash(basis["driver"]) != basis["driver_hash"]
                    or content_hash({k:v for k,v in basis.items() if k != "content_hash"}) != basis["content_hash"]):
                raise ForecastModelValidationError("cash filed historical base has invalid source identity")
        role = _one_of(line["role"], (OPERATING_CASH_FLOW, CAPITAL_EXPENDITURE),
                       f"cash_flow_companion.lines[{index}].role")
        if (line["ref"] != f"cash-flow:{role.replace('_', '-')}"
                or line["result_ref"] != f"result:{role}"):
            raise ForecastModelValidationError("cash-flow companion line identity differs")
        line["status"] = _one_of(
            line["status"], ("filed", "incomplete", "unavailable"),
            f"cash_flow_companion.lines[{index}].status")
        source_bound = line["status"] in {"filed", "incomplete"}
        if line["source"] is not None:
            if not isinstance(line["source"], Mapping):
                raise ForecastModelValidationError("cash-flow companion source is invalid")
            line["source"] = dict(line["source"])
            if line["source_hash"] != content_hash(line["source"]):
                raise ForecastModelValidationError("cash-flow companion source hash differs")
        elif line["source_hash"] is not None:
            raise ForecastModelValidationError("cash-flow companion source hash lacks source")
        if source_bound:
            line["concept"] = _text(line["concept"], "cash-flow concept")
            line["unit"] = _text(line["unit"], "cash-flow unit").casefold()
            line["source_hash"] = _sha256(line["source_hash"], "cash-flow source_hash")
            expected_source_status = FILED if line["status"] == "filed" else INCOMPLETE
            if (line["source"] is None
                    or line["source"].get("status") != expected_source_status
                    or str(line["source"].get("role")) != role
                    or str(line["source"].get("concept")) != line["concept"]
                    or str(line["source"].get("unit")).casefold() != line["unit"]
                    or line["unit"] != wire["reporting_unit"]):
                raise ForecastModelValidationError("cash-flow companion source authority differs")
            method = _one_of(
                line["forecast_method"], ("share_of_line", "unavailable"),
                "cash-flow forecast_method")
            if method == "share_of_line" and line["status"] == "filed":
                if (line["reason"] is not None
                        or not isinstance(line["forecast_base_ref"], str)
                        or not line["forecast_base_ref"]
                        or not isinstance(line["forecast_base_result_ref"], str)
                        or not line["forecast_base_result_ref"]):
                    raise ForecastModelValidationError(
                        "forecastable cash-flow companion line differs")
            elif (line["reason"] is None or line["forecast_base_ref"] is not None
                  or line["forecast_base_result_ref"] is not None):
                raise ForecastModelValidationError(
                    "unforecastable filed cash-flow companion line differs")
            if line["reason"] is not None:
                line["reason"] = _text(line["reason"], "cash-flow unavailable reason")
        else:
            if ((line["concept"] is not None and not isinstance(line["concept"], str))
                    or line["unit"] is not None
                    or line["reason"] is None or line["forecast_method"] != "unavailable"
                    or line["forecast_base_ref"] is not None
                    or line["forecast_base_result_ref"] is not None):
                raise ForecastModelValidationError("unavailable cash-flow companion line differs")
            line["reason"] = _text(line["reason"], "cash-flow unavailable reason")
            if line["source_hash"] is not None:
                line["source_hash"] = _sha256(
                    line["source_hash"], "cash-flow unavailable source_hash")
        normalized_lines.append(line)
    if [item["role"] for item in normalized_lines] != [
        OPERATING_CASH_FLOW, CAPITAL_EXPENDITURE,
    ]:
        raise ForecastModelValidationError("cash-flow companion source line order differs")
    declarations: dict[str, Mapping[str, Any]] = {}
    for index, item in enumerate(declared_lines):
        if not isinstance(item, Mapping) or set(item) != {
            "role", "concept", "forecast_method", "forecast_base_ref", "because",
        }:
            raise ForecastModelValidationError(
                f"cash-flow companion specification line {index} differs")
        role = item.get("role")
        if role not in {OPERATING_CASH_FLOW, CAPITAL_EXPENDITURE} or role in declarations:
            raise ForecastModelValidationError("cash-flow companion specification roles differ")
        if item.get("forecast_method") not in {"share_of_line", "unavailable"}:
            raise ForecastModelValidationError("cash-flow companion forecast method differs")
        if not isinstance(item.get("because"), str) or not item["because"].strip():
            raise ForecastModelValidationError("cash-flow companion reason is empty")
        if item.get("concept") is not None and (
            not isinstance(item["concept"], str) or not item["concept"].strip()
        ):
            raise ForecastModelValidationError("cash-flow companion concept differs")
        if item["forecast_method"] == "share_of_line" and (
            not isinstance(item.get("forecast_base_ref"), str)
            or not item["forecast_base_ref"].strip() or item.get("concept") is None
        ):
            raise ForecastModelValidationError("cash-flow companion forecast base differs")
        if item["forecast_method"] == "unavailable" and item.get("forecast_base_ref") is not None:
            raise ForecastModelValidationError("unavailable cash-flow line has a base")
        declarations[str(role)] = item
    for line in normalized_lines:
        declared = declarations[str(line["role"])]
        if line["concept"] != declared["concept"]:
            raise ForecastModelValidationError(
                "cash-flow companion selected concept differs from specification")
        if line["status"] == "filed" and declared["forecast_method"] == "share_of_line":
            if (line["forecast_method"] != "share_of_line"
                    or line["forecast_base_ref"] != declared["forecast_base_ref"]):
                raise ForecastModelValidationError(
                    "cash-flow companion forecast basis differs from specification")
    wire["lines"] = normalized_lines
    formula = wire["formula"]
    if formula != {
        "output_ref": "result:free_cash_flow", "operator": "sum",
        "terms": [
            {"result_ref": "result:operating_cash_flow", "coefficient": "1"},
            {"result_ref": "result:capital_expenditure", "coefficient": "-1"},
        ],
    }:
        raise ForecastModelValidationError("cash-flow companion formula differs")
    if wire["specification"]["formula"] != {
        "output_ref": "free_cash_flow", "operator": "sum",
        "terms": [
            {"role": "operating_cash_flow", "coefficient": "1"},
            {"role": "capital_expenditure", "coefficient": "-1"},
        ],
    }:
        raise ForecastModelValidationError("cash-flow companion specification formula differs")
    base = dict(wire)
    asserted = base.pop("content_hash")
    if content_hash(base) != asserted:
        raise ForecastModelValidationError("cash-flow companion content_hash is invalid")
    return wire


def cash_flow_formula_hash(
    structure: Mapping[str, Any], binding: Mapping[str, Any],
    companion: Mapping[str, Any],
) -> str:
    return content_hash({
        "executor_contract": STRUCTURE_EXECUTOR_CONTRACT,
        "cash_executor_contract": CASH_FLOW_COMPANION_CONTRACT,
        "structure_hash": structure.get("content_hash"),
        "binding_hash": binding.get("content_hash"),
        "cash_flow_companion_hash": companion.get("content_hash"),
    })


def build_cash_flow_companion_drivers(
    table: Mapping[str, Any], companion: Mapping[str, Any],
) -> list[dict[str, Any]]:
    wire = validate_cash_flow_companion(companion)
    selected = {
        str(item.get("role")): item for item in (table.get("cash_flow_inputs") or [])
        if isinstance(item, Mapping)
    }
    drivers: list[dict[str, Any]] = []
    for line in wire["lines"]:
        if line["status"] not in {"filed", "incomplete"}:
            continue
        source = selected.get(str(line["role"]))
        if (source is None or content_hash(source) != line["source_hash"]
                or source.get("status") != (
                    FILED if line["status"] == "filed" else INCOMPLETE)
                or str(source.get("concept")) != line["concept"]
                or str(source.get("unit")).casefold() != line["unit"]):
            raise ForecastModelUnavailable(
                f"cash-flow companion source {line['role']} differs from current inputs")
        history = [{
            "concept": str(line["concept"]),
            "period_start": item.get("period_start"),
            "period_end": str(item["period_end"]),
            "value": str(item["value"]),
            "basis": str(item["basis"]),
            "accessions": [str(ref) for ref in item.get("source_accessions") or []],
            "source_forms": [str(form) for form in item.get("source_forms") or []],
            **({"derived_from": [
                {**dict(operand), "unit": str(operand.get("unit") or "").casefold()}
                for operand in item["derived_from"]
            ]}
               if item.get("derived_from") else {}),
        } for item in ((source.get("series") or {}).get("quarters") or [])]
        drivers.append({
            "ref": f"concept:{line['concept']}", "kind": "cash_flow",
            "label": ("Operating cash flow" if line["role"] == OPERATING_CASH_FLOW
                      else "Capital expenditure"),
            "concept": line["concept"], "unit": line["unit"], "statement": "cash",
            "status": FILED, "role": line["role"], "spec_rows": [line["ref"]],
            "note": None, "history": history,
            "structure_line_ref": line["ref"],
            "forecast_method": line["forecast_method"],
            "forecast_base_ref": line["forecast_base_ref"],
        })
    return drivers


def default_cash_flow_companion_assumptions(
    income_drivers: Sequence[Mapping[str, Any]],
    cash_drivers: Sequence[Mapping[str, Any]],
    periods: Sequence[Mapping[str, str]], companion: Mapping[str, Any], *,
    decided_by: str = AUTOMATION_ACTOR,
    income_structure: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    wire = validate_cash_flow_companion(companion)
    by_role = {str(item["role"]): item for item in wire["lines"]}
    by_line = {
        str(item.get("structure_line_ref")): item for item in income_drivers
        if item.get("structure_line_ref")
    }
    assumptions: list[dict[str, Any]] = []
    for driver in cash_drivers:
        source = by_role[str(driver["role"])]
        base_driver = by_line.get(str(source["forecast_base_ref"]))
        if source.get("historical_base") is not None:
            from .cash_flow_filed_tie_base import validate as validate_filed_tie
            if income_structure is None:
                continue
            validate_filed_tie(wire, income_structure, income_drivers)
            trailing = {**source["historical_base"]["trailing"],
                        "value": Decimal(source["historical_base"]["trailing"]["value"])}
        elif base_driver is None:
            # A derived subtotal is a result, never a fabricated filed driver.
            # The exact validated structure is supplied by the builder/replayer.
            structure = income_structure
            if structure is None or wire["specification"].get("schema_version") != "0.2":
                continue
            from .cash_flow_derived_base import derived_base_is_forecastable, derived_trailing_share
            if not derived_base_is_forecastable(structure, str(source["forecast_base_ref"])):
                continue
            historical, historical_refs = _structure_historical_values(income_drivers, structure)
            base_ref = str(source["forecast_base_ref"])
            trailing = derived_trailing_share(
                quarterly_history(driver), historical.get(base_ref, {}),
                historical_refs.get(base_ref, {}), income_drivers,
                quarters=TRAILING_QUARTERS, rate_quant=_RATE_QUANT)
        else:
            base_cells = quarterly_history(base_driver)
            trailing = trailing_share(quarterly_history(driver), base_cells)
        if trailing is None:
            continue
        derived_base = base_driver is None
        because = (
            f"the average share of {source['forecast_base_ref']} over the "
            f"{trailing['count']} "
            f"quarters filed between {trailing['first_period']} and "
            f"{trailing['last_period']} ({_percent(trailing['value'])}%), carried "
            f"forward against {source['forecast_base_ref']} by the cash-flow companion"
        )
        if derived_base:
            because = (
                f"the average share of formula-derived {source['forecast_base_ref']} "
                f"over {trailing['count']} complete source-replayed quarters between "
                f"{trailing['first_period']} and {trailing['last_period']} "
                f"({_percent(trailing['value'])}%), carried forward against the "
                "explicit income DAG result; the denominator is not a filed fact")
        for period in periods:
            extra = {}
            if source.get("historical_base") is not None:
                from .cash_flow_filed_tie_base import RULE_REF
                extra["rule_ref"] = RULE_REF
                because = (f"the average cash share of independently reported {source['historical_base']['tie_out_concept']} "
                           f"over the latest {trailing['count']} source-bound quarters from {trailing['first_period']} "
                           f"to {trailing['last_period']}, used only as historical denominator; missing income formula "
                           "components remain missing and future cash still uses the declared income DAG result")
            elif derived_base:
                from .cash_flow_derived_base import CONTRACT_REF
                extra["rule_ref"] = CONTRACT_REF
            assumptions.append(_assumption(
                driver_ref=str(driver["ref"]), period=period,
                measure="share_of_line", value=trailing["value"], unit="ratio",
                because=because, refs=trailing["refs"], decided_by=decided_by,
                **extra,
            ))
    return sorted(assumptions, key=lambda item: (
        str(item["driver_ref"]), str(item["period"]["end"])))


#: 2026-10-09 (P0): the working-capital lines whose change enters a derived
#: operating cash flow, with the sign of their increase.
_WORKING_CAPITAL_SIGNS = (("accounts_receivable", -1), ("accounts_payable", 1),
                          ("deferred_revenue", 1))
CASH_BRIDGE_RULE = "rule:cash-bridge-consistency:1"
#: A forecast quarter whose operating margin is at or below this is "deeply
#: negative" for the cash-bridge consistency check.
DEEP_LOSS_MARGIN = Decimal("-0.05")


def _income_values(income_results: Sequence[Mapping[str, Any]], role: str
                   ) -> tuple[str | None, dict[str, Decimal]]:
    found = [item for item in income_results if item.get("role") == role]
    if len(found) != 1:
        return None, {}
    item = found[0]
    return str(item["ref"]), {
        str(cell["period"]["end"]): _decimal(cell["value"], "income result")
        for cell in item.get("cells") or []
        if cell.get("status") == "computed" and not cell.get("superseded_by")}


def operating_cash_residual(
    income_drivers: Sequence[Mapping[str, Any]], cash_drivers: Sequence[Mapping[str, Any]],
    structure: Mapping[str, Any], *, quarters: int = 4,
) -> dict[str, Any] | None:
    """The trailing mean of filed CFO - net income - D&A - working-capital change.

    What a derived cash flow carries for everything else between earnings and
    cash (share-based compensation, deferred taxes, other non-cash items):
    the company's own recent filed relation, carried forward as an amount.
    None when the filings do not give the four quarters.
    """

    try:
        values, _refs = _structure_historical_values(income_drivers, structure)
    except ForecastModelUnavailable:
        return None
    by_role: dict[str, dict[str, Decimal]] = {}
    for line in structure.get("lines") or []:
        role = str(line.get("role"))
        if str(line.get("ref")) in values and role not in by_role:
            by_role[role] = values[str(line["ref"])]
    ocf_driver = next((item for item in cash_drivers
                       if item.get("role") == OPERATING_CASH_FLOW), None)
    if ocf_driver is None or "net_income" not in by_role \
            or "depreciation_amortization" not in by_role:
        return None
    ocf = {str(cell["period_end"]): _decimal(cell["value"], "history value")
           for cell in quarterly_history(ocf_driver)}
    net, da = by_role["net_income"], by_role["depreciation_amortization"]
    working = [(by_role[role], sign) for role, sign in _WORKING_CAPITAL_SIGNS if role in by_role]
    ends = sorted(set(ocf) & set(net) & set(da))
    residuals: list[tuple[str, Decimal]] = []
    for end in ends:
        total = ocf[end] - net[end] - da[end]
        complete = True
        for series, sign in working:
            prior = [day for day in sorted(series) if day < end]
            if end not in series or not prior:
                complete = False
                break
            total -= sign * (series[end] - series[prior[-1]])
        if complete:
            residuals.append((end, total))
    used = residuals[-quarters:]
    if len(used) < quarters:
        return None
    mean = sum((value for _end, value in used), Decimal(0)) / Decimal(len(used))
    return {"value": mean.quantize(_VALUE_QUANT, ROUND_HALF_UP),
            "ends": [end for end, _value in used]}


def derived_operating_cash_flow(
    income_results: Sequence[Mapping[str, Any]], periods: Sequence[Mapping[str, str]],
    balance_last: Mapping[str, Decimal] | None = None,
    residual: Mapping[str, Any] | None = None,
) -> dict[str, Any] | None:
    """CFO = net income + D&A + working-capital change, when those lines exist.

    The default whenever the model computes net income and carries a D&A
    line: a cash flow that cannot stay positive while the model's earnings
    collapse.  Working capital is each held receivable/payable/deferred
    revenue line's change (receivables with the opposite sign); a line the
    model does not carry contributes nothing, a line it carries but cannot
    compute for a quarter leaves that quarter unavailable.  None when net
    income or D&A is not a line of this model.
    """

    net_ref, net = _income_values(income_results, "net_income")
    da_ref, da = _income_values(income_results, "depreciation_amortization")
    if net_ref is None or da_ref is None:
        return None
    working: list[tuple[str, int, dict[str, Decimal], Decimal | None]] = []
    for role, sign in _WORKING_CAPITAL_SIGNS:
        ref, values = _income_values(income_results, role)
        if ref is not None:
            working.append((ref, sign, values, (balance_last or {}).get(role)))
    values: dict[str, Decimal] = {}
    reasons: dict[str, str] = {}
    links: dict[str, list[dict[str, str]]] = {}
    for period in periods:
        end = str(period["end"])
        if end not in net or end not in da:
            reasons[end] = "net income or D&A is unavailable for this quarter"
            continue
        total = net[end] + da[end]
        cited = [{"ref": net_ref, "period_end": end}, {"ref": da_ref, "period_end": end}]
        missing = None
        for index, (ref, sign, series, last) in enumerate(working):
            prior_end = None
            ends = [str(item["end"]) for item in periods]
            position = ends.index(end)
            prior = series.get(ends[position - 1]) if position > 0 else last
            if position > 0:
                prior_end = ends[position - 1]
            if end not in series or prior is None:
                missing = ref
                break
            total += sign * (series[end] - prior)
            cited.append({"ref": ref, "period_end": end})
            if prior_end is not None:
                cited.append({"ref": ref, "period_end": prior_end})
        if missing is not None:
            reasons[end] = f"working-capital line {missing} is unavailable for this quarter"
            continue
        if residual is not None:
            total += _decimal(residual["value"], "operating cash residual")
        values[end] = total.quantize(_VALUE_QUANT, ROUND_HALF_UP)
        links[end] = cited
    return {"values": values, "reasons": reasons, "links": links,
            "formula": ("operating_cash_flow[k] = net_income[k] + depreciation_amortization[k] "
                        + "".join(f" {'-' if sign < 0 else '+'} Δ{ref.split(':', 1)[-1]}[k]"
                                  for ref, sign, _v, _l in working)
                        + ("" if residual is None else
                           f" + other_operating_items ({residual['value']}: the mean of filed "
                           f"CFO - net income - D&A - Δworking capital over "
                           f"{', '.join(residual['ends'])}, i.e. share-based pay and other "
                           "non-cash items, carried forward)")
                        + f" ({CASH_BRIDGE_RULE})")}


def cash_bridge_contradiction(
    income_results: Sequence[Mapping[str, Any]], periods: Sequence[Mapping[str, str]],
    operating_cash: Mapping[str, Decimal],
) -> dict[str, Any] | None:
    """The first quarter a share-of-revenue cash flow contradicts the model's losses.

    Operating cash flow positive and not falling while the model's operating
    income is deeply negative (margin at or below ``DEEP_LOSS_MARGIN``) is
    not a forecast, it is two forecasts that disagree.  Returns the quarter
    and the numbers, or None.
    """

    _ref, operating = _income_values(income_results, "operating_income")
    _rev_ref, revenue = _income_values(income_results, "revenue")
    previous = None
    for period in periods:
        end = str(period["end"])
        cash, income, sales = operating_cash.get(end), operating.get(end), revenue.get(end)
        if cash is not None and income is not None and sales and income / sales <= DEEP_LOSS_MARGIN \
                and cash > 0 and (previous is None or cash >= previous):
            return {"period_end": end, "operating_income": income, "revenue": sales,
                    "operating_cash_flow": cash,
                    "reason": (f"现金桥矛盾（{CASH_BRIDGE_RULE}）：模型在 {end} 的营业利润为 {income}"
                               f"（营业利润率 {(income / sales * 100).quantize(Decimal('0.1'))}%），"
                               f"经营现金流却按收入比例外推为 +{cash} 且仍在上升；模型没有净利润＋折旧＋"
                               "营运资金的现金桥，自此季起经营现金流与自由现金流不可用")}
        if cash is not None:
            previous = cash
    return None


def compute_cash_flow_companion_results(
    cash_drivers: Sequence[Mapping[str, Any]],
    assumptions: Sequence[Mapping[str, Any]],
    periods: Sequence[Mapping[str, str]], companion: Mapping[str, Any],
    income_results: Sequence[Mapping[str, Any]],
    balance_last: Mapping[str, Decimal] | None = None,
    residual: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Forecast the two selected cash lines and their explicitly bound FCF sum.

    2026-10-09 (P0): operating cash flow is derived from net income + D&A +
    working capital whenever the model has those lines
    (:func:`derived_operating_cash_flow`); otherwise the share-of-revenue
    forecast stands only until it contradicts the model's own losses
    (:func:`cash_bridge_contradiction`), and from that quarter on operating
    and free cash flow are unavailable with the contradiction as the reason.
    """

    wire = validate_cash_flow_companion(companion)
    derived = derived_operating_cash_flow(income_results, periods, balance_last, residual)
    income_by_ref = {str(item.get("ref")): item for item in income_results}
    by_driver = {str(item["ref"]): item for item in cash_drivers}
    by_assumption = {
        (str(item["driver_ref"]), str(item["period"]["end"])): item
        for item in assumptions if not item.get("superseded_by")
    }
    results: list[dict[str, Any]] = []
    values: dict[str, dict[str, Decimal]] = {}
    for source in wire["lines"]:
        role, result_ref = str(source["role"]), str(source["result_ref"])
        driver_ref = (
            f"concept:{source['concept']}" if source["status"] == "filed" else None)
        result = _result(
            result_ref, role,
            "Operating cash flow" if role == OPERATING_CASH_FLOW else "Capital expenditure",
            str(source.get("unit") or wire["reporting_unit"]),
            f"{role}[k] = {source.get('forecast_base_ref')}[k] * share[k]",
            driver_ref=driver_ref,
        )
        base_result = income_by_ref.get(str(source.get("forecast_base_result_ref")))
        base_values = {
            str(cell["period"]["end"]): _decimal(cell["value"], "cash-flow base result")
            for cell in ((base_result or {}).get("cells") or [])
            if cell.get("status") == "computed" and not cell.get("superseded_by")
        }
        values[result_ref] = {}
        driver = None if driver_ref is None else by_driver.get(driver_ref)
        if role == OPERATING_CASH_FLOW and derived is not None:
            result["formula"] = derived["formula"]
            for period in periods:
                end = str(period["end"])
                if end in derived["values"]:
                    values[result_ref][end] = derived["values"][end]
                    result["cells"].append(_computed_cell(
                        result_ref, period, derived["values"][end],
                        results=derived["links"][end]))
                else:
                    result["cells"].append(_unavailable_cell(
                        result_ref, period, derived["reasons"].get(end)
                        or "net income or D&A is unavailable for this quarter"))
            results.append(_finish(result))
            continue
        for period in periods:
            end = str(period["end"])
            if source["status"] != "filed" or driver is None:
                result["cells"].append(_unavailable_cell(
                    result_ref, period, str(source["reason"])))
                continue
            assumption = by_assumption.get((driver_ref, end))
            base = base_values.get(end)
            if assumption is None:
                result["cells"].append(_unavailable_cell(
                    result_ref, period,
                    f"no cash-flow share assumption for {source['concept']} in this quarter"))
                continue
            if base is None:
                result["cells"].append(_unavailable_cell(
                    result_ref, period,
                    f"structured base {source.get('forecast_base_ref')} is unavailable "
                    "for this quarter"))
                continue
            value = (base * _decimal(assumption["value"], "cash-flow assumption")).quantize(
                _VALUE_QUANT, ROUND_HALF_UP)
            if role == CAPITAL_EXPENDITURE and value < 0:
                result["cells"].append(_unavailable_cell(
                    result_ref, period,
                    "the forecast base would make positive-outflow capital expenditure negative"))
                continue
            values[result_ref][end] = value
            result["cells"].append(_computed_cell(
                result_ref, period, value,
                assumptions=[str(assumption["ref"])],
                results=[{
                    "ref": str(source["forecast_base_result_ref"]), "period_end": end,
                }],
            ))
        results.append(_finish(result))
    if derived is None:
        conflict = cash_bridge_contradiction(
            income_results, periods, values.get("result:operating_cash_flow") or {})
        if conflict is not None:
            ends = [str(period["end"]) for period in periods]
            cut = set(ends[ends.index(conflict["period_end"]):])
            for index, item in enumerate(results):
                if item.get("ref") != "result:operating_cash_flow":
                    continue
                cells = []
                for cell in item["cells"]:
                    end = str(cell["period"]["end"])
                    if end in cut and cell.get("status") == "computed":
                        values["result:operating_cash_flow"].pop(end, None)
                        cells.append(_unavailable_cell(
                            "result:operating_cash_flow", cell["period"], conflict["reason"]))
                    else:
                        cells.append(cell)
                results[index] = _finish({**item, "cells": cells})
    fcf = _result(
        "result:free_cash_flow", None, "Free cash flow",
        wire["reporting_unit"],
        "free_cash_flow[k] = operating_cash_flow[k] - capital_expenditure[k]",
    )
    for period in periods:
        end = str(period["end"])
        ocf = values["result:operating_cash_flow"].get(end)
        capex = values["result:capital_expenditure"].get(end)
        if ocf is None or capex is None:
            ocf_cell = next((cell for item in results if item.get("ref") == "result:operating_cash_flow"
                             for cell in item["cells"] if str(cell["period"]["end"]) == end), None)
            reason = (ocf_cell or {}).get("reason") if ocf is None else None
            fcf["cells"].append(_unavailable_cell(
                "result:free_cash_flow", period,
                reason if reason and CASH_BRIDGE_RULE in reason else
                "operating cash flow or capital expenditure is unavailable for this quarter"))
        else:
            fcf["cells"].append(_computed_cell(
                "result:free_cash_flow", period, ocf - capex,
                results=[
                    {"ref": "result:operating_cash_flow", "period_end": end},
                    {"ref": "result:capital_expenditure", "period_end": end},
                ],
            ))
    results.append(_finish(fcf))
    return results


NET_CASH_CHANGE_REF = "result:net_cash_change"
_CASH_OUTFLOW_ROLES = ("share_repurchase", "dividends_paid", "acquisitions")


def roll_cash_balance(
    drivers: Sequence[Mapping[str, Any]], periods: Sequence[Mapping[str, str]],
    structure: Mapping[str, Any], results: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Roll the cash balance forward from the forecast cash flow (structure 0.6).

    ``net_cash_change[k] = free_cash_flow[k] - buybacks[k] - dividends[k] -
    acquisitions[k]`` over the outflow lines the structure holds, and
    ``cash[k] = cash[k-1] + net_cash_change[k]`` starting from the last filed
    balance.  A first-version roll: debt issuance, FX and other financing are
    not modelled, which the formula text says.  Without a free-cash-flow line
    the cash balance is held at its last filed value instead.
    """

    lines = [line for line in structure.get("lines") or []
             if line.get("forecast_method") == "roll_forward" and line.get("role") == "cash_and_equivalents"]
    if not lines:
        return [dict(item) for item in results]
    by_ref = {str(item.get("ref")): dict(item) for item in results}
    result_refs = _structure_result_refs(structure)
    roles = {str(line.get("role")): str(line["ref"]) for line in structure.get("lines") or []}
    fcf = by_ref.get("result:free_cash_flow")
    outflows = [result_refs[roles[role]] for role in _CASH_OUTFLOW_ROLES if role in roles]

    def live(ref: str) -> dict[str, Decimal]:
        line = by_ref.get(ref) or {}
        return {str(cell["period"]["end"]): _decimal(cell["value"], "result value")
                for cell in line.get("cells") or []
                if cell.get("status") == "computed" and cell.get("kind") == "estimate"
                and not cell.get("superseded_by")}

    out = [dict(item) for item in results]
    change: dict[str, Decimal] = {}
    if fcf is not None:
        net = _result(
            NET_CASH_CHANGE_REF, None, "现金净变动（第一版：自由现金流 − 回购 − 分红 − 并购）", str(
                fcf.get("unit") or "usd"),
            "net_cash_change[k] = free_cash_flow[k] - "
            + " - ".join(item.split(":", 1)[1] for item in outflows)
            + "[k] (debt issuance, FX and other financing not modelled)"
            if outflows else "net_cash_change[k] = free_cash_flow[k]")
        fcf_values = live("result:free_cash_flow")
        outflow_values = {ref: live(ref) for ref in outflows}
        for period in periods:
            end = str(period["end"])
            missing = ([] if end in fcf_values else ["result:free_cash_flow"]) + [
                ref for ref in outflows if end not in outflow_values[ref]]
            if missing:
                net["cells"].append(_unavailable_cell(
                    NET_CASH_CHANGE_REF, period,
                    "formula terms unavailable for this quarter: " + ", ".join(missing)))
                continue
            value = fcf_values[end] - sum(
                (outflow_values[ref][end] for ref in outflows), Decimal(0))
            change[end] = value
            net["cells"].append(_computed_cell(
                NET_CASH_CHANGE_REF, period, value,
                results=[{"ref": "result:free_cash_flow", "period_end": end}]
                + [{"ref": ref, "period_end": end} for ref in outflows]))
        out.append(_finish(net))
    drivers_by_line = {str(item.get("structure_line_ref")): item for item in drivers}
    for line in lines:
        ref = result_refs[str(line["ref"])]
        driver = drivers_by_line.get(str(line["ref"]))
        history = list((driver or {}).get("history") or [])
        rolled = _result(
            ref, str(line.get("role")), str(line.get("label")), str(line.get("unit")),
            (f"{line['ref']}[k] = {line['ref']}[k-1] + net_cash_change[k]"
             if fcf is not None else f"{line['ref']}[k] = {line['ref']}[last filed] (held: "
             "no free-cash-flow forecast to roll it with)"),
            driver_ref=None if driver is None else str(driver["ref"]))
        if not history:
            for period in periods:
                rolled["cells"].append(_unavailable_cell(
                    ref, period, f"statement line {line['ref']} has no filed balance"))
        else:
            level = _decimal(history[-1]["value"], "history value")
            prior: dict[str, Any] = _cell_ref(history[-1])
            for period in periods:
                end = str(period["end"])
                if fcf is None:
                    rolled["cells"].append(_computed_cell(
                        ref, period, level, inputs=[_cell_ref(history[-1])]))
                    continue
                if end not in change or level is None:
                    level = None
                    rolled["cells"].append(_unavailable_cell(
                        ref, period, "the net change in cash is unavailable for this quarter"))
                    continue
                level = level + change[end]
                links = [{"ref": NET_CASH_CHANGE_REF, "period_end": end}]
                if prior.get("kind") == "input_cell":
                    rolled["cells"].append(_computed_cell(
                        ref, period, level, inputs=[prior], results=links))
                else:
                    rolled["cells"].append(_computed_cell(
                        ref, period, level, results=[prior, *links]))
                prior = {"ref": ref, "period_end": end}
        rolled = _finish(rolled)
        out = [rolled if str(item.get("ref")) == ref else item for item in out]
    return out


def compute_structured_model_results(
    drivers: Sequence[Mapping[str, Any]], assumptions: Sequence[Mapping[str, Any]],
    periods: Sequence[Mapping[str, str]], structure: Mapping[str, Any],
    companion: Mapping[str, Any] | None,
) -> list[dict[str, Any]]:
    """Income DAG, then the cash companion, then the cash roll -- one order."""

    income_drivers = [item for item in drivers if item.get("kind") != "cash_flow"]
    cash_drivers = [item for item in drivers if item.get("kind") == "cash_flow"]
    income = compute_structure_results(income_drivers, assumptions, periods, structure)
    results = list(income)
    if companion is not None:
        # The last filed working-capital balances, for the first forecast
        # quarter's change in a derived operating cash flow.
        balance_last: dict[str, Decimal] = {}
        roles = {str(line.get("ref")): str(line.get("role"))
                 for line in structure.get("lines") or []}
        for driver in income_drivers:
            role = roles.get(str(driver.get("structure_line_ref")))
            history = driver.get("history") or []
            if role in {name for name, _sign in _WORKING_CAPITAL_SIGNS} and history:
                last = max(history, key=lambda cell: str(cell.get("period_end")))
                balance_last[role] = _decimal(last["value"], "history value")
        results.extend(compute_cash_flow_companion_results(
            cash_drivers, assumptions, periods, companion, income, balance_last,
            operating_cash_residual(income_drivers, cash_drivers, structure)))
    return roll_cash_balance(income_drivers, periods, structure, results)


def _coverage_assumptions(
    drivers: Sequence[Mapping[str, Any]], periods: Sequence[Mapping[str, Any]],
    structure: Mapping[str, Any], sources: Mapping[str, Any] | None, actor_ref: str,
) -> list[dict[str, Any]]:
    from .forecast_coverage_assumptions import coverage_structure_assumptions

    return coverage_structure_assumptions(
        drivers, periods, structure, sources=sources, decided_by=actor_ref)


def _mark_bands(
    drivers: Sequence[Mapping[str, Any]], structure: Mapping[str, Any],
    companion: Mapping[str, Any] | None, assumptions: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    from .forecast_coverage_assumptions import mark_outside_band

    record = {
        "schema_version": (STRUCTURED_CASH_SCHEMA_VERSION if companion is not None
                           else STRUCTURED_SCHEMA_VERSION),
        "drivers": [dict(item) for item in drivers],
        "financial_statement_structure": dict(structure),
        "cash_flow_companion": companion,
    }
    return mark_outside_band(record, assumptions)


def build_structured_forecast_model(
    spec: Mapping[str, Any], table: Mapping[str, Any], *,
    structure: Mapping[str, Any], replay: Mapping[str, Any],
    binding: Mapping[str, Any], actor_ref: str = AUTOMATION_ACTOR,
    mission_version_ref: str | None = None,
    assumptions: Sequence[Mapping[str, Any]] | None = None,
    generator_ref: str | None = STRUCTURE_GENERATOR_REF,
    change_reason: str = "evidence_thicker",
    evidence_refs: Sequence[Mapping[str, Any]] | None = None,
    decision: str | None = None,
    assumption_sources: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a v0.4 model from the income DAG and source-bound cash companion.

    A coverage (0.6) structure takes its assumptions from
    ``forecast_coverage_assumptions`` in source-priority order and rolls the
    cash balance after the cash flow; ``assumption_sources`` carries the held
    preliminary actual, guidance/consensus targets and prior judgements.
    """

    from .company_financial_statement_structure import forecast_structure_binding

    exact_binding = forecast_structure_binding(structure, replay, table)
    if dict(binding) != exact_binding:
        raise ForecastModelUnavailable("forecast statement structure binding differs")
    company_ref = _text(table.get("company_ref"), "company_ref")
    spec_ref = _text(table.get("spec_ref") or spec.get("spec_id"), "spec_ref")
    if (
        structure.get("company_ref") != company_ref
        or binding.get("company_ref") != company_ref
        or structure.get("spec_ref") != spec_ref
        or binding.get("spec_ref") != spec_ref
    ):
        raise ForecastModelUnavailable("forecast statement structure belongs to another model")
    horizon = spec.get("horizon") or {}
    wanted = horizon.get("forecast_quarters")
    if isinstance(wanted, bool) or not isinstance(wanted, int) or wanted < 1:
        wanted = MIN_FORECAST_PERIODS
    income_drivers = build_structure_drivers(table, structure)
    from .forecast_financial_execution import additional_drivers
    income_drivers.extend(additional_drivers(spec, table, income_drivers))
    coverage = _is_coverage(structure)
    if coverage and generator_ref == STRUCTURE_GENERATOR_REF:
        generator_ref = COVERAGE_GENERATOR_REF
    if spec.get("schema_version") != "0.4":
        policy = forecast_policy(revenue_anchor(income_drivers), assumption_sources)
        periods = forecast_periods(revenue_anchor(income_drivers), max(wanted, MIN_FORECAST_PERIODS), policy=policy)
        if assumptions is None:
            assumptions = (
                _coverage_assumptions(income_drivers, periods, structure,
                                      assumption_sources, actor_ref)
                if coverage else
                default_structure_assumptions(
                    income_drivers, periods, structure, decided_by=actor_ref))
        if coverage:
            assumptions = _mark_bands(income_drivers, structure, None, assumptions)
        results = (
            compute_structured_model_results(
                income_drivers, assumptions, periods, structure, None)
            if coverage else
            compute_structure_results(income_drivers, assumptions, periods, structure))
        anchor = revenue_anchor(income_drivers)
        if evidence_refs is None:
            evidence_refs = filing_refs(income_drivers, table.get("periods"))
        return _new_forecast_model({
            SOURCE_VERSION_KEY: None,
            "schema_version": STRUCTURED_SCHEMA_VERSION,
            "model_ref": f"forecast-model:{company_ref}",
            "company_ref": company_ref,
            "spec_ref": spec_ref,
            "spec_hash": _sha256(spec.get("content_hash"), "spec.content_hash"),
            "inputs_hash": content_hash(json.loads(canonical_json(table))),
            "unit": str(anchor.get("unit") or "usd"),
            "currency": str(anchor.get("unit") or "usd").upper(),
            "history_periods": [str(item) for item in (table.get("periods") or [])],
            "realised_periods": [],
            "forecast_periods": [dict(item) for item in periods],
            "forecast_policy": policy,
            "statements": statement_importances(spec),
            "formula_ref": STRUCTURE_FORMULA_REF,
            "formula_hash": structure_formula_hash(structure, binding),
            "generator_ref": generator_ref,
            "drivers": income_drivers,
            "assumptions": [dict(item) for item in assumptions],
            "results": results,
            "financial_statement_structure": dict(structure),
            "financial_statement_structure_replay": dict(replay),
            "forecast_structure_binding": dict(binding),
            "change_reason": _one_of(change_reason, CHANGE_REASONS, "change_reason"),
            "evidence_refs": [dict(item) for item in evidence_refs],
            "decision": decision,
            "mission_version_ref": mission_version_ref,
            "actor_ref": actor_ref,
        }, spec=spec, table=table, assumption_sources=assumption_sources)
    companion = build_cash_flow_companion(spec, table, structure)
    cash_drivers = build_cash_flow_companion_drivers(table, companion)
    from .cash_flow_filed_tie_base import drivers as filed_tie_drivers
    income_drivers.extend(filed_tie_drivers(companion))
    drivers = [*income_drivers, *cash_drivers]
    policy = forecast_policy(revenue_anchor(income_drivers), assumption_sources)
    periods = forecast_periods(revenue_anchor(income_drivers), max(wanted, MIN_FORECAST_PERIODS), policy=policy)
    if assumptions is None:
        assumptions = [
            *(_coverage_assumptions(income_drivers, periods, structure,
                                    assumption_sources, actor_ref)
              if coverage else
              default_structure_assumptions(
                  income_drivers, periods, structure, decided_by=actor_ref)),
            *default_cash_flow_companion_assumptions(
                income_drivers, cash_drivers, periods, companion,
                decided_by=actor_ref, income_structure=structure),
        ]
        assumptions = sorted(assumptions, key=lambda item: (
            str(item["driver_ref"]), str(item["period"]["end"])))
    if coverage:
        assumptions = _mark_bands(drivers, structure, companion, assumptions)
        results = compute_structured_model_results(
            drivers, assumptions, periods, structure, companion)
    else:
        income_results = compute_structure_results(
            income_drivers, assumptions, periods, structure)
        results = [
            *income_results,
            *compute_cash_flow_companion_results(
                cash_drivers, assumptions, periods, companion, income_results),
        ]
    anchor = revenue_anchor(income_drivers)
    if evidence_refs is None:
        evidence_refs = filing_refs(drivers, table.get("periods"))
    return _new_forecast_model({
        SOURCE_VERSION_KEY: None,
        "schema_version": STRUCTURED_CASH_SCHEMA_VERSION,
        "model_ref": f"forecast-model:{company_ref}",
        "company_ref": company_ref,
        "spec_ref": spec_ref,
        "spec_hash": _sha256(spec.get("content_hash"), "spec.content_hash"),
        "inputs_hash": content_hash(json.loads(canonical_json(table))),
        "unit": str(anchor.get("unit") or "usd"),
        "currency": str(anchor.get("unit") or "usd").upper(),
        "history_periods": [str(item) for item in (table.get("periods") or [])],
        "realised_periods": [],
        "forecast_periods": [dict(item) for item in periods],
        "forecast_policy": policy,
        "statements": statement_importances(spec),
        "formula_ref": STRUCTURE_FORMULA_REF,
        "formula_hash": cash_flow_formula_hash(structure, binding, companion),
        "generator_ref": generator_ref,
        "drivers": drivers,
        "assumptions": [dict(item) for item in assumptions],
        "results": results,
        "financial_statement_structure": dict(structure),
        "financial_statement_structure_replay": dict(replay),
        "forecast_structure_binding": dict(binding),
        "cash_flow_companion": companion,
        "change_reason": _one_of(change_reason, CHANGE_REASONS, "change_reason"),
        "evidence_refs": [dict(item) for item in evidence_refs],
        "decision": decision,
        "mission_version_ref": mission_version_ref,
        "actor_ref": actor_ref,
    }, spec=spec, table=table, assumption_sources=assumption_sources)


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


def _structure_definition(value: Mapping[str, Any]) -> dict[str, Any]:
    """The company judgement frozen by a structure, apart from input version.

    A new filing necessarily changes the financial-input and replay hashes.  It
    must not silently change the line topology, signs, forecast methods, or
    note evidence while an existing model is merely being actualised.
    """

    if _is_coverage(value):
        # A coverage structure is completed against the filings, so its
        # evidence citations and history depths move with every filing.  The
        # judgement is the topology: lines, signs, methods and bases.
        return {
            "schema_version": value.get("schema_version"),
            "lines": [dict(item) for item in value.get("lines") or []],
            "formulas": [
                {key: item[key] for key in item if key != "evidence_refs"}
                for item in value.get("formulas") or []
            ],
            "note_evidence": value.get("note_evidence"),
        }
    return {
        key: value for key, value in value.items()
        if key not in {"content_hash", "financial_input_hash", "structure_ref"}
    }


def structure_topology_changed(prior: Mapping[str, Any],
                               structure: Mapping[str, Any]) -> bool:
    """Whether a model's held structure differs in judgement from a new one."""

    held = prior.get("financial_statement_structure")
    if not isinstance(held, Mapping):
        return True
    return _structure_definition(held) != _structure_definition(structure)


def _cash_companion_definition(value: Mapping[str, Any]) -> dict[str, Any]:
    """The selected cash concepts/formula, excluding the newly filed bytes."""

    wire = validate_cash_flow_companion(value)
    lines = [{key: item[key] for key in item
              if key not in {"source", "source_hash", "historical_base"}} for item in wire["lines"]]
    if wire["schema_version"] == "cash-flow-companion-0.2":
        for definition, item in zip(lines, wire["lines"]):
            basis = item["historical_base"]
            definition["historical_base_identity"] = (None if basis is None else {
                key:basis[key] for key in ("rule_ref", "source_basis", "base_ref", "tie_out_concept")})
    return {
        key: (lines if key == "lines" else wire[key])
        for key in wire
        if key not in {
            "content_hash", "financial_input_hash", "income_structure_hash", "lines",
        }
    } | {"lines": lines}


def cash_companion_topology_changed(prior, spec, table, structure):
    held=prior.get('cash_flow_companion')
    if not isinstance(held, Mapping):return False
    # Equal source hashes do not prove equal calculation contracts. Rebuild
    # from the current in-memory authority, including same-input upgrades.
    current=build_cash_flow_companion(spec,table,structure)
    return _cash_companion_definition(held)!=_cash_companion_definition(current)


def _cash_historical_values(
    drivers: Sequence[Mapping[str, Any]], companion: Mapping[str, Any],
) -> tuple[dict[str, dict[str, Decimal]], dict[str, dict[str, list[dict[str, Any]]]]]:
    values: dict[str, dict[str, Decimal]] = {}
    refs: dict[str, dict[str, list[dict[str, Any]]]] = {}
    by_role = {str(item.get("role")): item for item in drivers
               if item.get("kind") == "cash_flow"}
    for line in companion.get("lines") or []:
        result_ref = str(line["result_ref"])
        values[result_ref], refs[result_ref] = {}, {}
        driver = by_role.get(str(line["role"]))
        for cell in quarterly_history(driver or {}):
            end = str(cell["period_end"])
            values[result_ref][end] = _decimal(cell["value"], "cash-flow history")
            refs[result_ref][end] = [_cell_ref(cell)]
    values["result:free_cash_flow"], refs["result:free_cash_flow"] = {}, {}
    common = set(values["result:operating_cash_flow"]) & set(
        values["result:capital_expenditure"])
    for end in sorted(common):
        values["result:free_cash_flow"][end] = (
            values["result:operating_cash_flow"][end]
            - values["result:capital_expenditure"][end]
        )
        refs["result:free_cash_flow"][end] = []
    return values, refs


def _roll_actualized_forecast(body: dict[str, Any]) -> dict[str, Any]:
    """Append future quarters while retaining the immutable estimates/actuals."""
    drivers = body["drivers"]
    anchor = revenue_anchor(drivers)
    previous_policy = body.get("forecast_policy") or {}
    policy = forecast_policy(anchor, {"fiscal_year_end": previous_policy.get("fiscal_year_end")})
    periods = forecast_periods(anchor, max(MIN_FORECAST_PERIODS, len(body["forecast_periods"])), policy=policy)
    structure = body.get("financial_statement_structure")
    companion = body.get("cash_flow_companion")
    actor = body["actor_ref"]
    if structure is not None:
        fresh = (_coverage_assumptions(drivers, periods, structure, None, actor)
                 if _is_coverage(structure) else
                 default_structure_assumptions(drivers, periods, structure, decided_by=actor))
        if companion is not None:
            fresh.extend(default_cash_flow_companion_assumptions(
                drivers, [d for d in drivers if d.get("kind") == "cash_flow"], periods,
                companion, decided_by=actor, income_structure=structure))
    else:
        fresh = default_assumptions(drivers, periods, decided_by=actor)
    held = {(a["driver_ref"], a["period"]["end"]) for a in body["assumptions"]
            if not a.get("superseded_by") and a.get("kind") != "actual"}
    assumptions = body["assumptions"] + [a for a in fresh if (a["driver_ref"], a["period"]["end"]) not in held]
    if structure is not None:
        results = compute_structured_model_results(drivers, assumptions, periods, structure, companion)
    else:
        results = compute_results(drivers, assumptions, periods, statements=body["statements"],
                                  base=chain_base(anchor, body, str(periods[0]["end"])))
    ends = {p["end"] for p in periods}
    prior_by_ref = {r["ref"]: r for r in body["results"]}
    for result in results:
        prior = prior_by_ref.get(result["ref"]) or {}
        result["cells"] = [dict(c) for c in prior.get("cells") or []
                           if c["period"]["end"] not in ends] + result["cells"]
        _finish(result)
    body.update(forecast_periods=periods, forecast_policy=policy, assumptions=assumptions, results=results)
    return _new_forecast_model(body)


def _actualize_structured_model(
    prior: Mapping[str, Any], table: Mapping[str, Any], *,
    structure: Mapping[str, Any] | None, replay: Mapping[str, Any] | None,
    binding: Mapping[str, Any] | None,
    evidence_refs: Sequence[Mapping[str, Any]] | None,
    actor_ref: str | None,
    source_accuracy: Mapping[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Actualise a v0.3 model by replaying its exact company statement DAG."""

    from .company_financial_statement_structure import forecast_structure_binding

    if not all(isinstance(item, Mapping) for item in (structure, replay, binding)):
        raise ForecastModelUnavailable(
            "structured model actualisation needs current statement authority")
    assert structure is not None and replay is not None and binding is not None
    exact_binding = forecast_structure_binding(structure, replay, table)
    if dict(binding) != exact_binding:
        raise ForecastModelUnavailable("current statement structure binding differs")
    prior_structure = prior.get("financial_statement_structure")
    if not isinstance(prior_structure, Mapping) or (
        _structure_definition(prior_structure) != _structure_definition(structure)
    ):
        raise ForecastModelUnavailable(
            "the statement structure changed, so this is a new model rather than "
            "an actualisation of the old one")

    ends = realised_ends(prior, table)
    if not ends:
        return None
    realised = set(ends)
    periods_by_end = {
        str(item["end"]): dict(item) for item in (prior.get("forecast_periods") or [])
    }
    income_drivers = build_structure_drivers(table, structure)
    from .forecast_financial_execution import additional_drivers
    income_drivers.extend(additional_drivers((prior.get("execution_contract") or {}).get("specification") or {}, table, income_drivers))
    current_companion = None
    cash_drivers: list[dict[str, Any]] = []
    if prior.get("schema_version") == STRUCTURED_CASH_SCHEMA_VERSION:
        current_companion = build_cash_flow_companion(
            {"schema_version": "0.4", "spec_id": prior["spec_ref"],
             "content_hash": prior["spec_hash"],
             "cash_flow_companion": prior["cash_flow_companion"]["specification"],
             "forecast_statements": [
                 {"statement": key, "importance": value}
                 for key, value in (prior.get("statements") or {}).items()
             ]},
            table, structure,
        )
        prior_companion = prior.get("cash_flow_companion")
        if (not isinstance(prior_companion, Mapping)
                or _cash_companion_definition(prior_companion)
                != _cash_companion_definition(current_companion)):
            raise ForecastModelUnavailable(
                "the cash-flow companion changed, so this is a new model rather than "
                "an actualisation of the old one")
        cash_drivers = build_cash_flow_companion_drivers(table, current_companion)
        from .cash_flow_filed_tie_base import drivers as filed_tie_drivers
        income_drivers.extend(filed_tie_drivers(current_companion))
    drivers = [*income_drivers, *cash_drivers]
    historical, historical_refs = _structure_historical_values(
        income_drivers, structure)
    result_refs = _structure_result_refs(structure)
    formulas = {
        str(item["output_ref"]): item for item in (structure.get("formulas") or [])
    }
    lines = {str(item["ref"]): item for item in (structure.get("lines") or [])}
    cash_values: dict[str, dict[str, Decimal]] = {}
    cash_refs: dict[str, dict[str, list[dict[str, Any]]]] = {}
    if current_companion is not None:
        cash_values, cash_refs = _cash_historical_values(drivers, current_companion)
        for item in current_companion["lines"]:
            result_refs[str(item["ref"])] = str(item["result_ref"])
            lines[str(item["ref"])] = item
        result_refs["cash-flow:free-cash-flow"] = "result:free_cash_flow"
        formulas["cash-flow:free-cash-flow"] = current_companion["formula"]
    if _is_coverage(structure):
        # The cash roll's net change has no filed history of its own; its
        # realised cells stay the estimates they were.
        result_refs["cash-roll:net-change"] = NET_CASH_CHANGE_REF

    results: list[dict[str, Any]] = []
    for prior_line in prior.get("results") or []:
        ref = str(prior_line["ref"])
        line_ref = next((key for key, value in result_refs.items() if value == ref), None)
        if line_ref is None and (ref.startswith("result:free_cash_flow_") or ref == "result:capital_expenditure_company" or ref.startswith("result:fcf-component:")):
            results.append(dict(prior_line))
            continue
        if line_ref is None:
            raise ForecastModelUnavailable(
                f"result {ref} is outside the current statement structure")
        formula = formulas.get(line_ref)
        is_cash = ref in cash_values
        if ref == NET_CASH_CHANGE_REF:
            results.append(_finish(dict(prior_line)))
            continue
        cells: list[dict[str, Any]] = []
        for cell in prior_line.get("cells") or []:
            end = str(cell["period"]["end"])
            if (end not in realised or cell.get("kind") != "estimate"
                    or cell.get("superseded_by")):
                cells.append(dict(cell))
                continue
            value = (cash_values.get(ref, {}).get(end) if is_cash
                     else historical.get(line_ref, {}).get(end))
            if value is None:
                cells.append(dict(cell))
                continue
            if formula is None:
                actual = _computed_cell(
                    ref, periods_by_end[end], value, kind="actual",
                    inputs=(cash_refs.get(ref, {}).get(end, []) if is_cash else
                            historical_refs.get(line_ref, {}).get(end, [])))
            else:
                if is_cash:
                    dependencies = [str(item["result_ref"])
                                    for item in formula["terms"]]
                    dependency_result_refs = dependencies
                else:
                    dependencies = (
                        [str(item["line_ref"]) for item in formula["terms"]]
                        if formula.get("operator") == "sum" else
                        [str(formula["numerator_ref"]), str(formula["denominator_ref"])]
                    )
                    dependency_result_refs = [result_refs[item] for item in dependencies]
                actual = _computed_cell(
                    ref, periods_by_end[end], value, kind="actual",
                    results=[{"ref": item, "period_end": end}
                             for item in dependency_result_refs])
            cells.append({**dict(cell), "superseded_by": actual["ref"]})
            cells.append(actual)
        updated = {**dict(prior_line), "cells": sorted(
            cells, key=lambda item: (str(item["period"]["end"]),
                                     0 if item["kind"] == "estimate" else 1))}
        results.append(_finish(updated))

    drivers_by_ref = {str(item["ref"]): item for item in drivers}
    lines_by_driver = {
        str(item["ref"]): str(item["structure_line_ref"]) for item in drivers
    }
    assumptions: list[dict[str, Any]] = []
    for item in prior.get("assumptions") or []:
        end = str(item["period"]["end"])
        if (end not in realised or item.get("kind") == "actual"
                or item.get("superseded_by")):
            assumptions.append(dict(item))
            continue
        driver_ref = str(item["driver_ref"])
        driver = drivers_by_ref.get(driver_ref)
        line_ref = lines_by_driver.get(driver_ref)
        filed = None if driver is None else _driver_cell(drivers, driver_ref, end)
        actual = None
        if filed is not None and line_ref is not None:
            value = _decimal(filed["value"], "filed value")
            refs = [_cell_ref(filed)]
            rate: Decimal | None = None
            if item.get("measure") == "quarterly_growth":
                base = _prior_quarter_cell(drivers, driver_ref, end)
                if base is not None:
                    divisor = _decimal(base["value"], "filed value")
                    if divisor != 0:
                        rate = (value - divisor) / divisor
                        refs.append(_cell_ref(base))
            elif item.get("measure") == "share_of_line":
                base_ref = str(lines[line_ref].get("forecast_base_ref"))
                divisor = historical.get(base_ref, {}).get(end)
                if divisor is not None and divisor != 0:
                    rate = value / divisor
                    refs.extend(historical_refs.get(base_ref, {}).get(end, []))
            if rate is not None:
                actual = _assumption(
                    driver_ref=driver_ref, period=periods_by_end[end],
                    measure=str(item["measure"]),
                    value=rate.quantize(_RATE_QUANT, ROUND_HALF_UP),
                    unit=str(item["unit"]), kind="actual",
                    because=f"what the filings reported for the quarter ended {end}",
                    refs=refs, decided_by=str(actor_ref or prior.get("actor_ref")),
                    rule_ref=None)
        assumptions.append({**dict(item),
                            "superseded_by": None if actual is None else actual["ref"]})
        if actual is not None:
            assumptions.append(actual)

    keep = (list(prior.get("realised_periods") or [])
            + [periods_by_end[end] for end in ends])[-MAX_REALISED_PERIODS:]
    forecast = [dict(item) for item in (prior.get("forecast_periods") or [])
                if str(item["end"]) not in realised]
    if forecast:
        # The forecasting judgement is unchanged, but a growth chain's k-1 is
        # now the filed actual rather than the superseded estimate. Replay the
        # remaining cells from the same immutable assumptions and company DAG
        # so their stored formulas and values continue to agree exactly.
        if _is_coverage(structure):
            replayed_results = compute_structured_model_results(
                drivers, assumptions, forecast, structure, current_companion)
        else:
            income_replayed = compute_structure_results(
                income_drivers, assumptions, forecast, structure)
            replayed_results = list(income_replayed)
            if current_companion is not None:
                replayed_results.extend(compute_cash_flow_companion_results(
                    cash_drivers, assumptions, forecast, current_companion,
                    income_replayed))
        replayed = {
            str(item["ref"]): item
            for item in replayed_results
        }
        forecast_ends = {str(item["end"]) for item in forecast}
        reanchored: list[dict[str, Any]] = []
        for result in results:
            fresh = replayed.get(str(result["ref"]))
            if fresh is None and (str(result["ref"]).startswith("result:free_cash_flow_") or result["ref"] == "result:capital_expenditure_company" or str(result["ref"]).startswith("result:fcf-component:")):
                reanchored.append(result)
                continue
            if fresh is None:
                raise ForecastModelUnavailable(
                    f"result {result['ref']} could not replay after actualisation")
            cells = [dict(item) for item in result.get("cells") or []
                     if not (item.get("kind") == "estimate"
                             and str(item["period"]["end"]) in forecast_ends)]
            cells.extend(dict(item) for item in fresh.get("cells") or [])
            reanchored.append(_finish({
                **dict(result),
                "cells": sorted(cells, key=lambda item: (
                    str(item["period"]["end"]),
                    0 if item["kind"] == "estimate" else 1,
                )),
            }))
        results = reanchored
    if evidence_refs is None:
        evidence_refs = filing_refs(drivers, ends)
    body = {
        SOURCE_VERSION_KEY: str(prior["id"]),
        **{key: value for key, value in prior.items()
           if key in (_RECORD_FIELDS | _STRUCTURE_RECORD_FIELDS
                      | _CASH_COMPANION_RECORD_FIELDS)
           and key not in _BODY_EXCLUDED},
        "history_periods": [str(item) for item in (table.get("periods") or [])],
        "realised_periods": keep,
        "forecast_periods": forecast,
        "inputs_hash": content_hash(json.loads(canonical_json(table))),
        "formula_hash": (
            cash_flow_formula_hash(structure, binding, current_companion)
            if current_companion is not None else
            structure_formula_hash(structure, binding)
        ),
        "drivers": drivers,
        "assumptions": sorted(assumptions, key=lambda item: (
            item["driver_ref"], item["period"]["end"], item["kind"])),
        "results": results,
        "financial_statement_structure": dict(structure),
        "financial_statement_structure_replay": dict(replay),
        "forecast_structure_binding": dict(binding),
        **({"cash_flow_companion": current_companion}
           if current_companion is not None else {}),
        "actor_ref": str(actor_ref or prior["actor_ref"]),
        "change_reason": "filing_actual",
        "evidence_refs": [dict(item) for item in evidence_refs],
        "decision": None,
        "mission_version_ref": prior.get("mission_version_ref"),
    }
    if source_accuracy is not None:
        import copy
        body["execution_contract"] = copy.deepcopy(prior.get("execution_contract") or {})
        body["execution_contract"]["source_accuracy"] = copy.deepcopy(source_accuracy)
    return _roll_actualized_forecast(body)


def actualize_model(
    prior: Mapping[str, Any],
    table: Mapping[str, Any],
    *,
    structure: Mapping[str, Any] | None = None,
    replay: Mapping[str, Any] | None = None,
    binding: Mapping[str, Any] | None = None,
    evidence_refs: Sequence[Mapping[str, Any]] | None = None,
    actor_ref: str | None = None,
    source_accuracy: Mapping[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Write the filed answer beside the estimate, and change nothing else.

    The one near-mechanical version in this layer. A quarter this model
    forecast has been filed; the estimate stays exactly where it is, marked
    ``superseded_by`` the actual that answered it, and the actual arrives with
    the accession it came from. A structured model replays later formula cells
    from that actual using the same recorded assumptions and company DAG. It
    does not choose a new assumption or interpret what the print means.

    Returns ``None`` when no forecast quarter has been filed yet.
    """

    if is_structured_schema(prior.get("schema_version")):
        return _actualize_structured_model(
            prior, table, structure=structure, replay=replay, binding=binding,
            evidence_refs=evidence_refs, actor_ref=actor_ref, source_accuracy=source_accuracy)

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
    return _roll_actualized_forecast(body)


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
            work_order_ref=change.get("work_order_ref"),
            outside_band=change.get("outside_band"))

    forecast = [periods_by_end[end] for end in sorted(periods_by_end)]
    drivers = prior.get("drivers") or []
    live = [item for item in assumptions
            if str(item["period"]["end"]) in periods_by_end]
    if is_structured_schema(prior.get("schema_version")):
        structure = prior.get("financial_statement_structure")
        if not isinstance(structure, Mapping):
            raise ForecastModelValidationError(
                "structured model carries no statement authority")
        income_drivers = [item for item in drivers if item.get("kind") != "cash_flow"]
        if _is_coverage(structure):
            recomputed = compute_structured_model_results(
                drivers, live, forecast, structure,
                validate_cash_flow_companion(prior.get("cash_flow_companion"))
                if prior.get("schema_version") == STRUCTURED_CASH_SCHEMA_VERSION
                else None)
        else:
            income_results = compute_structure_results(
                income_drivers, live, forecast, structure)
            recomputed = list(income_results)
            if prior.get("schema_version") == STRUCTURED_CASH_SCHEMA_VERSION:
                companion = validate_cash_flow_companion(prior.get("cash_flow_companion"))
                cash_drivers = [item for item in drivers if item.get("kind") == "cash_flow"]
                recomputed.extend(compute_cash_flow_companion_results(
                    cash_drivers, live, forecast, companion, income_results))
    else:
        recomputed = compute_results(
            drivers, live, forecast, statements=prior.get("statements") or {},
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

    body = {
        SOURCE_VERSION_KEY: str(prior["id"]),
        **{key: value for key, value in prior.items()
           if key in (_RECORD_FIELDS | _STRUCTURE_RECORD_FIELDS
                      | _CASH_COMPANION_RECORD_FIELDS)
           and key not in _BODY_EXCLUDED},
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
    if prior.get("execution_contract"):
        from .forecast_financial_execution import execute_financial_model
        contract = prior["execution_contract"]
        execute_financial_model(body, {**contract["specification"], "fcf_definition": contract.get("fcf_definition")})
    return body


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


def _normalize_driver(value: Any, name: str, *, schema_version: str) -> dict[str, Any]:
    cell_optional = (_CELL_OPTIONAL_FIELDS
                     if schema_version in {SCHEMA_VERSION, *STRUCTURED_SCHEMA_VERSIONS}
                     else frozenset())
    fields = (_DRIVER_FIELDS | _STRUCTURE_DRIVER_FIELDS
              if is_structured_schema(schema_version) else _DRIVER_FIELDS)
    wire = _closed(value, fields, name, optional=_DRIVER_OPTIONAL_FIELDS)
    if wire.get("statement_identity") is not None:
        from .company_model_inputs import validate_statement_identity
        wire["statement_identity"]=validate_statement_identity(wire["statement_identity"])
    wire["ref"] = _text(wire["ref"], f"{name}.ref")
    driver_kinds = (
        (*DRIVER_KINDS, "statement_line")
        if is_structured_schema(schema_version) else
        DRIVER_KINDS if schema_version == SCHEMA_VERSION else ("revenue", "expense")
    )
    wire["kind"] = _one_of(wire["kind"], driver_kinds, f"{name}.kind")
    wire["label"] = _text(wire["label"], f"{name}.label")
    wire["concept"] = _optional_text(wire["concept"], f"{name}.concept")
    wire["statement"] = _optional_text(wire["statement"], f"{name}.statement")
    wire["unit"] = _optional_text(wire["unit"], f"{name}.unit")
    wire["status"] = _one_of(wire["status"], DRIVER_STATUSES, f"{name}.status")
    if wire["role"] is not None:
        allowed_roles = ROLES
        if is_structured_schema(schema_version):
            from .company_financial_statement_structure import (
                COVERAGE_ROLES as statement_roles,
            )
            allowed_roles = (
                (*statement_roles, OPERATING_CASH_FLOW, CAPITAL_EXPENDITURE)
                if schema_version == STRUCTURED_CASH_SCHEMA_VERSION else statement_roles
            )
        wire["role"] = _one_of(wire["role"], allowed_roles, f"{name}.role")
    wire["note"] = _optional_text(wire["note"], f"{name}.note")
    if is_structured_schema(schema_version):
        wire["structure_line_ref"] = _text(
            wire["structure_line_ref"], f"{name}.structure_line_ref")
        wire["forecast_method"] = _one_of(
            wire["forecast_method"],
            ("quarterly_growth", "year_over_year_growth", "seasonal_factor", "share_of_line", "unavailable",
             "carry_forward_mean", "zero_one_off", "rate_times_balance",
             "hold_last", "roll_forward"),
            f"{name}.forecast_method")
        wire["forecast_base_ref"] = _optional_text(
            wire["forecast_base_ref"], f"{name}.forecast_base_ref")
        if ((wire["forecast_method"] in {"share_of_line", "rate_times_balance"} and wire["forecast_base_ref"] is None)
                or (wire["forecast_method"] not in {"share_of_line", "rate_times_balance", "roll_forward"} and wire["forecast_base_ref"] is not None)):
            raise ForecastModelValidationError(
                f"{name}.forecast_base_ref belongs only to share_of_line "
                "or rate_times_balance")
    if not isinstance(wire["spec_rows"], list) or not wire["spec_rows"]:
        raise ForecastModelValidationError(f"{name}.spec_rows must name a specification row")
    wire["spec_rows"] = [_text(item, f"{name}.spec_rows[]") for item in wire["spec_rows"]]
    if "cost_driver_slots" in wire:
        slots = wire["cost_driver_slots"]
        if not isinstance(slots, list) or not slots:
            raise ForecastModelValidationError(
                f"{name}.cost_driver_slots must be a non-empty list")
        normalized_slots = [
            _text(item, f"{name}.cost_driver_slots[{index}]")
            for index, item in enumerate(slots)
        ]
        if len(set(normalized_slots)) != len(normalized_slots):
            raise ForecastModelValidationError(
                f"{name}.cost_driver_slots must be unique")
        known_slots = {
            str(slot["slot_id"])
            for template in COST_DRIVER_TEMPLATES["templates"]
            for slot in template["cost_slots"]
        }
        unknown_slots = sorted(set(normalized_slots) - known_slots)
        if unknown_slots:
            raise ForecastModelValidationError(
                f"{name}.cost_driver_slots contains unknown slots {unknown_slots}")
        wire["cost_driver_slots"] = normalized_slots
    if not isinstance(wire["history"], list):
        raise ForecastModelValidationError(f"{name}.history must be a list")
    cells = []
    for index, cell in enumerate(wire["history"]):
        item = _closed(cell, _CELL_FIELDS, f"{name}.history[{index}]",
                       optional=cell_optional)
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
        if "source_forms" in item:
            forms = item["source_forms"]
            if not isinstance(forms, list):
                raise ForecastModelValidationError(
                    f"{name}.history[{index}].source_forms must contain 10-Q/10-K")
            normalized_forms = [
                _one_of(form, ("10-Q", "10-K", "10-Q/A", "10-K/A"),
                        f"{name}.history[{index}].source_forms[{form_index}]")
                for form_index, form in enumerate(forms)
            ]
            if len(set(normalized_forms)) != len(normalized_forms):
                raise ForecastModelValidationError(
                    f"{name}.history[{index}].source_forms must be unique")
            item["source_forms"] = normalized_forms
        if "derived_from" in item:
            operands = item["derived_from"]
            if not isinstance(operands, list) or len(operands) != 2:
                raise ForecastModelValidationError(
                    f"{name}.history[{index}].derived_from must contain two operands")
            normalized_operands = []
            for operand_index, operand in enumerate(operands):
                operand_name = (
                    f"{name}.history[{index}].derived_from[{operand_index}]")
                operand = _closed(operand, _DERIVED_OPERAND_FIELDS, operand_name)
                normalized_operands.append({
                    "period_start": _iso_date(operand.get("period_start"),
                                              f"{operand_name}.period_start"),
                    "period_end": _iso_date(operand.get("period_end"),
                                            f"{operand_name}.period_end"),
                    "value": format(_decimal(operand.get("value"),
                                             f"{operand_name}.value"), "f"),
                    "unit": (
                        _text(operand.get("unit"), f"{operand_name}.unit").casefold()
                        if (schema_version == STRUCTURED_CASH_SCHEMA_VERSION
                            and wire["kind"] == "cash_flow")
                        else _text(operand.get("unit"), f"{operand_name}.unit")
                    ),
                    "accession": _text(operand.get("accession"),
                                       f"{operand_name}.accession"),
                    "form": _one_of(operand.get("form"), ("10-Q", "10-K", "10-Q/A", "10-K/A"),
                                    f"{operand_name}.form"),
                })
            item["derived_from"] = normalized_operands
            derived = item["derived_from"]
            if ({operand["accession"] for operand in derived} != set(item["accessions"])
                    or {operand["form"] for operand in derived}
                    != set(item.get("source_forms") or [])):
                raise ForecastModelValidationError(
                    f"{name}.history[{index}] derived operands do not match its sources")
            if (derived[0]["unit"] != derived[1]["unit"]
                    or derived[0]["unit"] != wire["unit"]
                    or _decimal(derived[1]["value"], "derived value")
                    - _decimal(derived[0]["value"], "derived value")
                    != _decimal(item["value"], "derived quarter value")):
                raise ForecastModelValidationError(
                    f"{name}.history[{index}] derived arithmetic does not replay")
            try:
                period_chain_is_exact = (
                    derived[0]["period_start"] == derived[1]["period_start"]
                    and date.fromisoformat(derived[0]["period_end"])
                    + timedelta(days=1)
                    == date.fromisoformat(item["period_start"])
                    and derived[1]["period_end"] == item["period_end"]
                )
            except (TypeError, ValueError):
                period_chain_is_exact = False
            if not period_chain_is_exact:
                raise ForecastModelValidationError(
                    f"{name}.history[{index}] derived periods do not replay")
        if wire["kind"] == "cash_flow" and item["basis"] == "derived_from_cumulative":
            if "derived_from" not in item:
                raise ForecastModelValidationError(
                    f"{name}.history[{index}] derived cell must bind its operands")
        elif wire["kind"] == "cash_flow" and "derived_from" in item:
            raise ForecastModelValidationError(
                f"{name}.history[{index}] reported cell cannot carry derived operands")
        if wire["kind"] == "cash_flow" and item["basis"] not in (
                "reported", "derived_from_cumulative"):
            raise ForecastModelValidationError(
                f"{name}.history[{index}].basis is not a filed cash-flow basis")
        if wire["kind"] == "cash_flow" and not item.get("source_forms"):
            raise ForecastModelValidationError(
                f"{name}.history[{index}] cash flow cell must bind source forms")
        cells.append(item)
    if wire["concept"] is None and cells:
        raise ForecastModelValidationError(
            f"{name} has no filed concept and cannot carry filed history")
    wire["history"] = cells
    return wire


def _normalize_ref(value: Any, name: str) -> dict[str, Any]:
    wire = _closed(value, _REF_FIELDS, name, optional=_REF_OPTIONAL_FIELDS)
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
    if wire["kind"] == "doc_passage":
        from .evidence_index import parse_passage_ref, PassageRefInvalid
        try: parse_passage_ref(wire["ref"])
        except PassageRefInvalid as exc:
            raise ForecastModelValidationError(f"{name} must name an exact doc-passage reference") from exc
    if wire["kind"] in PROXY_GAP_REQUIRED:
        if wire["ref"] is None:
            raise ForecastModelValidationError(
                f"{name} is a market proxy and must name the series it cites")
        gap = wire.get("proxy_gap")
        if not isinstance(gap, str) or not gap.strip():
            # Refused, never repaired: a proxy whose distance from the company
            # is unstated is indistinguishable from a figure the company filed,
            # and a default sentence written here would be this module
            # inventing the one judgement the field exists to carry.
            raise ForecastModelValidationError(
                f"{name} cites a market proxy and does not say how far it sits "
                "from what this company realises (proxy_gap)")
        gap = gap.strip()
        if len(gap) > MAX_PROXY_GAP_CHARS:
            raise ForecastModelValidationError(
                f"{name}.proxy_gap is longer than {MAX_PROXY_GAP_CHARS} characters")
        wire["proxy_gap"] = gap
    elif "proxy_gap" in wire:
        raise ForecastModelValidationError(
            f"{name} is not a market proxy and cannot carry a proxy_gap")
    return wire


def _normalize_assumption(value: Any, name: str, *, driver_refs: set[str]) -> dict[str, Any]:
    # ``outside_band`` arrived after the first models were written and absence
    # means "nothing was claimed", so a record from before it existed reads
    # back unchanged rather than being refused for a field it could not have
    # carried.
    value = {"outside_band": None, **dict(value)} if isinstance(value, Mapping) else value
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
        (item["kind"] == "input_cell" or (wire["measure"] == "reported_actual" and item["kind"] == "filing")) and item["accession"] for item in wire["refs"]
    ):
        raise ForecastModelValidationError(
            f"{name} is an actual and must cite the filing it came from")
    wire["superseded_by"] = _optional_text(wire["superseded_by"], f"{name}.superseded_by")
    if wire["superseded_by"] is not None and wire["kind"] == "actual":
        raise ForecastModelValidationError(
            f"{name} is an actual and cannot have been superseded")
    wire["outside_band"] = _normalize_outside_band(
        wire["outside_band"], f"{name}.outside_band")
    wire["provenance"] = provenance
    return wire


def _normalize_outside_band(value: Any, name: str) -> dict[str, str] | None:
    """The flag, or nothing. A flag with no sentence behind it is not a flag."""

    if value is None:
        return None
    wire = _closed(value, _OUTSIDE_BAND_FIELDS, name)
    reason = _text(wire["reason"], f"{name}.reason")
    if len(reason.split()) < 3:
        raise ForecastModelValidationError(
            f"{name}.reason must be a sentence saying why this company is "
            "expected to leave the range it has filed")
    return {"reason": reason}


def _normalize_result(value: Any, name: str, *, assumption_refs: set[str],
                      driver_refs: set[str], schema_version: str = SCHEMA_VERSION) -> dict[str, Any]:
    wire = _closed(value, _RESULT_FIELDS, name)
    wire["ref"] = _text(wire["ref"], f"{name}.ref")
    wire["driver_ref"] = _optional_text(wire["driver_ref"], f"{name}.driver_ref")
    if wire["driver_ref"] is not None and wire["driver_ref"] not in driver_refs:
        raise ForecastModelValidationError(
            f"{name}.driver_ref names no driver of this model")
    if wire["role"] is not None:
        allowed_roles = ROLES
        if is_structured_schema(schema_version):
            from .company_financial_statement_structure import (
                COVERAGE_ROLES as statement_roles,
            )
            allowed_roles = (
                (*statement_roles, OPERATING_CASH_FLOW, CAPITAL_EXPENDITURE)
                if schema_version == STRUCTURED_CASH_SCHEMA_VERSION else statement_roles
            )
        wire["role"] = _one_of(wire["role"], allowed_roles, f"{name}.role")
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

    version = value.get("schema_version") if isinstance(value, Mapping) else None
    fields = (
        _RECORD_FIELDS | _STRUCTURE_RECORD_FIELDS
        | (_CASH_COMPANION_RECORD_FIELDS
           if version == STRUCTURED_CASH_SCHEMA_VERSION else frozenset())
        if is_structured_schema(version) else _RECORD_FIELDS
    )
    wire = _closed(value, fields - {"forecast_policy", "execution_contract"}, "forecast model", optional={"forecast_policy", "execution_contract"})
    if "forecast_policy" in wire:
        policy = wire["forecast_policy"]
        if not isinstance(policy, Mapping) or set(policy) != {"rule_ref", "latest_actual_end", "fiscal_year_end", "preliminary"}:
            raise ForecastModelValidationError("forecast_policy must carry its closed reporting/calendar authority")
        if policy["rule_ref"] != "rule:rolling-forecast-horizon:1":
            raise ForecastModelValidationError("forecast policy rule is unsupported")
        _iso_date(policy["latest_actual_end"], "forecast policy latest actual")
        if policy["fiscal_year_end"] is not None:
            _iso_date(policy["fiscal_year_end"], "forecast policy fiscal year end")
        preliminary = policy["preliminary"]
        if preliminary is not None:
            if not isinstance(preliminary, Mapping) or not preliminary.get("accession") or not preliminary.get("values"):
                raise ForecastModelValidationError("preliminary forecast anchor needs filed accession and values")
            _iso_date(preliminary.get("period_end"), "preliminary actual period")

    if wire["schema_version"] not in (
        LEGACY_SCHEMA_VERSION, SCHEMA_VERSION, *STRUCTURED_SCHEMA_VERSIONS,
    ):
        raise ForecastModelValidationError("forecast model schema_version is unsupported")
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
    if is_structured_schema(wire["schema_version"]):
        structure = wire["financial_statement_structure"]
        replay = wire["financial_statement_structure_replay"]
        binding = wire["forecast_structure_binding"]
        if not all(isinstance(item, Mapping) for item in (structure, replay, binding)):
            raise ForecastModelValidationError(
                "structured forecast model needs statement authority objects")
        if (
            structure.get("content_hash") != binding.get("structure_hash")
            or replay.get("structure_hash") != structure.get("content_hash")
            or content_hash(replay) != binding.get("historical_replay_hash")
            or structure.get("company_ref") != wire["company_ref"]
            or binding.get("company_ref") != wire["company_ref"]
            or structure.get("spec_ref") != wire["spec_ref"]
            or binding.get("spec_ref") != wire["spec_ref"]
            or structure.get("spec_hash") != wire["spec_hash"]
            or binding.get("spec_hash") != wire["spec_hash"]
        ):
            raise ForecastModelValidationError(
                "structured forecast model statement authority differs")
        for item, name in ((structure, "financial_statement_structure"),
                           (binding, "forecast_structure_binding")):
            base = dict(item)
            asserted = base.pop("content_hash", None)
            if not isinstance(asserted, str) or content_hash(base) != asserted:
                raise ForecastModelValidationError(f"{name} content hash is invalid")
        expected_formula_hash = (
            cash_flow_formula_hash(
                structure, binding,
                validate_cash_flow_companion(wire["cash_flow_companion"]),
            )
            if wire["schema_version"] == STRUCTURED_CASH_SCHEMA_VERSION else
            structure_formula_hash(structure, binding)
        )
        if (wire["formula_ref"] != STRUCTURE_FORMULA_REF
                or wire["formula_hash"] != expected_formula_hash):
            raise ForecastModelValidationError(
                "structured forecast model must bind its exact statement formula")
        if wire["schema_version"] == STRUCTURED_CASH_SCHEMA_VERSION:
            companion = validate_cash_flow_companion(wire["cash_flow_companion"])
            if (
                companion["company_ref"] != wire["company_ref"]
                or companion["spec_ref"] != wire["spec_ref"]
                or companion["spec_hash"] != wire["spec_hash"]
                or companion["income_structure_hash"] != structure.get("content_hash")
            ):
                raise ForecastModelValidationError(
                    "cash-flow companion belongs to another model authority")
            result_refs = _structure_result_refs(structure)
            structure_lines = {
                str(item.get("ref")): item for item in structure.get("lines") or []
            }
            for line in companion["lines"]:
                if line["forecast_method"] == "share_of_line" and (
                    result_refs.get(str(line["forecast_base_ref"]))
                    != line["forecast_base_result_ref"]
                    or str((structure_lines.get(str(line["forecast_base_ref"])) or {}).get(
                        "unit") or "").casefold() != companion["reporting_unit"]
                ):
                    raise ForecastModelValidationError(
                        "cash-flow companion base result differs from income structure")
    elif wire["formula_ref"] != FORMULA_REF or wire["formula_hash"] != FORMULA_HASH:
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
    drivers = [_normalize_driver(item, f"drivers[{index}]",
                                 schema_version=wire["schema_version"])
               for index, item in enumerate(wire["drivers"])]
    driver_refs = {item["ref"] for item in drivers}
    if len(driver_refs) != len(drivers):
        raise ForecastModelValidationError("driver refs must be unique")
    wire["drivers"] = drivers
    if (wire.get("cash_flow_companion") or {}).get("schema_version") == "cash-flow-companion-0.2":
        from .cash_flow_filed_tie_base import validate as validate_filed_tie, drivers as filed_tie_drivers
        validate_filed_tie(wire["cash_flow_companion"], wire["financial_statement_structure"], drivers)
        for expected in filed_tie_drivers(wire["cash_flow_companion"]):
            if [d for d in drivers if d["ref"] == expected["ref"]] != [expected]:
                raise ForecastModelValidationError("cash filed historical driver differs from source basis")

    if not isinstance(wire["assumptions"], list):
        raise ForecastModelValidationError("assumptions must be a list")
    assumptions = [
        _normalize_assumption(item, f"assumptions[{index}]", driver_refs=driver_refs)
        for index, item in enumerate(wire["assumptions"])]
    from .cash_flow_filed_tie_base import validate_assumptions as validate_filed_cash_assumptions
    validate_filed_cash_assumptions(wire.get("cash_flow_companion") or {}, assumptions)
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
                          driver_refs=driver_refs, schema_version=wire["schema_version"])
        for index, item in enumerate(wire["results"])]
    result_refs = {item["ref"] for item in wire["results"]}
    if len(result_refs) != len(wire["results"]):
        raise ForecastModelValidationError("result refs must be unique")
    if wire["schema_version"] == STRUCTURED_CASH_SCHEMA_VERSION:
        required_cash_results = {
            "result:operating_cash_flow", "result:capital_expenditure",
            "result:free_cash_flow",
        }
        if not required_cash_results.issubset(result_refs):
            raise ForecastModelValidationError(
                "cash-flow companion model is missing its result lines")
        companion = wire["cash_flow_companion"]
        expected_cash_drivers = {
            (line["role"], line["concept"])
            for line in companion["lines"]
            if line["status"] in {"filed", "incomplete"}
        }
        actual_cash_drivers = {
            (item["role"], item["concept"])
            for item in wire["drivers"] if item["kind"] == "cash_flow"
        }
        if actual_cash_drivers != expected_cash_drivers:
            raise ForecastModelValidationError(
                "cash-flow companion drivers differ from its selected sources")
        expected_driver_rows = build_cash_flow_companion_drivers(
            {"cash_flow_inputs": [line["source"] for line in companion["lines"]
                                  if line["source"] is not None]},
            companion,
        )
        held_driver_rows = [item for item in wire["drivers"]
                            if item["kind"] == "cash_flow"]
        if held_driver_rows != expected_driver_rows:
            raise ForecastModelValidationError(
                "cash-flow companion driver history differs from its source bytes")

    body = {key: value for key, value in wire.items() if key not in _BODY_EXCLUDED}
    expected_body = content_hash(body)
    if _sha256(wire["body_hash"], "body_hash") != expected_body:
        raise ForecastModelValidationError("forecast model body_hash is invalid")
    from .forecast_financial_execution import validate_execution_contract
    validate_execution_contract(wire)
    expected = _sha256(wire["content_hash"], "content_hash")
    base = {key: value for key, value in wire.items() if key != "content_hash"}
    if content_hash(base) != expected:
        raise ForecastModelValidationError("forecast model content_hash is invalid")
    return wire


def _statement_line_proof_row(row: Mapping[str, Any]) -> dict[str, Any]:
    wire = dict(row)
    dimensions = wire.pop("dimensions_json", None)
    if dimensions is not None and "dimensions" not in wire:
        wire["dimensions"] = json.loads(dimensions) if isinstance(dimensions, str) else dimensions
    if wire.get("dimensions") is None:
        wire.pop("dimensions", None)
    wire["is_breakdown"] = bool(wire.get("is_breakdown")) or wire.get(
        "dimension_axis") is not None
    return wire


def body_hash(body: Mapping[str, Any]) -> str:
    """What makes two models the same model: everything but when and who asked."""

    version = body.get("schema_version")
    fields = (
        _RECORD_FIELDS | _STRUCTURE_RECORD_FIELDS
        | (_CASH_COMPANION_RECORD_FIELDS
           if version == STRUCTURED_CASH_SCHEMA_VERSION else frozenset())
        if is_structured_schema(version) else _RECORD_FIELDS
    )
    return content_hash({key: value for key, value in body.items()
                         if key not in _BODY_EXCLUDED and key in fields})


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

    def publish(
        self,
        body: Mapping[str, Any],
        *,
        statement_rows: Sequence[Mapping[str, Any]] = (),
        solver_results: Sequence[Mapping[str, Any]] = (),
        source_groups: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
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
        # A pure generator has no database calendar. Bind a missing fiscal
        # year end here to the admitted annual authority before hashing.
        policy = body.get("forecast_policy")
        if isinstance(policy, Mapping) and policy.get("fiscal_year_end") is None and self.connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='coverage_mission_statement_filings'").fetchone():
            annual_end = self.connection.execute(
                "SELECT MAX(report_date) FROM coverage_mission_statement_filings WHERE company_ref=? AND form IN ('10-K','10-K/A')", (company_ref,)).fetchone()[0]
            if annual_end:
                body["forecast_policy"] = {**policy, "fiscal_year_end": annual_end}
        digest = body_hash(body)
        latest = self.connection.execute(
            "SELECT * FROM forecast_model_versions WHERE model_ref=? "
            "ORDER BY version_number DESC LIMIT 1", (model_ref,),
        ).fetchone()
        duplicate_existing = latest is not None and latest["body_hash"] == digest
        if duplicate_existing:
            existing_proof = self.connection.execute(
                "SELECT 1 FROM forecast_model_filing_proofs WHERE model_version_id=?",
                (latest["version_id"],),
            ).fetchone()
            # Legacy callers that are only checking model idempotence do not
            # owe filing evidence.  Backfill is entered only by a caller that
            # supplies the exact statement rows needed to construct a proof.
            if existing_proof is not None or not statement_rows:
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
        from .forecast_source_accuracy import validate_originals
        validate_originals(self.connection, wire)
        if not duplicate_existing:
            validate_new_forecast_rules(wire)
            from .forecast_financial_execution import validate_execution_contract, validate_assumption_sources, validate_stored_execution_spec
            validate_execution_contract(wire, publishing=True)
            validate_assumption_sources(wire, connection=self.connection)
            validate_stored_execution_spec(self.connection, wire)
            policy = wire.get("forecast_policy") or forecast_policy(revenue_anchor(wire["drivers"]))
            known_actual = max(wire["history_periods"] + [str(c["period_end"]) for d in wire["drivers"] for c in quarterly_history(d)])
            if self.connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='coverage_mission_statement_filings'").fetchone():
                latest_filed = self.connection.execute(
                    "SELECT MAX(report_date) FROM coverage_mission_statement_filings WHERE company_ref=? AND form IN ('10-Q','10-K','10-Q/A','10-K/A')", (company_ref,)).fetchone()[0]
                if latest_filed:
                    known_actual = max(known_actual, latest_filed)
                latest_annual = self.connection.execute(
                    "SELECT MAX(report_date) FROM coverage_mission_statement_filings WHERE company_ref=? AND form IN ('10-K','10-K/A')", (company_ref,)).fetchone()[0]
                if policy.get("fiscal_year_end") != latest_annual:
                    raise ForecastModelValidationError("forecast fiscal year end must bind the latest annual filing")
            from .forecast_assumption_sources import preliminary_quarter
            preliminary = preliminary_quarter(self.connection, company_ref, reporting_unit=str(wire["unit"]).casefold())
            if preliminary:
                known_actual = max(known_actual, preliminary["period_end"])
            if policy["latest_actual_end"] != known_actual:
                raise ForecastModelValidationError("forecast boundary must equal the latest admitted actual; arbitrary future dates are refused")
        if duplicate_existing:
            # Preserve the immutable legacy model byte for byte.  Only the
            # separately stored, replayable filing proof is being backfilled.
            wire = self.model(str(latest["version_id"]))
            version_id = wire["id"]
        # P17b. The structural validation above says the record is well formed;
        # this says the company it describes could exist. Chem's workbook
        # passed forty-two of the first kind and shipped a loss scenario as a
        # zero percent return. A failure here is the whole version refused and
        # recorded ``unavailable`` with its reasons -- never a repaired cell,
        # because the arithmetic that produced the impossible line produced
        # every other line too.
        from .economic_invariants import evaluate_forecast_model, gate

        evaluate_bound = evaluate_forecast_model
        if source_groups is not None:
            from .forecast_consumed_source_groups import select_from_authority, evaluate as evaluate_grouped
            expected_rows, expected_groups = select_from_authority(wire, self.connection)
            if (dict(source_groups) != expected_groups
                    or sorted((_statement_line_proof_row(r) for r in statement_rows), key=str)
                       != sorted((_statement_line_proof_row(r) for r in expected_rows), key=str)):
                raise ForecastModelValidationError("forecast consumed source selection differs from authority")
            source_groups = expected_groups
            evaluate_bound = evaluate_grouped
        from .forecast_statement_scope import statement_scope
        scoped_rows, model_statement_scope = statement_scope(
            wire, statement_rows, self.connection, source_groups=source_groups)
        if model_statement_scope is not None:
            from .forecast_execution_statement_scope import RULE_REF as EXECUTION_SCOPE_RULE, evaluate_execution
            if model_statement_scope.get("rule_ref") == EXECUTION_SCOPE_RULE:
                evaluate_bound = lambda record, **kwargs: evaluate_execution(
                    record, scope=model_statement_scope, **kwargs)
        candidate_report = evaluate_bound(
            wire, statement_rows=scoped_rows, solver_results=solver_results)
        gate(self.store, candidate_report,
             mission_version_ref=wire.get("mission_version_ref"))
        statement_wire = sorted(
            [_statement_line_proof_row(row) for row in statement_rows],
            key=lambda row: (str(row.get("ingest_id") or ""),
                             int(row.get("ordinal") or 0),
                             str(row.get("concept") or "")),
        )
        context_bindings = {}
        proof_rows_are_typed = bool(statement_wire) and all(
            row.get("ingest_id") is not None for row in statement_wire)
        if proof_rows_are_typed:
            line_refs = [str(row.get("line_id") or "") for row in statement_wire]
            if not all(line_refs) or len(set(line_refs)) != len(line_refs):
                raise ForecastModelValidationError(
                    "filing proof statement rows need unique line_id values")
            from .statement_context import resolve_line_refs, StatementContextError
            context_bindings = {}
            for ingest in {str(row["ingest_id"]) for row in statement_wire}:
                contexts = {row.get("statement_context_ref") for row in statement_wire if str(row["ingest_id"]) == ingest}
                if len(contexts) != 1:
                    raise ForecastModelValidationError("filing proof mixes statement context versions for one ingest")
                selected = next(iter(contexts))
                if selected is not None:
                    context_bindings[ingest] = selected
            try:
                stored_lines = resolve_line_refs(self.connection, line_refs, context_bindings)
            except StatementContextError as exc:
                raise ForecastModelValidationError(str(exc)) from exc
            authoritative = sorted(
                [_statement_line_proof_row(row) for row in stored_lines],
                key=lambda row: (str(row.get("ingest_id") or ""),
                                 int(row.get("ordinal") or 0),
                                 str(row.get("concept") or "")),
            )
            if len(authoritative) != len(statement_wire) or authoritative != statement_wire:
                raise ForecastModelValidationError(
                    "filing proof statement rows differ from immutable statement authority")
            statement_wire = authoritative
            ingest_ids = sorted({str(row["ingest_id"]) for row in statement_wire})
            placeholders = ",".join("?" for _ in ingest_ids)
            filing_rows = self.connection.execute(
                "SELECT ingest_id,company_ref,accession,form FROM "
                f"coverage_mission_statement_filings WHERE ingest_id IN ({placeholders})",
                ingest_ids,
            ).fetchall()
            found = {str(row["ingest_id"]): row for row in filing_rows}
            if set(found) != set(ingest_ids):
                missing = sorted(set(ingest_ids) - set(found))
                raise ForecastModelValidationError(
                    f"filing proof statement rows name unknown ingest ids: {missing}")
            wrong = sorted(ingest for ingest, row in found.items()
                           if row["company_ref"] != company_ref)
            if wrong:
                raise ForecastModelValidationError(
                    f"filing proof statement rows belong to another company: {wrong}")
            supplied_accessions = {str(row["accession"]) for row in found.values()}
            model_accessions = {
                str(accession)
                for driver in wire.get("drivers") or []
                for cell in driver.get("history") or []
                for accession in cell.get("accessions") or []
            }
            unknown_accessions = sorted(model_accessions - supplied_accessions)
            if unknown_accessions:
                raise ForecastModelValidationError(
                    "filing proof model history cites accessions outside its exact "
                    f"statement rows: {unknown_accessions}")
            for driver in wire.get("drivers") or []:
                for cell in driver.get("history") or []:
                    cell_accessions = set(map(str, cell.get("accessions") or []))
                    matches = [row for row in statement_wire
                               if str(found[str(row["ingest_id"])]["accession"])
                               in cell_accessions]
                    candidates = [
                        row for row in matches
                        if str(row.get("concept")) == str(cell.get("concept"))
                        and str(row.get("statement")) == str(driver.get("statement"))
                        and _filing_units_match(
                            row.get("unit"), driver.get("unit"),
                            structured=is_structured_schema(wire.get("schema_version")),
                        )
                        and ((row.get("dimensions")==driver["statement_identity"]["dimensions"]
                              and row.get("statement")==driver["statement_identity"]["statement"])
                             if driver.get("statement_identity") else
                             (row.get("dimension_axis") is None and row.get("dimension_member") is None and not row.get("dimensions")))
                    ]
                    if cell.get("basis") == "derived_from_cumulative":
                        if driver.get("kind") == "cash_flow":
                            operands = cell.get("derived_from") or []
                            operand_matches = []
                            for operand in operands:
                                operand_matches.append([
                                    row for row in candidates
                                    if row.get("period_start") == operand.get("period_start")
                                    and row.get("period_end") == operand.get("period_end")
                                    and str(row.get("value")) == str(operand.get("value"))
                                    and _filing_units_match(
                                        row.get("unit"), operand.get("unit"),
                                        structured=is_structured_schema(wire.get("schema_version")),
                                    )
                                    and str(found[str(row["ingest_id"])]["accession"])
                                    == str(operand.get("accession"))
                                    and str(found[str(row["ingest_id"])]["form"])
                                    == str(operand.get("form"))
                                ])
                            exact = (
                                len(operands) == 2
                                and all(len(items) == 1 for items in operand_matches)
                            )
                        else:
                            exact = _cumulative_source_pair_matches(
                                candidates, found, cell)
                    else:
                        exact = any(
                            row.get("period_start") == cell.get("period_start")
                            and row.get("period_end") == cell.get("period_end")
                            and str(row.get("value")) == str(cell.get("value"))
                            and cell_accessions == {
                                str(found[str(row["ingest_id"])]["accession"])}
                            and (driver.get("kind") != "cash_flow"
                                 or set(cell.get("source_forms") or []) == {
                                     str(found[str(row["ingest_id"])]["form"])} )
                            for row in candidates)
                    if not exact:
                        raise ForecastModelValidationError(
                            "filing reconciliation mismatch "
                            f"company={company_ref} metric={driver.get('ref')} "
                            f"period={cell.get('period_end')} value={cell.get('value')} "
                            f"accessions={cell.get('accessions')}")
            for method in (wire.get("financial_statement_structure_replay") or {}).get("forecast_methods") or []:
                source = method.get("annual_forecast_source")
                if source is None:
                    continue
                for observation in source.get("observations") or []:
                    matches = [row for row in statement_wire
                               if row.get("concept") == source.get("concept")
                               and not row.get("dimensions") and row.get("dimension_axis") is None
                               and row.get("period_start") == observation.get("period_start")
                               and row.get("period_end") == observation.get("period_end")
                               and _filing_units_match(row.get("unit"), observation.get("unit"), structured=True)
                               and _decimal(row.get("value"), "annual source value") == _decimal(observation.get("value"), "annual source value")
                               and str(found[str(row["ingest_id"])]["accession"]) in set(observation.get("source_accessions") or [])
                               and found[str(row["ingest_id"])]["form"] in {"10-K", "10-K/A"}]
                    if not matches:
                        raise ForecastModelValidationError("annual share forecast source does not match immutable direct annual filing rows")
        scoped_wire, model_statement_scope = statement_scope(
            wire, statement_wire, self.connection, source_groups=source_groups)
        invariant_report = evaluate_bound(
            wire, statement_rows=scoped_wire, solver_results=solver_results)
        gate(self.store, invariant_report,
             mission_version_ref=wire.get("mission_version_ref"))
        statement_rows_hash = content_hash({"statement_rows": statement_wire})
        filing_proof = None if not proof_rows_are_typed else {
            "schema_version": "0.1", "model_version_ref": wire["id"],
            "model_content_hash": wire["content_hash"],
            "company_ref": company_ref, "inputs_hash": wire["inputs_hash"],
            "statement_rows_hash": statement_rows_hash,
            "statement_line_refs": [str(row["line_id"]) for row in statement_wire],
            "statement_row_count": len(statement_wire),
            "solver_results": [dict(row) for row in solver_results],
            "invariant_report": invariant_report.as_dict(),
            "created_at": wire["created_at"],
        }
        if filing_proof is not None:
            if source_groups is not None:
                filing_proof["consumed_source_groups"] = dict(source_groups)
            if model_statement_scope is not None:
                filing_proof["model_statement_scope"] = model_statement_scope
            if context_bindings:
                filing_proof["statement_context_bindings"] = context_bindings
            filing_proof["content_hash"] = content_hash(filing_proof)
        with self._transaction() as cur:
            if not duplicate_existing:
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
            if filing_proof is not None:
                cur.execute(
                    "INSERT INTO forecast_model_filing_proofs"
                    "(model_version_id,company_ref,model_content_hash,inputs_hash,"
                    "statement_rows_hash,record_json,content_hash,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?)",
                    (wire["id"], company_ref, wire["content_hash"], wire["inputs_hash"],
                     statement_rows_hash, canonical_json(filing_proof),
                     filing_proof["content_hash"], wire["created_at"]),
                )
        # Read back through the same path a reader would take, so a record that
        # cannot be read is a failed write rather than a stored surprise.
        stored = self.model(version_id)
        if stored["content_hash"] != wire["content_hash"]:
            raise ForecastModelConflict("forecast model did not read back as written")
        return {**stored, "status": "duplicate" if duplicate_existing else "fresh"}

    def filing_proof(self, version_ref: str) -> dict[str, Any] | None:
        """The exact deterministic filing check stored with a model, if any."""
        return self._filing_proof(version_ref, require_replay=True)

    def filing_proof_recheck(self, version_ref: str) -> dict[str, Any] | None:
        """The stored filing check, re-evaluated now on its exact source rows.

        ADR-0011: the company_model gate asks whether a model's history
        reconciles to the filings *today*.  The stored proof answers that
        exactly while the invariant contract is unchanged; once the contract
        moves (a new report field, a re-defined subject hash) the stored
        report no longer replays byte-for-byte, although its hash-bound source
        rows are exactly the same.  This reader still refuses tampered or
        drifted source rows -- those raise as in ``filing_proof`` -- but on a
        replay difference it returns the stored proof with
        ``replays: False`` and the report recomputed under the current
        contract, so the gate can decide on the current evaluation instead of
        waiting forever for a model nobody will rebuild.
        """
        return self._filing_proof(version_ref, require_replay=False)

    def _filing_proof(self, version_ref: str, *,
                      require_replay: bool) -> dict[str, Any] | None:
        model = self.model(version_ref)
        row = self.connection.execute(
            "SELECT * FROM forecast_model_filing_proofs WHERE model_version_id=?",
            (version_ref,),
        ).fetchone()
        if row is None:
            return None
        try:
            wire = json.loads(row["record_json"])
        except (TypeError, json.JSONDecodeError) as exc:
            raise ForecastModelConflict("filing proof record_json is invalid") from exc
        base = dict(wire)
        asserted = base.pop("content_hash", None)
        if (
            canonical_json(wire) != row["record_json"]
            or asserted != content_hash(base)
            or asserted != row["content_hash"]
            or wire.get("model_version_ref") != row["model_version_id"]
            or wire.get("company_ref") != row["company_ref"]
            or wire.get("model_content_hash") != row["model_content_hash"]
            or wire.get("inputs_hash") != row["inputs_hash"]
            or wire.get("statement_rows_hash") != row["statement_rows_hash"]
            or wire.get("model_content_hash") != model["content_hash"]
            or wire.get("inputs_hash") != model["inputs_hash"]
            or (wire.get("invariant_report") or {}).get("output_ref") != model["id"]
            or (wire.get("invariant_report") or {}).get("company_ref") != model["company_ref"]
            or (wire.get("invariant_report") or {}).get("status") != "available"
        ):
            raise ForecastModelConflict("forecast model filing proof drifted")
        refs = wire.get("statement_line_refs") or []
        if not refs:
            raise ForecastModelConflict("forecast model filing proof names no statement rows")
        from .statement_context import resolve_line_refs, StatementContextError
        try:
            rows = resolve_line_refs(self.connection, refs, wire.get("statement_context_bindings"))
        except StatementContextError as exc:
            raise ForecastModelConflict(str(exc)) from exc
        statement_wire = sorted(
            [_statement_line_proof_row(row) for row in rows],
            key=lambda item: (str(item.get("ingest_id") or ""),
                              int(item.get("ordinal") or 0),
                              str(item.get("concept") or "")),
        )
        if ({str(row["line_id"]) for row in statement_wire} != set(refs)
                or content_hash({"statement_rows": statement_wire})
                != wire["statement_rows_hash"]):
            raise ForecastModelConflict("forecast model filing proof source rows drifted")
        from .forecast_source_accuracy import validate_originals
        validate_originals(self.connection, model)
        from .economic_invariants import evaluate_forecast_model
        if wire.get("model_statement_scope") is not None:
            from .forecast_statement_scope import statement_scope
            scoped_wire, scope_replay = statement_scope(
                model, statement_wire, self.connection, frozen_scope=wire["model_statement_scope"],
                source_groups=wire.get("consumed_source_groups"))
            if scope_replay != wire["model_statement_scope"]:
                raise ForecastModelConflict("forecast filing partition scope does not replay")
        else:
            scoped_wire = statement_wire
        evaluate_bound = evaluate_forecast_model
        if wire.get("consumed_source_groups") is not None:
            from .forecast_consumed_source_groups import replay_manifest, evaluate as evaluate_grouped
            try:
                replay_manifest(model, statement_wire, wire["consumed_source_groups"], self.connection)
            except ForecastModelValidationError as exc:
                raise ForecastModelConflict(str(exc)) from exc
            evaluate_bound = evaluate_grouped
        from .forecast_execution_statement_scope import RULE_REF as EXECUTION_SCOPE_RULE
        if (wire.get("model_statement_scope") or {}).get("rule_ref") == EXECUTION_SCOPE_RULE:
            from .forecast_execution_statement_scope import evaluate_execution
            evaluate_bound = lambda record, **kwargs: evaluate_execution(
                record, scope=scope_replay, **kwargs)
        replay = evaluate_bound(
            model, statement_rows=scoped_wire,
            solver_results=wire.get("solver_results") or [])
        current = replay.as_dict()
        replays = current == wire["invariant_report"]
        if require_replay:
            if not replays:
                raise ForecastModelConflict("forecast model filing proof does not replay")
            return wire
        return {**wire, "replays": replays, "current_invariant_report": current}

    def publish_annual_projection(
        self, projection: Mapping[str, Any], *, inputs: Mapping[str, Any],
        calendar_binding: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Persist one exact deterministic annual view beside its model version."""

        from .company_model_annual_projection import (
            validate_annual_projection, validate_stored_calendar_binding,
        )

        version_ref = _text(projection.get("model_version_ref"), "model_version_ref")
        model = self.model(version_ref)
        try:
            exact_calendar = validate_stored_calendar_binding(
                self.connection, calendar_binding,
                company_ref=str(model["company_ref"]),
            )
            wire = validate_annual_projection(
                projection, model=model, inputs=inputs,
                calendar_binding=exact_calendar)
        except Exception as exc:
            raise ForecastModelValidationError(
                "annual model projection authority differs") from exc
        existing = self.connection.execute(
            "SELECT record_json FROM forecast_model_annual_projections "
            "WHERE model_version_id=?", (version_ref,),
        ).fetchone()
        if existing is not None:
            held = json.loads(existing["record_json"])
            if held != wire:
                raise ForecastModelConflict(
                    "this model version already has another annual projection")
            return {**self.annual_projection(version_ref), "status": "duplicate"}
        with self._transaction() as cur:
            cur.execute(
                "INSERT INTO forecast_model_annual_projections"
                "(model_version_id,company_ref,model_content_hash,inputs_hash,"
                "calendar_hash,record_json,content_hash,created_at) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (version_ref, model["company_ref"], model["content_hash"],
                 model["inputs_hash"], wire["calendar_binding"]["content_hash"],
                 canonical_json(wire), wire["content_hash"], model["created_at"]),
            )
        return {**self.annual_projection(version_ref), "status": "fresh"}

    def annual_projection(self, version_ref: str) -> dict[str, Any] | None:
        """Read one immutable annual projection and replay its stored bindings."""

        model = self.model(version_ref)
        row = self.connection.execute(
            "SELECT * FROM forecast_model_annual_projections WHERE model_version_id=?",
            (version_ref,),
        ).fetchone()
        if row is None:
            return None
        try:
            wire = json.loads(row["record_json"])
        except (TypeError, json.JSONDecodeError) as exc:
            raise ForecastModelConflict("annual projection record_json is invalid") from exc
        base = dict(wire)
        asserted = base.pop("content_hash", None)
        binding = wire.get("calendar_binding") or {}
        if (
            canonical_json(wire) != row["record_json"]
            or asserted != content_hash(base)
            or asserted != row["content_hash"]
            or wire.get("model_version_ref") != row["model_version_id"]
            or wire.get("company_ref") != row["company_ref"]
            or wire.get("model_version_hash") != row["model_content_hash"]
            or wire.get("inputs_hash") != row["inputs_hash"]
            or binding.get("content_hash") != row["calendar_hash"]
            or wire.get("model_version_ref") != model["id"]
            or wire.get("model_version_hash") != model["content_hash"]
            or wire.get("inputs_hash") != model["inputs_hash"]
            or wire.get("structure_hash")
                != (model.get("financial_statement_structure") or {}).get("content_hash")
            or wire.get("structure_replay_hash")
                != content_hash(model.get("financial_statement_structure_replay") or {})
            or wire.get("forecast_structure_binding_hash")
                != (model.get("forecast_structure_binding") or {}).get("content_hash")
        ):
            raise ForecastModelConflict("annual model projection authority drifted")
        try:
            from .company_model_annual_projection import (
                validate_projection_record, validate_stored_calendar_binding,
            )

            validate_projection_record(wire, model=model)
            validate_stored_calendar_binding(
                self.connection, wire["calendar_binding"],
                company_ref=str(model["company_ref"]),
            )
        except Exception as exc:
            raise ForecastModelConflict(
                "annual model projection source authority drifted") from exc
        return wire

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
    "MARKET_PROXY",
    "MAX_PROXY_GAP_CHARS",
    "MAX_REALISED_PERIODS",
    "PROXY_GAP_REQUIRED",
    "REF_KINDS",
    "actualize_model",
    "assumption_ref",
    "market_proxy_ref",
    "proxy_gaps",
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
    "forecast_periods", "forecast_policy", "validate_new_forecast_rules",
    "model_readiness",
    "quarterly_history",
    "revenue_anchor",
    "role_drivers",
    "trailing_growth",
    "trailing_share",
    "validate_forecast_model",
]
