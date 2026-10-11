"""Company-specific financial statement lines and replayable formula topology.

This is the authority boundary between filed presentation and forecast math.
It does not choose a structure.  It verifies that a proposed structure names
lines this company actually filed, cites held statement or note evidence, and
replays its equations against historical periods before a forecast may use it.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
import re
from typing import Any, Callable, Mapping, Sequence

from .company_model_series import (
    ANNUAL_MAX_DAYS,
    NINE_MONTH_MAX_DAYS,
    QUARTER_MAX_DAYS,
    QUARTER_MIN_DAYS,
)
from .store import content_hash


LEGACY_SCHEMA_VERSION = "0.1"
ANNUAL_SCHEMA_VERSION = "0.2"
TYPED_NOTE_SCHEMA_VERSION = "0.3"
DIRECT_GROSS_PRETAX_SCHEMA_VERSION = "0.4"
REVENUE_PRETAX_SCHEMA_VERSION = "0.5"
# 0.6: every reported line with enough history carries a forecast method, the
# cash and balance statements are open for leaf lines, and the code completes
# and repairs a drafted structure (``forecast_coverage``) before it binds.
COVERAGE_SCHEMA_VERSION = "0.6"
SCHEMA_VERSION = COVERAGE_SCHEMA_VERSION
STRUCTURE_AUTHORITY_REF = "company-financial-statement-structure:0.6"
NET_INCOME_SOURCE_CARRY_VALIDATION_REF = "filed-net-income-source-carry:0.1"
CASH_LEAF_DURATION_UNIT_VALIDATION_REF = "filed-cash-leaf-duration-unit-proof:0.1"
_STRUCTURE_AUTHORITY_REFS = {
    LEGACY_SCHEMA_VERSION: "company-financial-statement-structure:0.1",
    ANNUAL_SCHEMA_VERSION: "company-financial-statement-structure:0.2",
    TYPED_NOTE_SCHEMA_VERSION: "company-financial-statement-structure:0.3",
    DIRECT_GROSS_PRETAX_SCHEMA_VERSION: "company-financial-statement-structure:0.4",
    REVENUE_PRETAX_SCHEMA_VERSION: "company-financial-statement-structure:0.5",
    COVERAGE_SCHEMA_VERSION: STRUCTURE_AUTHORITY_REF,
}

STATEMENTS = ("income", "balance", "cash")
PERIOD_KINDS = ("duration", "instant")
LINE_KINDS = ("filed", "derived")
ANNUAL_SEMANTICS = (
    "sum_quarters", "direct_annual", "annual_ratio", "not_applicable",
)
ROLES = (
    "revenue", "cost_of_revenue", "gross_profit", "operating_expense",
    "operating_income", "nonoperating_income_expense", "interest_income",
    "interest_expense", "pretax_income", "income_tax_expense",
    "income_from_continuing_operations", "discontinued_operations",
    "net_income", "noncontrolling_interest", "parent_net_income",
    "preferred_dividends", "participating_securities_allocation",
    "dilutive_securities_adjustment", "diluted_eps_numerator",
    "diluted_weighted_average_shares", "diluted_eps",
    "other_operating_income_expense", "other_nonoperating_income_expense",
    "company_presented_component", "company_presented_subtotal",
)
# Cash-flow and balance-sheet leaf roles (structure 0.6). They are filed
# leaves forecast by a method; they are never terms of an income formula.
CASH_LEAF_ROLES = ("share_repurchase", "dividends_paid", "acquisitions",
                   "depreciation_amortization", "finance_lease_principal", "capitalized_software", "capitalized_contract_costs")
BALANCE_LEAF_ROLES = (
    "cash_and_equivalents", "debt_current", "debt_noncurrent",
    "accounts_receivable", "accounts_payable", "deferred_revenue", "depreciable_assets", "amortizable_assets",
)
STATEMENT_LEAF_ROLES = CASH_LEAF_ROLES + BALANCE_LEAF_ROLES
COVERAGE_ROLES = ROLES + STATEMENT_LEAF_ROLES
# 2026-10-09 (P0): roles only the deterministic coverage completion adds from
# the filings (``forecast_coverage.add_statement_leaves``); a materialized
# structure may carry them, the drafter's schema does not offer them, so the
# drafting task (and its hash) is unchanged.
COMPLETION_ONLY_ROLES = ("depreciation_amortization",)
DRAFTED_COVERAGE_ROLES = tuple(role for role in COVERAGE_ROLES
                               if role not in COMPLETION_ONLY_ROLES)
FORMULA_OPERATORS = ("sum", "divide")
LEGACY_FORECAST_METHODS = ("quarterly_growth", "share_of_line", "formula", "unavailable")
# 0.6 methods.  ``share_of_line`` is the ratio-to-line method (a share of
# revenue for a cost, a rate of pretax income for tax).  The rest:
# ``carry_forward_mean`` holds the trailing-four-quarter mean amount (flagged
# 延续); ``zero_one_off`` forecasts zero for a line the spec calls one-off;
# ``rate_times_balance`` is an interest rate times a balance-sheet line;
# ``hold_last`` carries the last filed balance; ``roll_forward`` is the cash
# balance rolled by the forecast cash flow.
COVERAGE_FORECAST_METHODS = (
    "carry_forward_mean", "zero_one_off", "rate_times_balance", "hold_last",
    "roll_forward", "year_over_year_growth", "seasonal_factor",
)
FORECAST_METHODS = LEGACY_FORECAST_METHODS + COVERAGE_FORECAST_METHODS
BASED_FORECAST_METHODS = ("share_of_line", "rate_times_balance")
ANNUAL_FORECAST_METHODS = ("day_weighted_quarters", "annual_growth_from_filed", "unavailable")
MAX_STRUCTURE_LINES = 48
MAX_STRUCTURE_FORMULAS = 32
_LEGACY_LINE_FIELDS = {
    "ref", "role", "label", "kind", "concept", "statement", "unit",
    "period_kind", "annual_semantics", "forecast_method", "forecast_base_ref",
}
_LINE_FIELDS = _LEGACY_LINE_FIELDS | {"annual_forecast_method"}
# Code-written provenance of a 0.6 structure: what the completion changed and
# the method, history depth and reason of every line.
_COVERAGE_STRUCTURE_FIELDS = {"auto_repairs", "coverage"}
_SUM_FIELDS = {"output_ref", "operator", "terms", "tie_out_concept", "evidence_refs"}
_DIVIDE_FIELDS = {
    "output_ref", "operator", "numerator_ref", "denominator_ref",
    "tie_out_concept", "evidence_refs",
}
_TERM_FIELDS = {"line_ref", "coefficient"}
_NOTE_FIELDS = {"ref", "content_hash", "source_content_hash"}
_TYPED_NOTE_FIELDS = {
    "schema_version", "ref", "content_hash", "target_ref", "company_ref",
    "statement_ingest_ref", "statement_filing_hash", "accession", "form",
    "applicability_kind", "periods",
}
_NOTE_PERIOD_FIELDS = {"period_start", "period_end"}
FINANCIAL_NOTE_EVIDENCE_BINDING_VERSION = "financial-note-evidence-binding-0.1"
DILUTED_EPS_NUMERATOR_NOTE_TARGET = "financial_note:diluted_eps_numerator:0.1"
_ANNUAL_STRUCTURE_VERSIONS = {
    ANNUAL_SCHEMA_VERSION, TYPED_NOTE_SCHEMA_VERSION,
    DIRECT_GROSS_PRETAX_SCHEMA_VERSION, REVENUE_PRETAX_SCHEMA_VERSION,
    COVERAGE_SCHEMA_VERSION,
}
_TYPED_NOTE_STRUCTURE_VERSIONS = {
    TYPED_NOTE_SCHEMA_VERSION, DIRECT_GROSS_PRETAX_SCHEMA_VERSION,
    REVENUE_PRETAX_SCHEMA_VERSION, COVERAGE_SCHEMA_VERSION,
}
_REVENUE_PRETAX_VERSIONS = {REVENUE_PRETAX_SCHEMA_VERSION, COVERAGE_SCHEMA_VERSION}


def is_coverage_structure(value: Any) -> bool:
    return value == COVERAGE_SCHEMA_VERSION


def _schema_object(properties: Mapping[str, Any], required: Sequence[str]) -> dict[str, Any]:
    return {
        "type": "object", "additionalProperties": False,
        "required": list(required), "properties": dict(properties),
    }


_SCHEMA_TEXT = {"type": "string", "minLength": 1, "maxLength": 160}
_SCHEMA_REF = {
    "type": "string", "minLength": 1, "maxLength": 80,
    "pattern": r"^[a-z][a-z0-9]*(?:[-_:][a-z0-9]+)*$",
}
_STRUCTURE_LINE_SCHEMA = _schema_object(
    {
        "ref": _SCHEMA_REF,
        "role": {
            "enum": list(DRAFTED_COVERAGE_ROLES),
            "description": (
                "Use company_presented_component for an exact filed currency amount whose "
                "company-specific bridge position is expressed by a tied formula. "
                "A company-specific total (for example total operating expenses) is a "
                "company_presented_subtotal: make it a derived line whose sum formula "
                "adds its filed components and ties to the filed total; never use a "
                "filed total as a formula term. "
                "Neither role represents shares, per-share figures, ratios or percentages. "
                "Cash-flow leaves use share_repurchase, dividends_paid or acquisitions; "
                "balance-sheet leaves use cash_and_equivalents, debt_current, "
                "debt_noncurrent, accounts_receivable, accounts_payable or "
                "deferred_revenue. Include diluted_weighted_average_shares and a "
                "derived diluted_eps (divide) whenever the company files them."
            ),
        },
        "label": _SCHEMA_TEXT,
        "kind": {
            "enum": list(LINE_KINDS),
            "description": (
                "filed requires an exact filed concept; derived requires concept "
                "null and an associated formula. Prefer filed when an exact standard "
                "subtotal such as GrossProfit is available, except where the contract "
                "requires a final earnings formula."
            ),
        },
        "concept": {
            "type": ["string", "null"], "maxLength": 200,
            "description": (
                "Exact filed concept for kind filed; always null for kind derived. "
                "A derived line's historical filed tie belongs only in its "
                "formula.tie_out_concept."
            ),
        },
        "statement": {
            "enum": list(STATEMENTS),
            "description": (
                "income for every income-statement line. cash and balance lines are "
                "filed leaves only (never formula terms): cash lines are quarterly "
                "durations, balance lines are instants."
            ),
        },
        "unit": {
            "type": "string", "minLength": 3, "maxLength": 24,
            "description": (
                "Use shares only for diluted_weighted_average_shares and a "
                "currency-per-share unit such as usd_per_share only for diluted_eps. "
                "Every other role requires its filed ISO-4217 currency, such as usd. "
                "Do not change a source unit to fit a role."
            ),
        },
        "period_kind": {
            "enum": list(PERIOD_KINDS),
            "description": "instant only for balance-sheet lines; duration otherwise.",
        },
        "annual_semantics": {"enum": list(ANNUAL_SEMANTICS)},
        "forecast_method": {
            "enum": list(FORECAST_METHODS),
            "description": (
                "Every filed line with at least eight quarters of history needs a "
                "forecast method; unavailable is only for a line with less history, "
                "and the code records why. year_over_year_growth or seasonal_factor for "
                "stable seasonal distributions; average quarterly_growth is prohibited "
                "for significant seasonality. quarterly_growth for other revenue and share "
                "counts; share_of_line (ratio to a line) for costs (base revenue), "
                "income tax (base pretax income) and noncontrolling interest (base "
                "net income); zero_one_off for a line the spec calls one_off; "
                "rate_times_balance (base a balance-sheet line) or carry_forward_mean "
                "for interest and other non-operating items; carry_forward_mean for "
                "other recurring income-statement lines and cash-flow leaves, never "
                "for balance-sheet instants. Balance-sheet leaves (including accounts_receivable, "
                "accounts_payable, deferred_revenue, debt and other assets/liabilities) use "
                "hold_last, share_of_line with an exact legitimate revenue base, or unavailable "
                "when the actual base is missing. Only cash_and_equivalents may additionally "
                "use roll_forward. Balance leaves have annual_semantics not_applicable and "
                "never use carry_forward_mean. A filed standard subtotal is tie "
                "authority only: forecast it as a derived formula line instead "
                "(operating income = revenue minus each cost line; pretax income = "
                "operating income plus every non-operating item; net income = pretax "
                "income minus tax; attributable = net income minus noncontrolling "
                "interest). Every formula term must itself be forecastable. The code "
                "completes missing methods and repairs a filed subtotal used as a "
                "term, but a structure whose earnings chain still cannot be computed "
                "is refused."
            ),
        },
        "forecast_base_ref": {"type": ["string", "null"], "maxLength": 80},
        "annual_forecast_method": {
            "type": ["string", "null"],
            "enum": [*ANNUAL_FORECAST_METHODS, None],
            "description": (
                "Only diluted weighted-average shares may select "
                "day_weighted_quarters. It authorizes annual forecast aggregation "
                "only after an exact historical direct-annual tie."
            ),
        },
    },
    tuple(sorted(_LINE_FIELDS)),
)
_SUM_FORMULA_SCHEMA = _schema_object(
    {
        "output_ref": _SCHEMA_REF, "operator": {"const": "sum"},
        "terms": {"type": "array", "minItems": 1, "maxItems": 24,
                  "description": (
                      "Use the statement bridge roles accepted for the output. A direct "
                      "company presentation may derive pretax_income from exactly one "
                      "gross_profit term at coefficient 1 plus one or more "
                      "company_presented_component or company_presented_subtotal terms. "
                      "A direct company presentation may instead use exactly one revenue "
                      "term at coefficient 1 with those same company-presented terms."
                  ),
                  "items": _schema_object(
                      {"line_ref": _SCHEMA_REF,
                       "coefficient": {"enum": ["-1", "1"]}},
                      tuple(sorted(_TERM_FIELDS)))},
        "tie_out_concept": {
            "type": ["string", "null"], "maxLength": 200,
            "description": (
                "Exact filed historical result tied by this formula; do not copy "
                "it into the derived output line's concept field. A derived sum "
                "must tie in every applicable source period. The only tolerance is "
                "the filed presentation rounding of that period (each presented "
                "line rounded to the same unit, at most (n + 1) / 2 units for n "
                "filed leaves); never an arbitrary tolerance or a balancing term."
            ),
        },
        "evidence_refs": {"type": "array", "minItems": 1, "maxItems": 24,
                          "items": _SCHEMA_TEXT},
    },
    tuple(sorted(_SUM_FIELDS)),
)
_DIVIDE_FORMULA_SCHEMA = _schema_object(
    {
        "output_ref": _SCHEMA_REF, "operator": {"const": "divide"},
        "numerator_ref": _SCHEMA_REF, "denominator_ref": _SCHEMA_REF,
        "tie_out_concept": {
            "type": ["string", "null"], "maxLength": 200,
            "description": (
                "Exact filed historical result tied by this formula; do not copy "
                "it into the derived output line's concept field. A per-share divide "
                "ties using each source period's disclosed filed precision; do not "
                "invent an arbitrary or global tolerance."
            ),
        },
        "evidence_refs": {"type": "array", "minItems": 1, "maxItems": 24,
                          "items": _SCHEMA_TEXT},
    },
    tuple(sorted(_DIVIDE_FIELDS)),
)
STRUCTURE_PROPOSAL_SCHEMA = _schema_object(
    {
        "schema_version": {"const": SCHEMA_VERSION},
        "lines": {"type": "array", "minItems": 1,
                  "maxItems": MAX_STRUCTURE_LINES, "items": _STRUCTURE_LINE_SCHEMA},
        "formulas": {"type": "array", "minItems": 1,
                     "maxItems": MAX_STRUCTURE_FORMULAS,
                     "items": {"oneOf": [_SUM_FORMULA_SCHEMA,
                                          _DIVIDE_FORMULA_SCHEMA]}},
    },
    ("schema_version", "lines", "formulas"),
)

_SUM_ROLE_INPUTS: dict[str, frozenset[str]] = {
    "cost_of_revenue": frozenset({"cost_of_revenue"}),
    "gross_profit": frozenset({"revenue", "cost_of_revenue"}),
    "operating_expense": frozenset({"operating_expense"}),
    "operating_income": frozenset({
        "revenue", "cost_of_revenue", "gross_profit", "operating_expense",
        "other_operating_income_expense",
    }),
    "nonoperating_income_expense": frozenset({
        "nonoperating_income_expense", "interest_income", "interest_expense",
        "other_nonoperating_income_expense",
    }),
    "pretax_income": frozenset({
        "operating_income", "nonoperating_income_expense", "interest_income",
        "interest_expense", "other_nonoperating_income_expense",
    }),
    "income_from_continuing_operations": frozenset({
        "pretax_income", "income_tax_expense",
    }),
    "net_income": frozenset({
        "pretax_income", "income_tax_expense", "income_from_continuing_operations",
        "discontinued_operations",
    }),
    "parent_net_income": frozenset({"net_income", "noncontrolling_interest"}),
    "diluted_eps_numerator": frozenset({
        "parent_net_income", "preferred_dividends",
        "participating_securities_allocation", "dilutive_securities_adjustment",
        # A filer with no noncontrolling interest divides net income itself; a
        # filer with exchangeable shares (Accenture Canada) adds that
        # interest back because those shares are in the diluted count.  The
        # filed diluted-EPS tie decides which, never the role table.
        "net_income", "noncontrolling_interest",
    }),
}
_COMPANY_COMPONENT_ROLE = "company_presented_component"
_COMPANY_SUBTOTAL_ROLE = "company_presented_subtotal"
_COMPANY_SUM_INPUT_ROLES = frozenset(ROLES) - {
    "diluted_weighted_average_shares", "diluted_eps",
}
_FORMULA_TOTAL_ROLES = frozenset({
    "gross_profit", "operating_income", "nonoperating_income_expense",
    "pretax_income", "income_from_continuing_operations", "net_income",
    "parent_net_income", "diluted_eps_numerator", "diluted_eps",
    _COMPANY_SUBTOTAL_ROLE,
})
_FINAL_EARNINGS_ROLES = frozenset({
    "income_from_continuing_operations", "net_income", "parent_net_income",
})


class FinancialStatementStructureError(ValueError):
    pass


def _closed(value: Any, fields: set[str], name: str) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != fields:
        raise FinancialStatementStructureError(f"{name} has an invalid closed shape")
    return dict(value)


def _text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise FinancialStatementStructureError(f"{name} must be non-empty text")
    return value.strip()


def _decimal(value: Any, name: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (str, int, Decimal)):
        raise FinancialStatementStructureError(f"{name} must be a decimal string")
    try:
        result = Decimal(str(value))
    except Exception as exc:
        raise FinancialStatementStructureError(f"{name} must be a decimal string") from exc
    if not result.is_finite():
        raise FinancialStatementStructureError(f"{name} must be finite")
    return result


def financial_input_authority(value: Mapping[str, Any]) -> dict[str, Any]:
    """Stable financial-only projection, excluding unrelated company state."""

    if not isinstance(value, Mapping):
        raise FinancialStatementStructureError("financial inputs must be an object")
    company_ref = _text(value.get("company_ref"), "financial inputs company_ref")
    lines = value.get("filed_lines")
    if not isinstance(lines, list):
        raise FinancialStatementStructureError("financial inputs filed_lines must be a list")
    projection = {
        "schema_version": (
            "financial-input-authority-0.2"
            if value.get("schema_version") == "0.3"
            else "financial-input-authority-0.1"
        ),
        "company_ref": company_ref,
        "periods": list(value.get("periods") or []),
        "filed_lines": lines,
        "cash_flow_inputs": list(value.get("cash_flow_inputs") or []),
    }
    return {**projection, "content_hash": content_hash(projection)}


def _filed_index(inputs: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    indexed: dict[str, dict[str, Any]] = {}
    for raw in inputs.get("filed_lines") or []:
        if not isinstance(raw, Mapping):
            raise FinancialStatementStructureError("financial input line must be an object")
        concept = _text(raw.get("concept"), "financial input concept")
        if concept in indexed:
            raise FinancialStatementStructureError(f"financial input concept {concept} is duplicated")
        indexed[concept] = dict(raw)
    return indexed


def _units(line: Mapping[str, Any], *, include_duration: bool = False) -> set[str]:
    return {
        str(cell.get("unit")).casefold()
        for cell in (
            list((line.get("cells") or {}).values())
            + (list(line.get("duration_facts") or []) if include_duration else [])
        )
        if isinstance(cell, Mapping) and cell.get("unit")
    }


def _is_currency_unit(value: str) -> bool:
    return re.fullmatch(r"[a-z]{3}", value) is not None


def _statement_refs(inputs: Mapping[str, Any], *, include_duration: bool = False) -> set[str]:
    return {
        str(ref)
        for line in inputs.get("filed_lines") or []
        if isinstance(line, Mapping)
        for cell in (
            list((line.get("cells") or {}).values())
            + (list(line.get("duration_facts") or []) if include_duration else [])
        )
        if isinstance(cell, Mapping)
        for ref in (cell.get("source_accessions") or [])
        if isinstance(ref, str) and ref
    }


def _digest(value: Any, name: str) -> str:
    digest = _text(value, name)
    if len(digest) != 64 or any(ch not in "0123456789abcdef" for ch in digest):
        raise FinancialStatementStructureError(f"{name} must be a lowercase SHA-256")
    return digest


def _note_refs(note_evidence: Sequence[Mapping[str, Any]], *,
               schema_version: str) -> set[str]:
    if schema_version in _TYPED_NOTE_STRUCTURE_VERSIONS:
        # Kept lazy to avoid the evidence -> inventory -> annual projection ->
        # structure import cycle while still refusing a contract split.
        from .financial_note_evidence import BINDING_SCHEMA_VERSION, TARGET_REF
        if (
            BINDING_SCHEMA_VERSION != FINANCIAL_NOTE_EVIDENCE_BINDING_VERSION
            or TARGET_REF != DILUTED_EPS_NUMERATOR_NOTE_TARGET
        ):
            raise FinancialStatementStructureError(
                "financial note evidence contract constants drifted"
            )
    refs: set[str] = set()
    for index, raw in enumerate(note_evidence):
        fields = _TYPED_NOTE_FIELDS if schema_version in _TYPED_NOTE_STRUCTURE_VERSIONS else _NOTE_FIELDS
        wire = _closed(raw, fields, f"note_evidence[{index}]")
        ref = _text(wire["ref"], f"note_evidence[{index}].ref")
        _digest(wire["content_hash"], f"note_evidence[{index}].content_hash")
        if schema_version in _TYPED_NOTE_STRUCTURE_VERSIONS:
            if wire["schema_version"] != FINANCIAL_NOTE_EVIDENCE_BINDING_VERSION:
                raise FinancialStatementStructureError("note evidence schema_version is invalid")
            if wire["target_ref"] != DILUTED_EPS_NUMERATOR_NOTE_TARGET:
                raise FinancialStatementStructureError(
                    "note evidence target is not the diluted EPS numerator target"
                )
            for field in ("company_ref", "statement_ingest_ref", "accession", "form"):
                _text(wire[field], f"note_evidence[{index}].{field}")
            _digest(wire["statement_filing_hash"],
                    f"note_evidence[{index}].statement_filing_hash")
            if wire["applicability_kind"] not in {"annual", "quarter"}:
                raise FinancialStatementStructureError(
                    "note evidence applicability_kind is invalid"
                )
            periods = wire["periods"]
            if not isinstance(periods, list) or not periods:
                raise FinancialStatementStructureError(
                    "note evidence periods must be a non-empty list"
                )
            normalized_periods: list[tuple[str, str]] = []
            for number, raw_period in enumerate(periods):
                period = _closed(
                    raw_period, _NOTE_PERIOD_FIELDS,
                    f"note_evidence[{index}].periods[{number}]",
                )
                start = _text(period["period_start"], "note evidence period_start")
                end = _text(period["period_end"], "note evidence period_end")
                try:
                    # SEC duration facts and company_model_series use inclusive
                    # day counts.  The note authority must use the same boundary
                    # or a one-day period can be accepted by one side and refused
                    # by the other.
                    days = (
                        date.fromisoformat(end) - date.fromisoformat(start)
                    ).days + 1
                except ValueError as exc:
                    raise FinancialStatementStructureError(
                        "note evidence period must use ISO dates"
                    ) from exc
                correct_kind = (
                    NINE_MONTH_MAX_DAYS < days <= ANNUAL_MAX_DAYS
                    if wire["applicability_kind"] == "annual"
                    else QUARTER_MIN_DAYS <= days <= QUARTER_MAX_DAYS
                )
                if not correct_kind:
                    raise FinancialStatementStructureError(
                        "note evidence period differs from its applicability kind"
                    )
                normalized_periods.append((start, end))
            if normalized_periods != sorted(set(normalized_periods)):
                raise FinancialStatementStructureError(
                    "note evidence periods must be unique and sorted"
                )
        else:
            _digest(wire["source_content_hash"],
                    f"note_evidence[{index}].source_content_hash")
        if ref in refs:
            raise FinancialStatementStructureError("note evidence ref is duplicated")
        refs.add(ref)
    return refs


def _normalized_note_evidence(
    note_evidence: Sequence[Mapping[str, Any]],
    resolver: Callable[[str], Mapping[str, Any] | None] | None,
    *, schema_version: str,
) -> list[dict[str, Any]]:
    # _note_refs performs the closed/hash validation. Keep the complete proof
    # in the normalized authority so a note ref cannot later resolve to new bytes.
    refs = _note_refs(note_evidence, schema_version=schema_version)
    if refs and resolver is None:
        raise FinancialStatementStructureError(
            "note evidence requires an authoritative resolver"
        )
    normalized = sorted((dict(item) for item in note_evidence), key=lambda item: item["ref"])
    for item in normalized:
        held = None if resolver is None else resolver(item["ref"])
        if not isinstance(held, Mapping) or dict(held) != item:
            raise FinancialStatementStructureError("note evidence authority differs")
    return normalized


def _spec_authority(spec: Mapping[str, Any]) -> dict[str, str]:
    company_ref = _text(spec.get("company_ref"), "company spec company_ref")
    spec_ref = _text(spec.get("spec_id"), "company spec spec_id")
    digest = _text(spec.get("content_hash"), "company spec content_hash")
    if len(digest) != 64 or any(ch not in "0123456789abcdef" for ch in digest):
        raise FinancialStatementStructureError(
            "company spec content_hash must be a lowercase SHA-256"
        )
    return {"company_ref": company_ref, "spec_ref": spec_ref, "spec_hash": digest}


def _validate_spec_alignment(
    spec: Mapping[str, Any], lines: Sequence[Mapping[str, Any]],
) -> None:
    """Keep the extension attached to concepts selected by the current spec."""

    revenue = [line for line in lines if line["role"] == "revenue"]
    if (
        len(revenue) != 1
        or revenue[0]["kind"] != "filed"
        or revenue[0]["concept"] != spec.get("revenue_anchor_concept")
    ):
        raise FinancialStatementStructureError(
            "structure revenue must be the company spec's exact filed anchor"
        )
    held_concepts = {
        line["concept"] for line in lines if line["kind"] == "filed"
    }
    selected_expenses = {
        item.get("basis_concept")
        for item in (spec.get("expense_lines") or [])
        if isinstance(item, Mapping) and item.get("basis_concept")
    }
    missing = sorted(selected_expenses - held_concepts)
    if missing:
        raise FinancialStatementStructureError(
            "structure omits expense concepts selected by the company spec: "
            + ", ".join(str(item) for item in missing)
        )


def _normalize_line(
    raw: Any, index: int, filed: Mapping[str, Mapping[str, Any]], *,
    validate_values: bool = True, schema_version: str = SCHEMA_VERSION,
) -> dict[str, Any]:
    fields = _LEGACY_LINE_FIELDS if schema_version == LEGACY_SCHEMA_VERSION else _LINE_FIELDS
    wire = _closed(raw, fields, f"lines[{index}]")
    line = {
        "ref": _text(wire["ref"], f"lines[{index}].ref"),
        "role": _text(wire["role"], f"lines[{index}].role"),
        "label": _text(wire["label"], f"lines[{index}].label"),
        "kind": _text(wire["kind"], f"lines[{index}].kind"),
        "concept": wire["concept"],
        "statement": _text(wire["statement"], f"lines[{index}].statement"),
        "unit": _text(wire["unit"], f"lines[{index}].unit").casefold(),
        "period_kind": _text(wire["period_kind"], f"lines[{index}].period_kind"),
        "annual_semantics": _text(
            wire["annual_semantics"], f"lines[{index}].annual_semantics"
        ),
        "forecast_method": _text(
            wire["forecast_method"], f"lines[{index}].forecast_method"
        ),
        "forecast_base_ref": wire["forecast_base_ref"],
    }
    if schema_version in _ANNUAL_STRUCTURE_VERSIONS:
        line["annual_forecast_method"] = wire["annual_forecast_method"]
    coverage = is_coverage_structure(schema_version)
    for field, allowed in (
        ("role", COVERAGE_ROLES if coverage else ROLES), ("kind", LINE_KINDS),
        ("statement", STATEMENTS),
        ("period_kind", PERIOD_KINDS), ("annual_semantics", ANNUAL_SEMANTICS),
        ("forecast_method", FORECAST_METHODS if coverage else LEGACY_FORECAST_METHODS),
    ):
        if line[field] not in allowed:
            raise FinancialStatementStructureError(f"lines[{index}].{field} is invalid")
    if line["role"] in STATEMENT_LEAF_ROLES:
        _normalize_statement_leaf(line, index, filed, schema_version=schema_version)
        return line
    if line["statement"] != "income" or line["period_kind"] != "duration":
        raise FinancialStatementStructureError(
            "financial statement structure 0.1 supports duration income lines only"
        )
    from .forecast_coverage import COVERAGE_CONCEPTS
    depreciation = line.get("concept") in COVERAGE_CONCEPTS["depreciation_amortization"]
    if line["forecast_method"] in {"hold_last", "roll_forward"} and not (depreciation and line["forecast_method"] == "roll_forward"):
        raise FinancialStatementStructureError(
            f"lines[{index}] {line['forecast_method']} is reserved for balance-sheet lines"
        )
    if (
        line["role"] == _COMPANY_COMPONENT_ROLE and line["kind"] != "filed"
    ) or (
        line["role"] == _COMPANY_SUBTOTAL_ROLE and line["kind"] != "derived"
    ):
        raise FinancialStatementStructureError(
            "company-presented components must be filed and subtotals must be derived"
        )
    concept = line["concept"]
    if line["kind"] == "derived":
        if concept is not None:
            raise FinancialStatementStructureError("a derived line cannot claim a filed concept")
        if line["forecast_method"] != "formula" or line["forecast_base_ref"] is not None:
            raise FinancialStatementStructureError(
                "a derived line must use formula forecast_method without a base"
            )
    else:
        if line["forecast_method"] == "formula":
            raise FinancialStatementStructureError(
                "a filed line cannot use formula forecast_method"
            )
        concept = _text(concept, f"lines[{index}].concept")
        source = filed.get(concept)
        if source is None or source.get("status") != "filed":
            raise FinancialStatementStructureError(
                f"lines[{index}].concept is not a filed line in this input authority"
            )
        if (source.get("statement") != line["statement"]
                and not _same_net_income_source(line, source, concept)):
            raise FinancialStatementStructureError("filed line statement differs from authority")
        if source.get("period_basis") != line["period_kind"]:
            raise FinancialStatementStructureError("filed line period kind differs from authority")
        units = _units(source, include_duration=schema_version in _TYPED_NOTE_STRUCTURE_VERSIONS)
        if units != {line["unit"]}:
            raise FinancialStatementStructureError("filed line unit differs or is ambiguous")
        line["concept"] = concept
        if (
            line["role"] in _FORMULA_TOTAL_ROLES
            and line["forecast_method"] != "unavailable"
            and not (
                # 0.6: a filed "interest and other income, net" is the
                # company's own non-operating leaf; its place in the pretax
                # bridge is still proven by the tied pretax formula.
                coverage and line["role"] == "nonoperating_income_expense"
                and line["forecast_method"] in {
                    "carry_forward_mean", "rate_times_balance", "zero_one_off"}
            )
        ):
            raise FinancialStatementStructureError(
                "a filed subtotal may only be actual/tie authority or unavailable; "
                "forecasted subtotals require an explicit formula"
            )
    if line["forecast_method"] in BASED_FORECAST_METHODS or (depreciation and line["forecast_method"] == "roll_forward"):
        line["forecast_base_ref"] = _text(
            line["forecast_base_ref"], f"lines[{index}].forecast_base_ref"
        )
    elif line["forecast_base_ref"] is not None:
        raise FinancialStatementStructureError(
            "only share_of_line or rate_times_balance may carry forecast_base_ref"
        )
    if line["role"] == "diluted_weighted_average_shares":
        if (
            line["kind"] != "filed"
            or
            line["annual_semantics"] != "direct_annual"
            or line["unit"] != "shares"
            or line["period_kind"] != "duration"
        ):
            raise FinancialStatementStructureError(
                "diluted weighted-average shares require duration direct_annual shares"
            )
        if validate_values and line["kind"] == "filed" and any(
            _decimal(cell.get("value"), "diluted weighted-average shares") <= 0
            for cell in (filed[line["concept"]].get("cells") or {}).values()
            if isinstance(cell, Mapping)
        ):
            raise FinancialStatementStructureError(
                "diluted weighted-average shares must be positive"
            )
        if schema_version in _ANNUAL_STRUCTURE_VERSIONS:
            annual_method = _text(
                line["annual_forecast_method"],
                f"lines[{index}].annual_forecast_method",
            )
            if annual_method not in ANNUAL_FORECAST_METHODS:
                raise FinancialStatementStructureError(
                    f"lines[{index}].annual_forecast_method is invalid"
                )
            line["annual_forecast_method"] = annual_method
            if (
                annual_method == "day_weighted_quarters"
                and line["forecast_method"] not in {"quarterly_growth", "year_over_year_growth", "seasonal_factor", "share_of_line"}
            ):
                raise FinancialStatementStructureError(
                    "day-weighted annual shares require an explicit quarterly forecast method"
                )
    elif schema_version in _ANNUAL_STRUCTURE_VERSIONS and line["annual_forecast_method"] is not None:
        raise FinancialStatementStructureError(
            "only diluted weighted-average shares may carry annual_forecast_method"
        )
    if line["role"] == "diluted_eps" and line["annual_semantics"] not in {
        "direct_annual", "annual_ratio",
    }:
        raise FinancialStatementStructureError(
            "diluted EPS cannot be aggregated from quarterly EPS"
        )
    if line["role"] == "diluted_eps":
        if (
            line["period_kind"] != "duration"
            or re.fullmatch(r"[a-z]{3}_per_share", line["unit"]) is None
        ):
            raise FinancialStatementStructureError(
                "diluted EPS requires a duration currency-per-share unit"
            )
    elif line["role"] != "diluted_weighted_average_shares":
        if line["period_kind"] != "duration" or not _is_currency_unit(line["unit"]):
            raise FinancialStatementStructureError(
                "income statement amounts require a duration ISO-4217 currency unit"
            )
        if line["annual_semantics"] not in {"sum_quarters", "direct_annual"}:
            raise FinancialStatementStructureError(
                "income statement amounts require explicit amount annual semantics"
            )
    return line


def _normalize_statement_leaf(
    line: dict[str, Any], index: int, filed: Mapping[str, Mapping[str, Any]], *,
    schema_version: str = SCHEMA_VERSION,
) -> None:
    """A 0.6 cash-flow or balance-sheet leaf: filed, method-forecast, no formula."""

    cash = line["role"] in CASH_LEAF_ROLES
    expected = ("cash", "duration", "sum_quarters") if cash else (
        "balance", "instant", "not_applicable")
    if (line["statement"], line["period_kind"], line["annual_semantics"]) != expected:
        raise FinancialStatementStructureError(
            f"lines[{index}] {line['role']} must be a {expected[0]} {expected[1]} "
            f"line with {expected[2]} annual semantics"
        )
    if line["kind"] != "filed":
        raise FinancialStatementStructureError(
            f"lines[{index}] cash-flow and balance-sheet lines must be filed leaves"
        )
    allowed = (
        {"carry_forward_mean", "year_over_year_growth", "seasonal_factor", "share_of_line", "unavailable"} if cash else
        {"hold_last", "share_of_line", "unavailable"}
        | ({"roll_forward"} if line["role"] == "cash_and_equivalents" else set())
    )
    if line["role"] == "depreciation_amortization":
        allowed |= {"roll_forward"}
    if line["forecast_method"] not in allowed:
        raise FinancialStatementStructureError(
            f"lines[{index}] {line['role']} cannot use {line['forecast_method']}"
        )
    if line["forecast_method"] == "share_of_line" or (line["role"] == "depreciation_amortization" and line["forecast_method"] == "roll_forward"):
        line["forecast_base_ref"] = _text(
            line["forecast_base_ref"], f"lines[{index}].forecast_base_ref"
        )
    elif line["forecast_base_ref"] is not None:
        raise FinancialStatementStructureError(
            "only share_of_line may carry forecast_base_ref"
        )
    if line.get("annual_forecast_method") is not None:
        raise FinancialStatementStructureError(
            "only diluted weighted-average shares may carry annual_forecast_method"
        )
    concept = _text(line["concept"], f"lines[{index}].concept")
    source = filed.get(concept)
    if source is None or source.get("status") != "filed":
        raise FinancialStatementStructureError(
            f"lines[{index}].concept is not a filed line in this input authority"
        )
    if source.get("statement") != line["statement"]:
        raise FinancialStatementStructureError("filed line statement differs from authority")
    if source.get("period_basis") != line["period_kind"]:
        raise FinancialStatementStructureError("filed line period kind differs from authority")
    # Duration authority proves a unit even when no standalone quarter exists.
    # This does not create quarter cells or authorize cumulative-as-quarter values.
    units = _units(source, include_duration=schema_version in _TYPED_NOTE_STRUCTURE_VERSIONS)
    if units != {line["unit"]} or not _is_currency_unit(line["unit"]):
        raise FinancialStatementStructureError("filed line unit differs or is ambiguous")
    line["concept"] = concept


def _same_net_income_source(line: Mapping[str, Any], source: Mapping[str, Any],
                            concept: str) -> bool:
    """A consolidated earnings fact may open cash flow and tie income.

    The filed authority retains its original cash statement, values, contexts
    and evidence. This mirrors the presentation's exact-concept carry; it
    does not move arbitrary cash lines or permit a different accounting fact.
    Unit, duration, source evidence and numeric tie checks still run normally.
    """
    from .company_model_inputs import NET_INCOME_CONCEPTS

    return (
        concept in NET_INCOME_CONCEPTS
        and source.get("status") == "filed"
        and source.get("statement") == "cash"
        and source.get("period_basis") == "duration"
        and line.get("statement") == "income"
        and line.get("period_kind") == "duration"
        and line.get("role") in {"net_income", "diluted_eps_numerator"}
    )


def _normalize_formula(
    raw: Any, index: int, lines: Mapping[str, Mapping[str, Any]],
    filed: Mapping[str, Mapping[str, Any]], allowed_evidence: set[str], *,
    schema_version: str, note_evidence: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise FinancialStatementStructureError(f"formulas[{index}] must be an object")
    operator = raw.get("operator")
    fields = _SUM_FIELDS if operator == "sum" else _DIVIDE_FIELDS
    wire = _closed(raw, fields, f"formulas[{index}]")
    output = _text(wire["output_ref"], f"formulas[{index}].output_ref")
    if output not in lines or lines[output]["kind"] != "derived":
        raise FinancialStatementStructureError("formula output must name a derived line")
    refs = wire["evidence_refs"]
    if (
        not isinstance(refs, list) or not refs
        or any(not isinstance(ref, str) or ref not in allowed_evidence for ref in refs)
        or len(refs) != len(set(refs))
    ):
        raise FinancialStatementStructureError(
            "formula evidence_refs must name held statement or note evidence"
        )
    tie = wire["tie_out_concept"]
    if tie is not None:
        tie = _text(tie, f"formulas[{index}].tie_out_concept")
        target = filed.get(tie)
        if target is None or target.get("status") != "filed":
            raise FinancialStatementStructureError("formula tie-out concept is not filed")
        if (target.get("statement") != lines[output]["statement"]
                and not _same_net_income_source(lines[output], target, tie)):
            raise FinancialStatementStructureError("formula tie-out statement differs")
        if _units(
            target, include_duration=schema_version in _TYPED_NOTE_STRUCTURE_VERSIONS,
        ) != {lines[output]["unit"]}:
            raise FinancialStatementStructureError("formula tie-out unit differs")
        if target.get("period_basis") != lines[output]["period_kind"]:
            raise FinancialStatementStructureError("formula tie-out period kind differs")
    elif not (
        schema_version in _TYPED_NOTE_STRUCTURE_VERSIONS
        and operator == "sum"
        and lines[output]["role"] == "diluted_eps_numerator"
        and (
            (note_evidence and any(ref in note_evidence for ref in refs))
            # 0.6: a numerator with no filed total of its own (net income
            # attributable plus exchangeable-share interest) is proven by the
            # filed diluted EPS it divides into, period by period.
            or is_coverage_structure(schema_version)
        )
    ):
        raise FinancialStatementStructureError(
            "every derived formula must tie to an exact filed concept"
        )
    result: dict[str, Any] = {
        "output_ref": output, "operator": operator,
        "tie_out_concept": tie, "evidence_refs": list(refs),
    }
    if operator == "sum":
        terms = wire["terms"]
        if not isinstance(terms, list) or not terms:
            raise FinancialStatementStructureError("sum formula needs terms")
        normalized = []
        for number, raw_term in enumerate(terms):
            term = _closed(raw_term, _TERM_FIELDS, f"formulas[{index}].terms[{number}]")
            ref = _text(term["line_ref"], "formula term line_ref")
            if ref not in lines or ref == output:
                raise FinancialStatementStructureError("formula term line_ref is invalid")
            coefficient = _decimal(term["coefficient"], "formula term coefficient")
            if coefficient not in {Decimal(-1), Decimal(1)}:
                raise FinancialStatementStructureError("sum coefficient must be -1 or 1")
            if lines[ref]["unit"] != lines[output]["unit"]:
                raise FinancialStatementStructureError("sum terms must use the output unit")
            if lines[ref]["period_kind"] != lines[output]["period_kind"]:
                raise FinancialStatementStructureError(
                    "sum terms must use the output period kind"
                )
            normalized.append({"line_ref": ref, "coefficient": str(int(coefficient))})
        if len({term["line_ref"] for term in normalized}) != len(normalized):
            raise FinancialStatementStructureError("sum formula repeats a term")
        output_role = lines[output]["role"]
        allowed_roles = (
            _COMPANY_SUM_INPUT_ROLES
            if output_role == _COMPANY_SUBTOTAL_ROLE
            else _SUM_ROLE_INPUTS.get(output_role)
        )
        direct_pretax_role = (
            "gross_profit"
            if schema_version in {DIRECT_GROSS_PRETAX_SCHEMA_VERSION,
                                  *_REVENUE_PRETAX_VERSIONS}
            else None
        )
        if schema_version in _REVENUE_PRETAX_VERSIONS:
            direct_pretax_roles = {"gross_profit", "revenue"}
        else:
            direct_pretax_roles = {direct_pretax_role} if direct_pretax_role else set()
        direct_anchor_terms = [
            term for term in normalized
            if lines[term["line_ref"]]["role"] in direct_pretax_roles
        ]
        direct_pretax_presentation = (
            bool(direct_pretax_roles)
            and output_role == "pretax_income"
            and len(direct_anchor_terms) == 1
            and direct_anchor_terms[0]["coefficient"] == "1"
            and len(normalized) >= 2
            and all(
                lines[term["line_ref"]]["role"] in {
                    *direct_pretax_roles,
                    _COMPANY_COMPONENT_ROLE, _COMPANY_SUBTOTAL_ROLE,
                }
                for term in normalized
            )
        )
        if not direct_pretax_presentation and (allowed_roles is None or any(
            lines[term["line_ref"]]["role"] not in (
                allowed_roles | {_COMPANY_COMPONENT_ROLE, _COMPANY_SUBTOTAL_ROLE}
            )
            for term in normalized
        )):
            raise FinancialStatementStructureError(
                "sum formula roles do not match its company statement output; "
                f"formulas[{index}].output_ref={output!r}, output_role={output_role!r}; "
                "disallowed_terms=" + repr([
                    {"line_ref": term["line_ref"],
                     "role": lines[term["line_ref"]]["role"]}
                    for term in normalized
                    if allowed_roles is None or lines[term["line_ref"]]["role"] not in (
                        allowed_roles | {_COMPANY_COMPONENT_ROLE, _COMPANY_SUBTOTAL_ROLE}
                    )
                ]) + "; allowed_roles=" + repr(sorted(
                    (allowed_roles or frozenset())
                    | {_COMPANY_COMPONENT_ROLE, _COMPANY_SUBTOTAL_ROLE}
                ))
            )
        if tie is None:
            cited_notes = [ref for ref in refs if ref in (note_evidence or {})]
            if cited_notes:
                if any(lines[term["line_ref"]]["kind"] != "filed" for term in normalized):
                    raise FinancialStatementStructureError(
                        "note-backed diluted EPS numerator terms must be exact filed lines"
                    )
            elif not is_coverage_structure(schema_version):
                raise FinancialStatementStructureError(
                    "untied diluted EPS numerator needs exact typed note evidence"
                )
            # else: 0.6 EPS-proven numerator; the consuming divide must tie
            # to the filed diluted EPS (checked with the whole structure).
        result["terms"] = normalized
    elif operator == "divide":
        numerator = _text(wire["numerator_ref"], "formula numerator_ref")
        denominator = _text(wire["denominator_ref"], "formula denominator_ref")
        if numerator not in lines or denominator not in lines:
            raise FinancialStatementStructureError("divide formula names an unknown line")
        if lines[output]["role"] != "diluted_eps":
            raise FinancialStatementStructureError("divide is reserved for diluted EPS")
        if lines[numerator]["role"] != "diluted_eps_numerator":
            raise FinancialStatementStructureError(
                "EPS numerator must use the company-specific diluted EPS numerator role; "
                f"formulas[{index}].output_ref={output!r}, "
                f"numerator_ref={numerator!r}, "
                f"numerator_role={lines[numerator]['role']!r}"
            )
        if lines[denominator]["role"] != "diluted_weighted_average_shares":
            raise FinancialStatementStructureError(
                "EPS denominator must be diluted weighted-average shares"
            )
        if (
            lines[numerator]["period_kind"] != lines[output]["period_kind"]
            or lines[denominator]["period_kind"] != lines[output]["period_kind"]
        ):
            raise FinancialStatementStructureError(
                "EPS operands must use the output period kind"
            )
        if (
            not _is_currency_unit(lines[numerator]["unit"])
            or lines[denominator]["unit"] != "shares"
            or lines[output]["unit"] != f"{lines[numerator]['unit']}_per_share"
        ):
            raise FinancialStatementStructureError(
                "EPS requires matching currency numerator, shares denominator, and per-share output"
            )
        result.update({"numerator_ref": numerator, "denominator_ref": denominator})
    else:
        raise FinancialStatementStructureError("formula operator is invalid")
    return result


def _period_cells(line: Mapping[str, Any]) -> dict[tuple[str | None, str], Decimal]:
    return {
        (cell.get("period_start"), str(end)): _decimal(cell.get("value"), "filed cell value")
        for end, cell in (line.get("cells") or {}).items()
        if isinstance(cell, Mapping)
    }


def _filed_period_facts(
    line: Mapping[str, Any], *, period_start: str, period_end: str,
    accession: str, form: str,
) -> list[dict[str, Any]]:
    facts = [
        dict(item) for item in (line.get("duration_facts") or [])
        if isinstance(item, Mapping)
        and item.get("period_start") == period_start
        and item.get("period_end") == period_end
    ]
    if not facts:
        cell = (line.get("cells") or {}).get(period_end)
        if isinstance(cell, Mapping) and cell.get("period_start") == period_start:
            facts = [{**dict(cell), "period_end": period_end}]
    return [
        item for item in facts
        if accession in (item.get("source_accessions") or [])
        and form in (item.get("source_forms") or [])
    ]


def _rounding_unit(values: Sequence[Decimal]) -> Decimal:
    for exponent in (6, 5, 4, 3):
        unit = Decimal(10) ** exponent
        if all(value % unit == 0 for value in values):
            return unit
    return Decimal(1)


def presentation_rounding_ties(
    calculated: Decimal, filed: Decimal, leaf_values: Sequence[Decimal],
) -> bool:
    """Whether a sum misses its filed total only by the filed rounding.

    Each of ``n`` presented leaves and the presented total is rounded to the
    period's presentation unit (the largest of 1e3..1e6 dividing every one of
    them), so their sum can differ from the total by at most ``(n + 1) / 2``
    units.  Below a thousand there is no presentation rounding to allow.
    """

    if calculated == filed:
        return True
    unit = _rounding_unit([filed, *leaf_values])
    if unit < 1000 or not leaf_values:
        return False
    return abs(calculated - filed) <= unit * Decimal(len(leaf_values) + 1) / 2


def _filed_leaves(output: str, lines: Mapping[str, Mapping[str, Any]],
                  formulas: Mapping[str, Mapping[str, Any]]) -> list[str]:
    found: list[str] = []

    def walk(ref: str, trail: frozenset[str]) -> None:
        if ref in trail:
            return
        line = lines.get(ref) or {}
        formula = formulas.get(ref)
        if line.get("kind") == "filed" or formula is None:
            found.append(ref)
            return
        if formula.get("operator") != "sum":
            found.append(ref)
            return
        for term in formula.get("terms") or []:
            walk(str(term["line_ref"]), trail | {ref})

    walk(output, frozenset())
    return found


def _eps_proven(structure: Mapping[str, Any], formula: Mapping[str, Any]) -> bool:
    """A 0.6 untied numerator proven only through its filed diluted-EPS divide."""

    notes = {item["ref"] for item in structure.get("note_evidence") or []}
    return (
        is_coverage_structure(structure.get("schema_version"))
        and formula.get("operator") == "sum"
        and formula.get("tie_out_concept") is None
        and not any(ref in notes for ref in formula.get("evidence_refs") or [])
    )


def _note_backed_eps_replay(
    structure: Mapping[str, Any], filed: Mapping[str, Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Validate exact note-scoped numerator periods through filed diluted EPS."""

    if structure.get("schema_version") not in _TYPED_NOTE_STRUCTURE_VERSIONS:
        return []
    lines = {line["ref"]: line for line in structure["lines"]}
    formulas = list(structure["formulas"])
    notes = {item["ref"]: item for item in structure.get("note_evidence") or []}
    reports: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for formula in formulas:
        if formula.get("tie_out_concept") is not None:
            continue
        if _eps_proven(structure, formula):
            continue
        output = formula["output_ref"]
        if lines[output]["role"] != "diluted_eps_numerator":
            raise FinancialStatementStructureError(
                "only diluted EPS numerator may use note-backed replay"
            )
        divides = [
            item for item in formulas
            if item["operator"] == "divide" and item["numerator_ref"] == output
        ]
        if len(divides) != 1 or divides[0].get("tie_out_concept") is None:
            raise FinancialStatementStructureError(
                "note-backed numerator needs one filed diluted EPS divide tie"
            )
        divide = divides[0]
        denominator = lines[divide["denominator_ref"]]
        if denominator["kind"] != "filed":
            raise FinancialStatementStructureError(
                "note-backed diluted EPS denominator must be an exact filed line"
            )
        eps_line = filed[divide["tie_out_concept"]]
        formula_reports: list[dict[str, Any]] = []
        for evidence_ref in sorted(
            ref for ref in formula["evidence_refs"] if ref in notes
        ):
            evidence = notes[evidence_ref]
            accession = evidence["accession"]
            form = evidence["form"]
            for period in evidence["periods"]:
                start = period["period_start"]
                end = period["period_end"]
                identity = (output, start, end)
                if identity in seen:
                    raise FinancialStatementStructureError(
                        "note-backed numerator period authority is ambiguous"
                    )
                seen.add(identity)
                sources: list[dict[str, Any]] = []
                calculated = Decimal(0)
                missing = False
                for term in formula["terms"]:
                    term_line = lines[term["line_ref"]]
                    matches = _filed_period_facts(
                        filed[term_line["concept"]], period_start=start,
                        period_end=end, accession=accession, form=form,
                    )
                    if len(matches) != 1:
                        missing = True
                        break
                    fact = matches[0]
                    if str(fact.get("unit") or "").casefold() != lines[output]["unit"]:
                        raise FinancialStatementStructureError(
                            "note-backed numerator term unit differs"
                        )
                    calculated += _decimal(term["coefficient"], "coefficient") * _decimal(
                        fact.get("value"), "note-backed numerator term"
                    )
                    sources.append(fact)
                shares = _filed_period_facts(
                    filed[denominator["concept"]], period_start=start,
                    period_end=end, accession=accession, form=form,
                )
                eps = _filed_period_facts(
                    eps_line, period_start=start, period_end=end,
                    accession=accession, form=form,
                )
                if missing or len(shares) != 1 or len(eps) != 1:
                    formula_reports.append({
                        "period_start": start, "period_end": end,
                        "applicability_kind": evidence["applicability_kind"],
                        "evidence_ref": evidence_ref, "status": "unavailable",
                        "value": None,
                        "reason": "the exact statement period lacks one numerator term, shares, or EPS tie",
                    })
                    continue
                share_value = _decimal(shares[0].get("value"), "diluted shares")
                if (
                    str(shares[0].get("unit") or "").casefold() != "shares"
                    or share_value <= 0
                ):
                    raise FinancialStatementStructureError(
                        "note-backed diluted EPS shares must be positive shares"
                    )
                filed_eps = _decimal(eps[0].get("value"), "filed diluted EPS")
                if str(eps[0].get("unit") or "").casefold() != lines[divide["output_ref"]]["unit"]:
                    raise FinancialStatementStructureError(
                        "note-backed filed diluted EPS unit differs"
                    )
                quotient = calculated / share_value
                quantum = Decimal(1).scaleb(filed_eps.as_tuple().exponent)
                if quotient.quantize(quantum) != filed_eps:
                    raise FinancialStatementStructureError(
                        "note-backed diluted EPS numerator does not tie through filed EPS"
                    )
                formula_reports.append({
                    "period_start": start, "period_end": end,
                    "applicability_kind": evidence["applicability_kind"],
                    "evidence_ref": evidence_ref, "status": "validated",
                    "value": str(calculated), "unit": lines[output]["unit"],
                    "filed_diluted_eps": str(filed_eps),
                    "source_accessions": [accession], "reason": None,
                })
        reports.append({
            "output_ref": output,
            "status": (
                "validated" if formula_reports
                and all(item["status"] == "validated" for item in formula_reports)
                else "unavailable"
            ),
            "periods": formula_reports,
        })
    return reports


def _validate_formula_evidence(
    formulas: Sequence[Mapping[str, Any]], lines: Mapping[str, Mapping[str, Any]],
    filed: Mapping[str, Mapping[str, Any]], note_refs: set[str], *,
    include_duration: bool = False,
) -> None:
    by_output = {formula["output_ref"]: formula for formula in formulas}
    visiting: set[str] = set()
    memo: dict[str, set[str]] = {}

    def sources(ref: str) -> set[str]:
        if ref in memo:
            return memo[ref]
        if ref in visiting:
            raise FinancialStatementStructureError("formula graph contains a cycle")
        line = lines[ref]
        if line["kind"] == "filed":
            result = {
                str(source_ref)
                for cell in (
                    list((filed[line["concept"]].get("cells") or {}).values())
                    + (list(filed[line["concept"]].get("duration_facts") or [])
                       if include_duration else [])
                )
                if isinstance(cell, Mapping)
                for source_ref in (cell.get("source_accessions") or [])
            }
        else:
            visiting.add(ref)
            formula = by_output.get(ref)
            if formula is None:
                raise FinancialStatementStructureError(
                    "derived line has no formula evidence authority"
                )
            dependencies = (
                [term["line_ref"] for term in formula["terms"]]
                if formula["operator"] == "sum"
                else [formula["numerator_ref"], formula["denominator_ref"]]
            )
            result = set().union(*(sources(dependency) for dependency in dependencies))
            result.update(ref for ref in formula["evidence_refs"] if ref in note_refs)
            visiting.remove(ref)
        memo[ref] = result
        return result

    for formula in formulas:
        dependencies = (
            [term["line_ref"] for term in formula["terms"]]
            if formula["operator"] == "sum"
            else [formula["numerator_ref"], formula["denominator_ref"]]
        )
        allowed = note_refs | set().union(*(sources(ref) for ref in dependencies))
        if not set(formula["evidence_refs"]).issubset(allowed):
            raise FinancialStatementStructureError(
                f"formula {formula['output_ref']} cites evidence outside its operands"
            )


def _validate_typed_note_use(
    formulas: Sequence[Mapping[str, Any]], lines: Mapping[str, Mapping[str, Any]],
    notes: Mapping[str, Mapping[str, Any]],
) -> None:
    if not notes:
        return
    for evidence_ref in notes:
        uses = [formula for formula in formulas
                if evidence_ref in formula["evidence_refs"]]
        if len(uses) != 1:
            raise FinancialStatementStructureError(
                "typed financial note evidence must be cited by exactly one formula"
            )
        numerator = uses[0]
        if (
            numerator["operator"] != "sum"
            or numerator.get("tie_out_concept") is not None
            or lines[numerator["output_ref"]]["role"] != "diluted_eps_numerator"
        ):
            raise FinancialStatementStructureError(
                "typed financial note evidence is reserved for an untied diluted EPS numerator"
            )
        divides = [
            formula for formula in formulas
            if formula["operator"] == "divide"
            and formula["numerator_ref"] == numerator["output_ref"]
            and formula.get("tie_out_concept") is not None
        ]
        if len(divides) != 1:
            raise FinancialStatementStructureError(
                "note-backed numerator must feed one filed diluted EPS tie"
            )


def _validate_forecast_bases(lines: Sequence[Mapping[str, Any]]) -> None:
    by_ref = {line["ref"]: line for line in lines}
    for line in lines:
        if line["forecast_method"] not in BASED_FORECAST_METHODS and not (line["forecast_method"] == "roll_forward" and line.get("forecast_base_ref")):
            continue
        base = by_ref.get(line["forecast_base_ref"])
        if base is None or base["ref"] == line["ref"]:
            raise FinancialStatementStructureError(
                f"{line['forecast_method']} forecast base must name another structure line"
            )
        if line["forecast_method"] == "roll_forward":
            if base["role"] != "depreciable_assets":
                raise FinancialStatementStructureError("depreciation roll_forward needs its depreciable asset base")
            continue
        if line["forecast_method"] == "rate_times_balance":
            if (
                base["statement"] != "balance" or base["unit"] != line["unit"]
                or line["statement"] != "income"
            ):
                raise FinancialStatementStructureError(
                    "rate_times_balance needs an income line and a balance-sheet base "
                    "in the same currency"
                )
            continue
        # A balance-sheet line may be a ratio of a quarter's revenue (working
        # capital as a share of revenue); every other ratio keeps its kind.
        same_kind = base["period_kind"] == line["period_kind"] or (
            line["statement"] == "balance" and base["period_kind"] == "duration"
        )
        if base["unit"] != line["unit"] or not same_kind:
            raise FinancialStatementStructureError(
                "share_of_line forecast base must use the same unit and period kind"
            )


def _presentation_filed(state: Mapping[str, Any]) -> tuple[dict[str, dict[str, Any]], set[str]]:
    filings = state.get("filings")
    if not isinstance(filings, list) or not filings:
        raise FinancialStatementStructureError("company presentation has no filing authority")
    accessions = {
        _text(item.get("accession"), "company presentation accession")
        for item in filings if isinstance(item, Mapping)
    }
    if len(accessions) != len(filings):
        raise FinancialStatementStructureError("company presentation filing authority is ambiguous")
    statements = state.get("statements") or {}
    rows = [("income", raw) for raw in (statements.get("income") or [])] + [
        (name, raw) for name in ("cash", "balance")
        for raw in (statements.get(name) or [])
    ]
    filed: dict[str, dict[str, Any]] = {}
    for statement, raw in rows:
        if (
            not isinstance(raw, Mapping) or raw.get("is_breakdown")
            or raw.get("dimension_axis")
        ):
            continue
        concept = _text(raw.get("concept"), "company presentation concept")
        unit = _text(raw.get("unit"), "company presentation unit").casefold()
        period_kind = _text(
            raw.get("period_kind") or (
                "duration" if raw.get("period_start") else "instant"
            ),
            "company presentation period_kind",
        )
        candidate = {
            "status": "filed", "statement": statement,
            "period_basis": period_kind,
            "cells": {accession: {
                "unit": unit, "value": "1", "period_start": "2000-01-01",
                "source_accessions": sorted(accessions),
            } for accession in sorted(accessions)},
        }
        previous = filed.get(concept)
        if previous is not None and previous != candidate:
            if statement != "income":
                # A cash or balance concept that is ambiguous is simply not
                # offered as a leaf; the income presentation stays strict.
                continue
            raise FinancialStatementStructureError(
                f"company presentation concept {concept} is ambiguous"
            )
        filed[concept] = candidate
    if not filed:
        raise FinancialStatementStructureError(
            "company presentation has no consolidated income lines"
        )
    return filed, accessions


def _refuse_uncomputable_chain(
    lines: Sequence[Mapping[str, Any]], formulas: Sequence[Mapping[str, Any]],
) -> None:
    """Every formula term must be forecastable, or the proposal is refused.

    A filed earnings or cost total used as a term is tolerated here: the
    materialized completion replaces it by its tied sum of components once the
    filed history is bound.  Anything else that blocks a derived line is the
    drafter's to fix.
    """

    from .forecast_coverage import TOTAL_COST_CONCEPTS, forecast_computability

    blocked = forecast_computability(lines, formulas)
    by_ref = {line["ref"]: line for line in lines}
    repairable = {
        ref for ref, line in by_ref.items()
        if line["kind"] == "filed" and (
            line["role"] in _FORMULA_TOTAL_ROLES
            or line["role"] == _COMPANY_COMPONENT_ROLE
            or str(line.get("concept")) in TOTAL_COST_CONCEPTS
        )
    }
    for formula in formulas:
        output = formula["output_ref"]
        reason = blocked.get(output)
        if reason is None:
            continue
        leaf = reason.split(" ", 1)[0]
        if leaf in repairable:
            continue
        raise FinancialStatementStructureError(
            f"formula {output} cannot be forecast: {reason}; give every term a "
            "forecast method (see forecast_method)"
        )


def validate_structure_proposal(
    proposal: Mapping[str, Any], state: Mapping[str, Any], *,
    revenue_anchor_concept: str, expense_lines: Sequence[Mapping[str, Any]],
    note_evidence: Sequence[Mapping[str, Any]] = (),
    note_evidence_resolver: Callable[[str], Mapping[str, Any] | None] | None = None,
) -> dict[str, Any]:
    """Validate the formula definition inside a model-spec response."""

    body = _closed(proposal, {"schema_version", "lines", "formulas"},
                   "financial statement structure proposal")
    schema_version = body["schema_version"]
    if schema_version not in _STRUCTURE_AUTHORITY_REFS:
        raise FinancialStatementStructureError("unsupported structure proposal version")
    if is_coverage_structure(schema_version) and isinstance(body["lines"], list):
        # The code completes methods by role as soon as the drafter answers;
        # the history-dependent repairs run when the filings are bound.
        from .forecast_coverage import complete_methods_by_role

        body = complete_methods_by_role(body, spec_one_off=[
            str(item.get("basis_concept")) for item in expense_lines
            if isinstance(item, Mapping) and item.get("behaviour") == "one_off"
            and item.get("basis_concept")
        ])
    raw_lines = body["lines"]
    raw_formulas = body["formulas"]
    if (
        not isinstance(raw_lines, list) or not 1 <= len(raw_lines) <= MAX_STRUCTURE_LINES
        or not isinstance(raw_formulas, list)
        or not 1 <= len(raw_formulas) <= MAX_STRUCTURE_FORMULAS
    ):
        raise FinancialStatementStructureError("structure proposal exceeds its bounded shape")
    filed, accessions = _presentation_filed(state)
    lines = [
        _normalize_line(
            raw, index, filed, validate_values=False, schema_version=schema_version,
        )
        for index, raw in enumerate(raw_lines)
    ]
    by_ref = {line["ref"]: line for line in lines}
    if len(by_ref) != len(lines):
        raise FinancialStatementStructureError("structure line ref is duplicated")
    _validate_spec_alignment({
        "revenue_anchor_concept": revenue_anchor_concept,
        "expense_lines": list(expense_lines),
    }, lines)
    _validate_forecast_bases(lines)
    notes = _normalized_note_evidence(
        note_evidence, note_evidence_resolver, schema_version=schema_version,
    )
    if schema_version in _TYPED_NOTE_STRUCTURE_VERSIONS and any(
        item["company_ref"] != state.get("company_ref") for item in notes
    ):
        raise FinancialStatementStructureError(
            "note evidence company differs from company presentation"
        )
    if schema_version in _TYPED_NOTE_STRUCTURE_VERSIONS:
        numeric = state.get("numeric_context")
        filings = (
            numeric.get("filing_authorities")
            if isinstance(numeric, Mapping)
            and isinstance(numeric.get("filing_authorities"), list)
            else state.get("filings") or []
        )
        for note in notes:
            matches = [
                item for item in filings
                if isinstance(item, Mapping) and item.get("accession") == note["accession"]
            ]
            if len(matches) != 1 or any(
                matches[0].get(field) != note[note_field]
                for field, note_field in (
                    ("ingest_id", "statement_ingest_ref"),
                    ("content_hash", "statement_filing_hash"),
                    ("form", "form"),
                )
            ):
                raise FinancialStatementStructureError(
                    "note evidence statement filing differs from company presentation"
                )
    note_by_ref = {item["ref"]: item for item in notes}
    formulas = [
        _normalize_formula(
            raw, index, by_ref, filed, accessions | set(note_by_ref),
            schema_version=schema_version, note_evidence=note_by_ref,
        )
        for index, raw in enumerate(raw_formulas)
    ]
    outputs = [formula["output_ref"] for formula in formulas]
    derived = sorted(ref for ref, line in by_ref.items() if line["kind"] == "derived")
    if sorted(outputs) != derived or len(outputs) != len(set(outputs)):
        raise FinancialStatementStructureError(
            "every derived line must have exactly one formula"
        )
    if not any(
        line["kind"] == "derived" and line["role"] in _FINAL_EARNINGS_ROLES
        for line in lines
    ):
        raise FinancialStatementStructureError(
            "structure needs a formula-derived filed final earnings result"
        )
    _validate_formula_evidence(formulas, by_ref, filed, set(note_by_ref))
    _validate_typed_note_use(formulas, by_ref, note_by_ref)
    if is_coverage_structure(schema_version):
        _refuse_uncomputable_chain(lines, formulas)
    result = {"schema_version": schema_version, "lines": lines, "formulas": formulas}
    if schema_version in _TYPED_NOTE_STRUCTURE_VERSIONS and notes:
        result["note_evidence"] = notes
    return result


def replay_historical_structure(
    structure: Mapping[str, Any], financial_inputs: Mapping[str, Any],
) -> dict[str, Any]:
    """Replay every equation on complete periods; never fill a missing term."""

    filed = _filed_index(financial_inputs)
    lines = {line["ref"]: line for line in structure["lines"]}
    values: dict[str, dict[tuple[str | None, str], Decimal]] = {
        ref: _period_cells(filed[line["concept"]])
        for ref, line in lines.items() if line["kind"] == "filed"
    }
    note_formula_reports = _note_backed_eps_replay(structure, filed)
    note_reports_by_output = {
        item["output_ref"]: item for item in note_formula_reports
    }
    reports: list[dict[str, Any]] = []
    pending = {formula["output_ref"]: formula for formula in structure["formulas"]}
    formula_by_output = dict(pending)
    while pending:
        progressed = False
        for output, formula in list(pending.items()):
            dependencies = (
                [term["line_ref"] for term in formula["terms"]]
                if formula["operator"] == "sum"
                else [formula["numerator_ref"], formula["denominator_ref"]]
            )
            if any(ref not in values for ref in dependencies):
                continue
            periods = set(values[dependencies[0]])
            for ref in dependencies[1:]:
                periods &= set(values[ref])
            calculated: dict[tuple[str | None, str], Decimal] = {}
            for period in sorted(periods, key=lambda item: (item[1], item[0] or "")):
                if formula["operator"] == "sum":
                    calculated[period] = sum(
                        (_decimal(term["coefficient"], "coefficient")
                         * values[term["line_ref"]][period]
                         for term in formula["terms"]),
                        Decimal(0),
                    )
                else:
                    divisor = values[formula["denominator_ref"]][period]
                    if divisor == 0:
                        continue
                    calculated[period] = values[formula["numerator_ref"]][period] / divisor
            candidate_periods = set(calculated)
            note_report = note_reports_by_output.get(output)
            note_quarters = {
                (item["period_start"], item["period_end"])
                for item in ((note_report or {}).get("periods") or [])
                if item["applicability_kind"] == "quarter"
                and item["status"] == "validated"
            }
            candidate_quarters = {
                period for period in candidate_periods
                if period[0] is not None
                and QUARTER_MIN_DAYS <= (
                    date.fromisoformat(period[1])
                    - date.fromisoformat(period[0])
                ).days + 1 <= QUARTER_MAX_DAYS
            }
            if formula.get("tie_out_concept") is None and not _eps_proven(
                    structure, formula):
                # An annual note may prove the annual numerator, but it does
                # not authorize applying that relationship to a quarter.  A
                # dependent formula can see only independently validated note
                # quarter windows.
                calculated = {
                    period: value for period, value in calculated.items()
                    if period in note_quarters
                }
            values[output] = calculated
            tie = formula["tie_out_concept"]
            tied = {} if tie is None else _period_cells(filed[tie])
            tested = sorted(set(calculated) & set(tied), key=lambda item: item[1])
            note_validated = (
                bool(candidate_quarters)
                and candidate_quarters == note_quarters
                and set(calculated) == note_quarters
            )
            rounded_periods: set[tuple[str | None, str]] = set()

            def matches(period: tuple[str | None, str]) -> bool:
                if formula["operator"] != "divide":
                    if calculated[period] == tied[period]:
                        return True
                    if not is_coverage_structure(structure.get("schema_version")):
                        return False
                    # 0.6: a company that rounds every presented line (IBM
                    # files millions) cannot tie its own totals exactly.  The
                    # bound comes from the filed precision of that period.
                    leaves = _filed_leaves(output, lines, formula_by_output)
                    leaf_values = [values[leaf][period] for leaf in leaves
                                   if period in values.get(leaf, {})]
                    if len(leaf_values) != len(leaves):
                        return False
                    if presentation_rounding_ties(
                            calculated[period], tied[period], leaf_values):
                        rounded_periods.add(period)
                        return True
                    return False
                # Filed per-share values are rounded disclosures.  Use the
                # exponent the company actually filed for that period rather
                # than inventing one global EPS precision.
                quantum = Decimal(1).scaleb(tied[period].as_tuple().exponent)
                return calculated[period].quantize(quantum) == tied[period]
            mismatches = [
                {"period_start": period[0], "period_end": period[1],
                 "calculated": str(calculated[period]), "filed": str(tied[period])}
                for period in tested if not matches(period)
            ]
            if mismatches:
                raise FinancialStatementStructureError(
                    f"formula {output} does not tie to filed history"
                )
            reports.append({
                "output_ref": output,
                **({"tie_mode": ("presentation_rounding" if rounded_periods & set(tested)
                                 else "exact")}
                   if is_coverage_structure(structure.get("schema_version")) else {}),
                "status": "validated" if tested or note_validated else "unavailable",
                "tested_periods": [
                    {"period_start": period[0], "period_end": period[1]}
                    for period in (tested or sorted(note_quarters, key=lambda item: item[1]))
                ],
                "reason": None if tested or note_validated else (
                    "typed note evidence does not authorize every complete operand period"
                    if note_report is not None
                    else "no complete historical period has every formula term and filed tie-out"
                ),
            })
            del pending[output]
            progressed = True
        if not progressed:
            raise FinancialStatementStructureError("formula graph contains a cycle")
    for formula in structure["formulas"]:
        if not _eps_proven(structure, formula):
            continue
        output = formula["output_ref"]
        divides = [
            item for item in structure["formulas"]
            if item["operator"] == "divide" and item.get("numerator_ref") == output
            and item.get("tie_out_concept") is not None
        ]
        divide_reports = [item for item in reports
                          if divides and item["output_ref"] == divides[0]["output_ref"]]
        proven = (
            len(divides) == 1 and len(divide_reports) == 1
            and divide_reports[0]["status"] == "validated"
            and bool(divide_reports[0]["tested_periods"])
        )
        for item in reports:
            if item["output_ref"] == output:
                item.update({
                    "status": "validated" if proven else "unavailable",
                    "tested_periods": (list(divide_reports[0]["tested_periods"])
                                       if proven else []),
                    "reason": (
                        "proven period by period through the filed diluted EPS tie"
                        if proven else
                        "an untied numerator needs one filed diluted EPS divide that ties"
                    ),
                })
    forecast_reports: list[dict[str, Any]] = []
    annual_ready = True
    coverage_reasons = {
        str(item.get("line_ref")): item.get("reason")
        for item in (structure.get("coverage") or []) if isinstance(item, Mapping)
    }
    for ref, line in sorted(lines.items()):
        method = line["forecast_method"]
        if method == "formula":
            report = next(item for item in reports if item["output_ref"] == ref)
            forecast_reports.append({
                "line_ref": ref, "method": method, "base_ref": None,
                "status": report["status"], "observations": [],
                "reason": report["reason"],
            })
            if structure.get("schema_version") in _ANNUAL_STRUCTURE_VERSIONS:
                forecast_reports[-1]["annual_method"] = line.get(
                    "annual_forecast_method"
                )
            continue
        if method == "unavailable":
            forecast_reports.append({
                "line_ref": ref, "method": method, "base_ref": None,
                "status": "unavailable", "observations": [],
                "reason": coverage_reasons.get(ref) or (
                    "the company spec selected no forecast basis for this filed line"),
            })
            if structure.get("schema_version") in _ANNUAL_STRUCTURE_VERSIONS:
                forecast_reports[-1]["annual_method"] = line.get(
                    "annual_forecast_method"
                )
            continue
        cells = values[ref]
        observations: list[dict[str, Any]] = []
        if method in {"carry_forward_mean", "hold_last", "roll_forward"}:
            ordered = sorted(cells.items(), key=lambda item: item[0][1])
            observations = [
                {"period_start": period[0], "period_end": period[1], "value": str(value)}
                for period, value in ordered
            ][-(1 if method != "carry_forward_mean" else 4):]
        elif method == "zero_one_off":
            forecast_reports.append({
                "line_ref": ref, "method": method, "base_ref": None,
                "status": "validated", "observations": [],
                "reason": coverage_reasons.get(ref) or "one-off line forecast at zero",
            })
            if structure.get("schema_version") in _ANNUAL_STRUCTURE_VERSIONS:
                forecast_reports[-1]["annual_method"] = line.get("annual_forecast_method")
            continue
        elif method in {"year_over_year_growth", "seasonal_factor"}:
            from .forecast_seasonality import year_ago
            by_end = {period[1]: value for period, value in cells.items()}
            for end, value in sorted(by_end.items()):
                prior = year_ago(end, by_end)
                if prior and by_end[prior] != 0:
                    observations.append({"period_start": prior, "period_end": end,
                                         "value": str(value / by_end[prior] - 1)})
        elif method == "quarterly_growth":
            ordered = sorted(cells.items(), key=lambda item: item[0][1])
            for (prior_period, prior), (current_period, current) in zip(
                ordered, ordered[1:]
            ):
                gap = _quarter_gap(prior_period[1], current_period[1])
                if 80 <= gap <= 100 and prior != 0:
                    observations.append({
                        "period_start": prior_period[1],
                        "period_end": current_period[1],
                        "value": str((current - prior) / prior),
                    })
        elif is_coverage_structure(structure.get("schema_version")):
            # Ratios may pair an instant with a duration (a balance as a share
            # of the quarter's revenue, interest over a balance): match on the
            # period end.
            base_ref = line["forecast_base_ref"]
            base_by_end = {period[1]: value for period, value in values[base_ref].items()}
            for period in sorted(cells, key=lambda item: item[1]):
                base = base_by_end.get(period[1])
                if base is not None and base != 0:
                    observations.append({
                        "period_start": period[0], "period_end": period[1],
                        "value": str(cells[period] / base),
                    })
        else:
            base_ref = line["forecast_base_ref"]
            base_cells = values[base_ref]
            for period in sorted(set(cells) & set(base_cells), key=lambda item: item[1]):
                if base_cells[period] != 0:
                    observations.append({
                        "period_start": period[0], "period_end": period[1],
                        "value": str(cells[period] / base_cells[period]),
                    })
        observations = observations[-4:]
        forecast_reports.append({
            "line_ref": ref, "method": method,
            "base_ref": line["forecast_base_ref"],
            "status": "validated" if observations else "unavailable",
            "observations": observations,
            "reason": None if observations else (
                "historical inputs contain no complete nonzero forecast-measure pair"
            ),
        })
        if structure.get("schema_version") in _ANNUAL_STRUCTURE_VERSIONS:
            forecast_reports[-1]["annual_method"] = line.get(
                "annual_forecast_method"
            )
        if (
            structure.get("schema_version") in _ANNUAL_STRUCTURE_VERSIONS
            and line["role"] == "diluted_weighted_average_shares"
        ):
            annual_method = str(line["annual_forecast_method"])
            annual_report = (
                _historical_annual_share_replay(filed[line["concept"]])
                if annual_method == "day_weighted_quarters"
                else {"status": "unavailable", "observations": [],
                      "reason": "annual diluted-share forecast is explicitly unavailable"}
            )
            if annual_method == "annual_growth_from_filed":
                from .annual_share_forecast import annual_share_source
                annual_report = annual_share_source(filed[line["concept"]])
            forecast_reports[-1].update({
                "annual_status": annual_report["status"],
                "annual_observations": annual_report["observations"],
                "annual_reason": annual_report["reason"],
            })
            if annual_method == "day_weighted_quarters":
                annual_ready = annual_report["status"] == "validated"
            elif annual_method == "annual_growth_from_filed":
                forecast_reports[-1]["annual_forecast_source"] = annual_report
                annual_ready = annual_report["status"] == "validated"
    result = {
        "schema_version": (
            "financial-statement-structure-replay-0.6"
            if structure.get("schema_version") == COVERAGE_SCHEMA_VERSION
            else "financial-statement-structure-replay-0.5"
            if structure.get("schema_version") == REVENUE_PRETAX_SCHEMA_VERSION
            else "financial-statement-structure-replay-0.4"
            if structure.get("schema_version") == DIRECT_GROSS_PRETAX_SCHEMA_VERSION
            else "financial-statement-structure-replay-0.3"
            if structure.get("schema_version") == TYPED_NOTE_SCHEMA_VERSION
            else "financial-statement-structure-replay-0.2"
            if structure.get("schema_version") == ANNUAL_SCHEMA_VERSION
            else "financial-statement-structure-replay-0.1"
        ),
        "structure_hash": structure["content_hash"],
        "formulas": sorted(reports, key=lambda item: item["output_ref"]),
        "forecast_methods": forecast_reports,
        "ready_for_forecast": annual_ready and all(
            report["status"] == "validated" for report in reports
        ),
    }
    if structure.get("schema_version") in _TYPED_NOTE_STRUCTURE_VERSIONS:
        result["note_formula_periods"] = note_formula_reports
    if is_coverage_structure(structure.get("schema_version")):
        from .forecast_coverage import forecast_computability

        result["forecast_computability"] = forecast_computability(
            structure["lines"], structure["formulas"])
    return result


def validate_financial_statement_structure(
    proposal: Mapping[str, Any], company_spec: Mapping[str, Any],
    financial_inputs: Mapping[str, Any], *,
    note_evidence: Sequence[Mapping[str, Any]] = (),
    note_evidence_resolver: Callable[[str], Mapping[str, Any] | None] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Validate a company-spec extension and return it with replay proof."""

    fields = {
        "schema_version", "structure_ref", "company_ref", "spec_ref", "spec_hash",
        "financial_input_hash", "lines", "formulas", "created_by",
    }
    if isinstance(proposal, Mapping) and is_coverage_structure(
            proposal.get("schema_version")):
        # The code-written provenance is present on a materialized 0.6
        # structure; a hand-built one without it describes itself as drafted.
        if not any(name in proposal for name in _COVERAGE_STRUCTURE_FIELDS):
            proposal = {**dict(proposal), "auto_repairs": [], "coverage": None}
        fields = fields | _COVERAGE_STRUCTURE_FIELDS
    body = _closed(proposal, fields, "financial statement structure")
    authority = financial_input_authority(financial_inputs)
    spec_authority = _spec_authority(company_spec)
    schema_version = body["schema_version"]
    if schema_version not in _STRUCTURE_AUTHORITY_REFS:
        raise FinancialStatementStructureError("unsupported structure schema_version")
    if body["company_ref"] != authority["company_ref"]:
        raise FinancialStatementStructureError("structure company differs from financial inputs")
    if body["company_ref"] != spec_authority["company_ref"]:
        raise FinancialStatementStructureError("structure company differs from company spec")
    if (
        body["spec_ref"] != spec_authority["spec_ref"]
        or body["spec_hash"] != spec_authority["spec_hash"]
        or financial_inputs.get("spec_ref") != spec_authority["spec_ref"]
    ):
        raise FinancialStatementStructureError("structure company spec authority drifted")
    if body["financial_input_hash"] != authority["content_hash"]:
        raise FinancialStatementStructureError("structure financial input authority drifted")
    _text(body["structure_ref"], "structure_ref")
    actor = _text(body["created_by"], "created_by")
    if not actor.startswith(("human:", "automation:")):
        raise FinancialStatementStructureError("created_by must use human: or automation:")
    filed = _filed_index(financial_inputs)
    raw_lines = body["lines"]
    if not isinstance(raw_lines, list) or not raw_lines:
        raise FinancialStatementStructureError("structure needs at least one line")
    lines = [
        _normalize_line(raw, index, filed, schema_version=schema_version)
        for index, raw in enumerate(raw_lines)
    ]
    by_ref = {line["ref"]: line for line in lines}
    if len(by_ref) != len(lines):
        raise FinancialStatementStructureError("structure line ref is duplicated")
    _validate_spec_alignment(company_spec, lines)
    _validate_forecast_bases(lines)
    notes = _normalized_note_evidence(
        note_evidence, note_evidence_resolver, schema_version=schema_version,
    )
    if schema_version in _TYPED_NOTE_STRUCTURE_VERSIONS and any(
        item["company_ref"] != body["company_ref"] for item in notes
    ):
        raise FinancialStatementStructureError(
            "note evidence company differs from financial statement structure"
        )
    note_refs = {item["ref"] for item in notes}
    note_by_ref = {item["ref"]: item for item in notes}
    allowed_evidence = _statement_refs(
        financial_inputs, include_duration=schema_version in _TYPED_NOTE_STRUCTURE_VERSIONS,
    ) | note_refs
    raw_formulas = body["formulas"]
    if not isinstance(raw_formulas, list) or not raw_formulas:
        raise FinancialStatementStructureError(
            "structure needs at least one formula and a tied final earnings result"
        )
    formulas = [
        _normalize_formula(
            raw, index, by_ref, filed, allowed_evidence,
            schema_version=schema_version, note_evidence=note_by_ref,
        )
        for index, raw in enumerate(raw_formulas)
    ]
    outputs = [formula["output_ref"] for formula in formulas]
    derived = sorted(ref for ref, line in by_ref.items() if line["kind"] == "derived")
    if sorted(outputs) != derived or len(outputs) != len(set(outputs)):
        raise FinancialStatementStructureError(
            "every derived line must have exactly one formula"
        )
    if not any(
        line["kind"] == "derived" and line["role"] in _FINAL_EARNINGS_ROLES
        for line in lines
    ):
        raise FinancialStatementStructureError(
            "structure needs a formula-derived filed final earnings result"
        )
    _validate_formula_evidence(
        formulas, by_ref, filed, note_refs,
        include_duration=schema_version in _TYPED_NOTE_STRUCTURE_VERSIONS,
    )
    if schema_version in _TYPED_NOTE_STRUCTURE_VERSIONS:
        _validate_typed_note_use(formulas, by_ref, note_by_ref)
    normalized = {
        "schema_version": schema_version,
        "authority_ref": _STRUCTURE_AUTHORITY_REFS[schema_version],
        "structure_ref": body["structure_ref"],
        "company_ref": body["company_ref"],
        "spec_ref": body["spec_ref"],
        "spec_hash": body["spec_hash"],
        "financial_input_hash": body["financial_input_hash"],
        "lines": lines,
        "formulas": formulas,
        "note_evidence": notes,
        "created_by": actor,
    }
    if is_coverage_structure(schema_version):
        normalized["auto_repairs"] = _coverage_list(
            body["auto_repairs"], "auto_repairs",
            {"kind", "line_ref", "detail", "tie_out_concept"})
        normalized["coverage"] = _coverage_list(
            body["coverage"] if body["coverage"] is not None else [
                {"line_ref": line["ref"], "statement": line["statement"],
                 "method": line["forecast_method"], "history_quarters": None,
                 "source": "drafted", "reason": None}
                for line in lines
            ], "coverage",
            {"line_ref", "statement", "method", "history_quarters", "source", "reason"})
        if [item["line_ref"] for item in normalized["coverage"]] != [
                line["ref"] for line in lines]:
            raise FinancialStatementStructureError(
                "structure coverage must describe every line in order")
    normalized["content_hash"] = content_hash(normalized)
    replay = replay_historical_structure(normalized, financial_inputs)
    return normalized, replay


def _coverage_list(value: Any, name: str, fields: set[str]) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise FinancialStatementStructureError(f"structure {name} must be a list")
    return [_closed(item, fields, f"{name}[{index}]") for index, item in enumerate(value)]


def materialize_financial_statement_structure(
    company_spec: Mapping[str, Any], financial_inputs: Mapping[str, Any], *,
    note_evidence: Sequence[Mapping[str, Any]] = (),
    note_evidence_resolver: Callable[[str], Mapping[str, Any] | None] | None = None,
    complete: bool = True,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Bind a persisted spec definition to the current filed financial inputs.

    A 0.3-0.6 definition is first completed and repaired by
    ``forecast_coverage`` (structure 0.6).  The persisted spec definition is
    unchanged; the bound structure is the completed form, so a new coverage
    rule reaches every company without a re-drafted spec.  Should the
    completed form ever fail validation, the drafted definition is bound as
    before -- the completion may add coverage, never remove a working model.
    """

    definition = company_spec.get("financial_statement_structure")
    if not isinstance(definition, Mapping):
        raise FinancialStatementStructureError(
            "company spec has no financial statement structure definition"
        )
    if definition.get("schema_version") not in _STRUCTURE_AUTHORITY_REFS:
        raise FinancialStatementStructureError(
            "company spec has an unsupported statement structure definition"
        )
    from .forecast_coverage import (
        UPGRADEABLE_STRUCTURE_VERSIONS, complete_structure_definition,
    )

    if complete and definition.get("schema_version") in UPGRADEABLE_STRUCTURE_VERSIONS:
        try:
            completed = complete_structure_definition(
                definition, company_spec=company_spec, financial_inputs=financial_inputs)
            return _materialize_definition(
                company_spec, financial_inputs, definition, completed,
                note_evidence=note_evidence, note_evidence_resolver=note_evidence_resolver)
        except (FinancialStatementStructureError, ValueError, KeyError, TypeError,
                ArithmeticError):
            pass
    return _materialize_definition(
        company_spec, financial_inputs, definition, None,
        note_evidence=note_evidence, note_evidence_resolver=note_evidence_resolver)


def _materialize_definition(
    company_spec: Mapping[str, Any], financial_inputs: Mapping[str, Any],
    definition: Mapping[str, Any], completed: Mapping[str, Any] | None, *,
    note_evidence: Sequence[Mapping[str, Any]],
    note_evidence_resolver: Callable[[str], Mapping[str, Any] | None] | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    authority = financial_input_authority(financial_inputs)
    schema_version = (completed or definition).get("schema_version")
    effective_note_evidence = list(note_evidence)
    if schema_version in _TYPED_NOTE_STRUCTURE_VERSIONS:
        held_notes = definition.get("note_evidence")
        if held_notes is None:
            held_notes = []
        if not isinstance(held_notes, list):
            raise FinancialStatementStructureError(
                "typed-note statement structure evidence must be a list"
            )
        if effective_note_evidence and effective_note_evidence != held_notes:
            raise FinancialStatementStructureError(
                "caller note evidence differs from the persisted structure definition"
            )
        effective_note_evidence = list(held_notes)
    identity = {
        "schema_version": schema_version,
        "spec_ref": company_spec.get("spec_id"),
        "spec_hash": company_spec.get("content_hash"),
        "definition_hash": content_hash(definition),
        "financial_input_hash": authority["content_hash"],
    }
    proposal = {
        "schema_version": schema_version,
        "structure_ref": "financial-statement-structure:" + content_hash(identity)[:32],
        "company_ref": company_spec.get("company_ref"),
        "spec_ref": company_spec.get("spec_id"),
        "spec_hash": company_spec.get("content_hash"),
        "financial_input_hash": authority["content_hash"],
        "lines": (completed or definition).get("lines"),
        "formulas": (completed or definition).get("formulas"),
        "created_by": company_spec.get("decided_by"),
    }
    if completed is not None:
        proposal["auto_repairs"] = completed["auto_repairs"]
        proposal["coverage"] = completed["coverage"]
    return validate_financial_statement_structure(
        proposal, company_spec, financial_inputs,
        note_evidence=effective_note_evidence,
        note_evidence_resolver=note_evidence_resolver,
    )


def _quarter_gap(left: str, right: str) -> int:
    return (date.fromisoformat(right) - date.fromisoformat(left)).days


def _inclusive_days(cell: Mapping[str, Any]) -> int:
    try:
        return (
            date.fromisoformat(str(cell.get("period_end")))
            - date.fromisoformat(str(cell.get("period_start")))
        ).days + 1
    except ValueError:
        return 0


def day_weighted_annual_shares(
    quarter_cells: Sequence[Mapping[str, Any]],
    *, direct_annual_cell: Mapping[str, Any] | None = None,
    required_calendar: str | None = None,
    required_definition_ref: str | None = None,
) -> dict[str, Any]:
    """Weight four positive quarter share averages by their exact day spans.

    A direct annual filed cell is optional for forecast calculation and
    required by the structure replay that authorizes this method historically.
    """

    quarters = sorted((dict(cell) for cell in quarter_cells),
                      key=lambda cell: str(cell.get("period_end")))
    units = {str(cell.get("unit") or "").casefold() for cell in quarters}
    days = [_inclusive_days(cell) for cell in quarters]
    contiguous = all(
        _quarter_gap(str(left.get("period_end")), str(right.get("period_start"))) == 1
        for left, right in zip(quarters, quarters[1:])
    )
    values = [_decimal(cell.get("value"), "quarter diluted shares") for cell in quarters]
    authority_bound = all(
        cell.get("calendar") == required_calendar
        and cell.get("definition_ref") == required_definition_ref
        for cell in quarters
    ) if required_calendar is not None or required_definition_ref is not None else True
    if (
        len(quarters) != 4 or units != {"shares"}
        or any(not 80 <= count <= 100 for count in days)
        or not contiguous or any(value <= 0 for value in values)
        or not authority_bound
    ):
        return {"status": "unavailable", "value": None,
                "reason": ("annual diluted shares need four contiguous positive "
                           "quarter averages with exact day spans")}
    calculated = sum(
        (value * Decimal(count) for value, count in zip(values, days)), Decimal(0)
    ) / Decimal(sum(days))
    if direct_annual_cell is not None:
        direct = dict(direct_annual_cell)
        if (
            str(direct.get("unit") or "").casefold() != "shares"
            or _inclusive_days(direct) <= NINE_MONTH_MAX_DAYS
            or _inclusive_days(direct) > ANNUAL_MAX_DAYS
            or direct.get("period_start") != quarters[0].get("period_start")
            or direct.get("period_end") != quarters[-1].get("period_end")
            or (required_calendar is not None
                and direct.get("calendar") != required_calendar)
            or (required_definition_ref is not None
                and direct.get("definition_ref") != required_definition_ref)
        ):
            return {"status": "unavailable", "value": None,
                    "reason": "direct annual diluted shares use a different fiscal window"}
        disclosed = _decimal(direct.get("value"), "direct annual diluted shares")
        if disclosed <= 0:
            return {"status": "unavailable", "value": None,
                    "reason": "direct annual diluted shares are not positive"}
        quantum = Decimal(1).scaleb(disclosed.as_tuple().exponent)
        if calculated.quantize(quantum) != disclosed:
            return {"status": "unavailable", "value": None,
                    "reason": "day-weighted quarters do not tie to direct annual diluted shares"}
    return {
        "status": "computed", "value": str(calculated), "unit": "shares",
        "source_periods": quarters + ([dict(direct_annual_cell)]
                                      if direct_annual_cell is not None else []),
    }


def _historical_annual_share_replay(line: Mapping[str, Any]) -> dict[str, Any]:
    facts = [dict(item) for item in (line.get("duration_facts") or [])
             if isinstance(item, Mapping)]
    annual = [item for item in facts
              if NINE_MONTH_MAX_DAYS < _inclusive_days(item) <= ANNUAL_MAX_DAYS]
    annual_ends = {
        str(item.get("period_end")) for item in annual
        if sum(str(other.get("period_end")) == str(item.get("period_end"))
               for other in annual) == 1
    }
    observations: list[dict[str, Any]] = []
    for direct in annual:
        if str(direct.get("period_end")) not in annual_ends:
            continue
        quarters = [
            item for item in facts
            if item.get("period_kind") == "quarter"
            and str(direct.get("period_start")) <= str(item.get("period_start"))
            and str(item.get("period_end")) <= str(direct.get("period_end"))
        ]
        tie = day_weighted_annual_shares(quarters, direct_annual_cell=direct)
        if tie["status"] == "computed":
            observations.append({
                "period_start": direct.get("period_start"),
                "period_end": direct.get("period_end"),
                "value": tie["value"],
                "source_accessions": sorted({
                    str(ref) for item in tie["source_periods"]
                    for ref in (item.get("source_accessions") or [])
                }),
            })
    return {
        "status": "validated" if observations else "unavailable",
        "observations": observations,
        "reason": None if observations else (
            "no four-quarter day-weighted share history ties to a direct annual filing"
        ),
    }


def aggregate_fiscal_year(
    cells: Sequence[Mapping[str, Any]], *, semantic: str, fiscal_year: str,
) -> dict[str, Any]:
    """Apply closed annual semantics without inventing missing quarters/shares."""

    selected = [dict(cell) for cell in cells if cell.get("fiscal_year") == fiscal_year]
    if semantic == "sum_quarters":
        quarters = sorted(
            (cell for cell in selected if cell.get("period_kind") == "quarter"),
            key=lambda cell: str(cell.get("period_end")),
        )
        units = {str(cell.get("unit")).casefold() for cell in quarters}
        calendars = {str(cell.get("calendar") or "") for cell in quarters}
        definitions = {str(cell.get("definition_ref") or "") for cell in quarters}
        complete_periods = all(
            isinstance(cell.get("period_start"), str)
            and isinstance(cell.get("period_end"), str)
            and 80 <= _quarter_gap(
                str(cell["period_start"]), str(cell["period_end"])
            ) <= 100
            for cell in quarters
        )
        contiguous = all(
            _quarter_gap(str(left["period_end"]), str(right["period_start"])) == 1
            for left, right in zip(quarters, quarters[1:])
        )
        if (
            len(quarters) != 4 or len(units) != 1
            or calendars == {""} or len(calendars) != 1
            or definitions == {""} or len(definitions) != 1
            or not complete_periods or not contiguous
        ):
            return {"status": "unavailable", "value": None,
                    "reason": ("a fiscal-year amount needs four contiguous quarters "
                               "with one calendar, definition, and unit")}
        return {"status": "computed", "value": str(sum(
                    (_decimal(cell.get("value"), "quarter value") for cell in quarters),
                    Decimal(0))), "unit": units.pop(), "source_periods": quarters}
    if semantic == "direct_annual":
        annual = [cell for cell in selected if cell.get("period_kind") == "annual"]
        if (
            len(annual) != 1
            or not isinstance(annual[0].get("period_start"), str)
            or not isinstance(annual[0].get("period_end"), str)
            or not NINE_MONTH_MAX_DAYS < _quarter_gap(
                str(annual[0].get("period_start")), str(annual[0].get("period_end"))
            ) <= ANNUAL_MAX_DAYS
            or not isinstance(annual[0].get("calendar"), str)
            or not annual[0].get("calendar")
            or not isinstance(annual[0].get("definition_ref"), str)
            or not annual[0].get("definition_ref")
        ):
            return {"status": "unavailable", "value": None,
                    "reason": ("this line requires one directly filed annual value "
                               "with an exact calendar and definition")}
        return {"status": "computed", "value": str(_decimal(annual[0].get("value"),
                                                               "annual value")),
                "unit": str(annual[0].get("unit")).casefold(),
                "source_periods": annual}
    raise FinancialStatementStructureError("annual semantic is invalid for aggregation")


def annual_diluted_eps(
    *, diluted_eps_numerator_cells: Sequence[Mapping[str, Any]],
    diluted_weighted_share_cells: Sequence[Mapping[str, Any]], fiscal_year: str,
    diluted_eps_cells: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """Annual disclosed diluted-EPS numerator divided by annual diluted shares."""

    quarterly_income = aggregate_fiscal_year(
        diluted_eps_numerator_cells, semantic="sum_quarters", fiscal_year=fiscal_year,
    )
    direct_income = aggregate_fiscal_year(
        diluted_eps_numerator_cells, semantic="direct_annual", fiscal_year=fiscal_year,
    )
    has_direct_income = any(
        cell.get("fiscal_year") == fiscal_year and cell.get("period_kind") == "annual"
        for cell in diluted_eps_numerator_cells
    )
    if has_direct_income and direct_income["status"] != "computed":
        return {"status": "unavailable", "value": None,
                "reason": "direct annual diluted-EPS numerator authority is ambiguous"}
    if direct_income["status"] == "computed":
        if (
            quarterly_income["status"] == "computed"
            and (
                quarterly_income["unit"] != direct_income["unit"]
                or _decimal(quarterly_income["value"], "quarterly annual numerator")
                != _decimal(direct_income["value"], "direct annual numerator")
            )
        ):
            return {"status": "unavailable", "value": None,
                    "reason": ("direct annual diluted-EPS numerator disagrees with "
                               "the four disclosed quarters")}
        income = direct_income
    else:
        income = quarterly_income
    shares = aggregate_fiscal_year(
        diluted_weighted_share_cells, semantic="direct_annual", fiscal_year=fiscal_year,
    )
    if income["status"] != "computed" or shares["status"] != "computed":
        return {"status": "unavailable", "value": None,
                "reason": ("annual diluted EPS needs its complete disclosed numerator "
                           "and direct annual weighted shares")}
    if not isinstance(income.get("unit"), str) or not _is_currency_unit(income["unit"]):
        return {"status": "unavailable", "value": None,
                "reason": "annual diluted EPS numerator must use an ISO-4217 currency unit"}
    if shares.get("unit") != "shares":
        return {"status": "unavailable", "value": None,
                "reason": "annual diluted weighted shares must use shares"}
    denominator = _decimal(shares["value"], "annual diluted weighted shares")
    income_calendars = {cell["calendar"] for cell in income["source_periods"]}
    share_period = shares["source_periods"][0]
    share_calendar = share_period["calendar"]
    income_start = min(str(cell["period_start"]) for cell in income["source_periods"])
    income_end = max(str(cell["period_end"]) for cell in income["source_periods"])
    if (
        income_calendars != {share_calendar}
        or share_period["period_start"] != income_start
        or share_period["period_end"] != income_end
    ):
        return {"status": "unavailable", "value": None,
                "reason": ("annual diluted EPS numerator and weighted shares use "
                           "different fiscal windows")}
    if denominator <= 0:
        return {"status": "unavailable", "value": None,
                "reason": "annual diluted weighted shares are not positive"}
    calculated = _decimal(income["value"], "annual diluted-EPS numerator") / denominator
    filed_eps = aggregate_fiscal_year(
        diluted_eps_cells, semantic="direct_annual", fiscal_year=fiscal_year,
    )
    has_filed_eps = any(
        cell.get("fiscal_year") == fiscal_year and cell.get("period_kind") == "annual"
        for cell in diluted_eps_cells
    )
    if has_filed_eps and filed_eps["status"] != "computed":
        return {"status": "unavailable", "value": None,
                "reason": "filed annual diluted EPS authority is ambiguous"}
    if filed_eps["status"] == "computed":
        eps_period = filed_eps["source_periods"][0]
        if (
            filed_eps["unit"] != f"{income['unit']}_per_share"
            or eps_period["calendar"] != share_calendar
            or eps_period["period_start"] != income_start
            or eps_period["period_end"] != income_end
        ):
            return {"status": "unavailable", "value": None,
                    "reason": "filed annual diluted EPS uses a different definition or window"}
        disclosed = _decimal(filed_eps["value"], "filed annual diluted EPS")
        quantum = Decimal(1).scaleb(disclosed.as_tuple().exponent)
        if calculated.quantize(quantum) != disclosed:
            return {"status": "unavailable", "value": None,
                    "reason": "computed annual diluted EPS does not tie to the filed value"}
    return {"status": "computed",
            "value": str(calculated),
            "unit": f"{income['unit']}_per_share",
            "source_periods": (income["source_periods"] + shares["source_periods"]
                               + (filed_eps["source_periods"]
                                  if filed_eps["status"] == "computed" else []))}


def forecast_structure_binding(
    structure: Mapping[str, Any], replay: Mapping[str, Any],
    financial_inputs: Mapping[str, Any],
) -> dict[str, Any]:
    """Closed handoff consumed by the future structure-aware forecast formula."""

    held = dict(structure)
    held_hash = held.pop("content_hash", None)
    if (
        structure.get("schema_version") not in _STRUCTURE_AUTHORITY_REFS
        or structure.get("authority_ref") != _STRUCTURE_AUTHORITY_REFS.get(
            structure.get("schema_version")
        )
        or not isinstance(held_hash, str)
        or content_hash(held) != held_hash
        or financial_input_authority(financial_inputs)["content_hash"]
        != structure.get("financial_input_hash")
    ):
        raise FinancialStatementStructureError("statement structure authority is invalid")
    expected_replay = replay_historical_structure(structure, financial_inputs)
    if dict(replay) != expected_replay:
        raise FinancialStatementStructureError("historical replay authority differs")
    if replay.get("structure_hash") != structure.get("content_hash"):
        raise FinancialStatementStructureError("historical replay is for another structure")
    if replay.get("ready_for_forecast") is not True:
        raise FinancialStatementStructureError("statement structure is not ready for forecast")
    projection = {
        "schema_version": (
            "forecast-statement-structure-binding-0.6"
            if structure.get("schema_version") == COVERAGE_SCHEMA_VERSION
            else "forecast-statement-structure-binding-0.5"
            if structure.get("schema_version") == REVENUE_PRETAX_SCHEMA_VERSION
            else "forecast-statement-structure-binding-0.4"
            if structure.get("schema_version") == DIRECT_GROSS_PRETAX_SCHEMA_VERSION
            else "forecast-statement-structure-binding-0.3"
            if structure.get("schema_version") == TYPED_NOTE_SCHEMA_VERSION
            else "forecast-statement-structure-binding-0.2"
            if structure.get("schema_version") == ANNUAL_SCHEMA_VERSION
            else "forecast-statement-structure-binding-0.1"
        ),
        "authority_ref": structure.get("authority_ref"),
        "structure_ref": structure.get("structure_ref"),
        "structure_hash": structure.get("content_hash"),
        "company_ref": structure.get("company_ref"),
        "spec_ref": structure.get("spec_ref"),
        "spec_hash": structure.get("spec_hash"),
        "financial_input_hash": structure.get("financial_input_hash"),
        "historical_replay_hash": content_hash(expected_replay),
    }
    for field, value in projection.items():
        _text(value, field)
    return {**projection, "content_hash": content_hash(projection)}


__all__ = [
    "ANNUAL_FORECAST_METHODS", "ANNUAL_SEMANTICS", "FORECAST_METHODS",
    "DIRECT_GROSS_PRETAX_SCHEMA_VERSION", "FinancialStatementStructureError",
    "LEGACY_SCHEMA_VERSION",
    "TYPED_NOTE_SCHEMA_VERSION",
    "LINE_KINDS", "MAX_STRUCTURE_FORMULAS", "MAX_STRUCTURE_LINES", "ROLES",
    "SCHEMA_VERSION", "STRUCTURE_AUTHORITY_REF", "STRUCTURE_PROPOSAL_SCHEMA",
    "aggregate_fiscal_year", "annual_diluted_eps", "day_weighted_annual_shares",
    "financial_input_authority",
    "forecast_structure_binding", "materialize_financial_statement_structure",
    "replay_historical_structure", "validate_financial_statement_structure",
    "validate_structure_proposal",
]
