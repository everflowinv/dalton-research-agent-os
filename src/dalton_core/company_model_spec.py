"""P13al: how *this* company should be modelled, decided per company.

A generic three-statement template is not a model. What matters about Accenture
is bookings and utilisation; what matters about IBM is the software mix and the
cash it throws off; DXC is a shrinking book being managed for margin. Same
industry, three different questions, and a template that treats them alike
answers none of them.

So the frame is fixed and the content is judged. This module is the frame:

* **What drives revenue.** Not "revenue grew 7%" -- what moves it. Volume,
  price, mix, a segment, a contract book.
* **What the costs are and how they behave.** An expense that varies with
  revenue and one that does not are different lines in a model even when they
  are one line in the filing.
* **Which statements need forecasting.** The income statement always. Whether
  the balance sheet and the cash flow statement matter is a judgement about the
  company -- for a capital-light consultancy the balance sheet is mostly
  working capital, and pretending otherwise produces three statements of which
  two are decoration.
* **Which operating metrics the market actually watches.** New bookings, book
  to bill, utilisation, headcount. These are the numbers the stock moves on and
  most of them are not in GAAP at all.

Two rules keep this honest, and they are the same two that keep the research
planner honest:

* **A basis concept must exist in the filings this company actually filed.**
  A model line resting on a concept nobody reported is fiction with a schema
  around it. The statements ledger is the vocabulary; the spec may not invent
  a word.
* **Every entry says why.** A spec whose reasons are absent is a template
  wearing a judgement's clothes, and there would be no way to argue with it --
  which is the whole reason a model gets reviewed.

And one thing the brain does not get to decide: the income statement is always
required. A company you cannot forecast revenue and margin for is a company you
are not modelling.

Deliberately no spreadsheet here. The model is structure and reasoning while it
is being built; a workbook with formulas is a rendering of it, produced when
something needs to be delivered, not the place the thinking lives.
"""

from __future__ import annotations

import json
import re
from typing import Any, Mapping, Sequence

from .company_model_inputs import CASH_FLOW_ROLE_CONCEPTS

from .driver_template import (
    COST_REGISTRY_HASH, COST_REGISTRY_REF, cost_prompt_block, cost_slot_ids,
    REGISTRY_HASH as TEMPLATE_REGISTRY_HASH,
    REGISTRY_REF as TEMPLATE_REGISTRY_REF,
    prompt_block,
    spec_gaps,
    template_for,
)
from .store import canonical_json, content_hash
from .company_financial_statement_structure import (
    FinancialStatementStructureError, STRUCTURE_PROPOSAL_SCHEMA,
    validate_structure_proposal,
)

SCHEMA_VERSION = "0.4"
LEGACY_SCHEMA_VERSION = "0.3"
TASK_REF = "task:company-model-spec:0.9"

MAX_REVENUE_DRIVERS = 8
MAX_EXPENSE_LINES = 14
MAX_OPERATING_METRICS = 10
# Five years of quarters back, three forward. The back window bounds what the
# statements lane may be asked to fetch; the forward one bounds the model.
MAX_HISTORICAL_QUARTERS = 20
MAX_FORECAST_QUARTERS = 12
MIN_FORECAST_QUARTERS = 4

STATEMENTS: tuple[str, ...] = ("income", "balance", "cash")
IMPORTANCE: tuple[str, ...] = ("required", "supporting", "not_material")
DRIVER_KINDS: tuple[str, ...] = (
    # The number of things sold: seats, engagements, billable heads.
    "volume",
    # What each one earns: rate, price, realisation.
    "price",
    # What is being sold, when the blend itself moves the total.
    "mix",
    # A reported segment or geography that moves on its own.
    "segment",
    # A book of work that converts to revenue over time: bookings, backlog.
    "contract_book",
    # A driver outside the company: an index, an FX rate, a market size.
    "external",
)
EXPENSE_BEHAVIOURS: tuple[str, ...] = (
    "variable_with_revenue",
    "variable_with_headcount",
    "fixed",
    "semi_variable",
    "one_off",
)
PERIODICITY: tuple[str, ...] = ("quarterly", "annual")
CASH_FLOW_ROLES: tuple[str, ...] = (
    "operating_cash_flow", "capital_expenditure",
)
CASH_FORECAST_METHODS: tuple[str, ...] = ("share_of_line", "unavailable")

_REF_RE = re.compile(r"^[a-z][a-z0-9]*(?:[-_][a-z0-9]+)*$")


def _entry(properties: Mapping[str, Any], required: Sequence[str]) -> dict[str, Any]:
    return {
        "type": "object", "additionalProperties": False,
        "required": list(required), "properties": dict(properties),
    }


_REF = {
    "type": "string", "minLength": 1, "maxLength": 60,
    "pattern": _REF_RE.pattern,
    "description": "Lowercase slug, unique within its list; the model's own name for this line.",
}
_LABEL = {"type": "string", "minLength": 1, "maxLength": 120}
_BECAUSE = {
    "type": "string", "minLength": 1, "maxLength": 400,
    "description": "Why this, for this company. A restatement of the label is not a reason.",
}
_BASIS = {
    "type": ["string", "null"], "maxLength": 160,
    "description": (
        "A concept from this company's filed statements, exactly as it appears "
        "there, or null when the line has no filed counterpart."
    ),
}

OUTPUT_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "CompanyModelSpecV0.4",
    "type": "object",
    "additionalProperties": False,
    "required": [
        "schema_version", "assessment", "revenue_anchor_concept",
        "revenue_drivers", "expense_lines",
        "forecast_statements", "operating_metrics", "horizon",
        "financial_statement_structure", "cash_flow_companion",
    ],
    "properties": {
        "schema_version": {"const": SCHEMA_VERSION},
        "revenue_anchor_concept": {
            "type": "string", "minLength": 1, "maxLength": 160,
            "description": (
                "The exact consolidated filed revenue concept used only as "
                "the deterministic calculation anchor. It is not an economic driver."
            ),
        },
        "assessment": {
            "type": "string", "minLength": 1, "maxLength": 1200,
            "description": (
                "What this company's economics actually turn on, and therefore "
                "what this model has to get right."
            ),
        },
        "revenue_drivers": {
            "type": "array", "minItems": 1, "maxItems": MAX_REVENUE_DRIVERS,
            "description": "What moves the top line. Not the top line itself.",
            "items": _entry(
                {
                    "ref": _REF, "label": _LABEL,
                    "kind": {"enum": list(DRIVER_KINDS)},
                    "basis_concept": _BASIS,
                    "unit": {
                        "type": "string", "minLength": 1, "maxLength": 40,
                        "description": "USD, percent, headcount, days -- what this is counted in.",
                    },
                    "because": _BECAUSE,
                },
                ("ref", "label", "kind", "basis_concept", "unit", "because"),
            ),
        },
        "expense_lines": {
            "type": "array", "minItems": 1, "maxItems": MAX_EXPENSE_LINES,
            "description": (
                "The cost structure as it behaves, which is not always as it is "
                "presented. Split a filed line when its parts behave differently."
            ),
            "items": _entry(
                {
                    "ref": _REF, "label": _LABEL,
                    "basis_concept": _BASIS,
                    "behaviour": {"enum": list(EXPENSE_BEHAVIOURS)},
                    "driver_ref": {
                        "type": ["string", "null"], "maxLength": 60,
                        "description": (
                            "The revenue driver this expense follows, when it "
                            "follows one. Must name a driver above."
                        ),
                    },
                    "because": _BECAUSE,
                    "cost_driver_slot": {"type": ["string", "null"]},
                    "cost_driver_unbound_reason": {"type": "string", "minLength": 1,
                                                   "maxLength": 400},
                },
                ("ref", "label", "basis_concept", "behaviour", "driver_ref", "because"),
            ),
        },
        "forecast_statements": {
            "type": "array", "minItems": len(STATEMENTS), "maxItems": len(STATEMENTS),
            "description": (
                "Whether each statement needs forecasting for this company. All "
                "three are answered; the income statement is always required."
            ),
            "items": _entry(
                {
                    "statement": {"enum": list(STATEMENTS)},
                    "importance": {"enum": list(IMPORTANCE)},
                    "because": _BECAUSE,
                },
                ("statement", "importance", "because"),
            ),
        },
        "operating_metrics": {
            "type": "array", "maxItems": MAX_OPERATING_METRICS,
            "description": (
                "What the market watches that GAAP does not report: new "
                "bookings, book to bill, utilisation, attrition, headcount."
            ),
            "items": _entry(
                {
                    "ref": _REF, "label": _LABEL,
                    "unit": {"type": "string", "minLength": 1, "maxLength": 40},
                    "periodicity": {"enum": list(PERIODICITY)},
                    "disclosed": {
                        "type": "boolean",
                        "description": (
                            "Does the company itself report this? An undisclosed "
                            "metric has to be estimated, and saying so is the point."
                        ),
                    },
                    "because": _BECAUSE,
                },
                ("ref", "label", "unit", "periodicity", "disclosed", "because"),
            ),
        },
        "horizon": _entry(
            {
                "historical_quarters": {
                    "type": "integer", "minimum": 1, "maximum": MAX_HISTORICAL_QUARTERS,
                    "description": "How much history this company's model needs to rest on.",
                },
                "forecast_quarters": {
                    "type": "integer",
                    "minimum": MIN_FORECAST_QUARTERS, "maximum": MAX_FORECAST_QUARTERS,
                },
                "because": _BECAUSE,
            },
            ("historical_quarters", "forecast_quarters", "because"),
        ),
        "financial_statement_structure": STRUCTURE_PROPOSAL_SCHEMA,
        "cash_flow_companion": _entry(
            {
                "schema_version": {"const": "0.1"},
                "lines": {
                    "type": "array", "minItems": 2, "maxItems": 2,
                    "items": _entry(
                        {
                            "role": {"enum": list(CASH_FLOW_ROLES)},
                            "concept": _BASIS,
                            "forecast_method": {"enum": list(CASH_FORECAST_METHODS)},
                            "forecast_base_ref": {"type": ["string", "null"],
                                                  "maxLength": 60},
                            "because": _BECAUSE,
                        },
                        ("role", "concept", "forecast_method", "forecast_base_ref",
                         "because"),
                    ),
                },
                "formula": _entry(
                    {
                        "output_ref": {"const": "free_cash_flow"},
                        "operator": {"const": "sum"},
                        "terms": {
                            "type": "array", "minItems": 2, "maxItems": 2,
                            "items": _entry(
                                {
                                    "role": {"enum": list(CASH_FLOW_ROLES)},
                                    "coefficient": {"enum": ["1", "-1"]},
                                },
                                ("role", "coefficient"),
                            ),
                        },
                    },
                    ("output_ref", "operator", "terms"),
                ),
            },
            ("schema_version", "lines", "formula"),
        ),
    },
}

TASK_HASH = content_hash({
    "task": TASK_REF,
    "output": OUTPUT_SCHEMA,
    "authority": "describes_one_company_using_only_concepts_that_company_filed",
    # W4: the frame is no longer the same four questions for every company, so
    # the hash of the task has to move when the templates move. Otherwise a
    # specification decided under the commodity template and one decided under
    # the generic one would be recorded as answers to the same question.
    "driver_template_registry": {
        "ref": TEMPLATE_REGISTRY_REF, "hash": TEMPLATE_REGISTRY_HASH,
    },
    "cost_driver_template_registry": {
        "ref": COST_REGISTRY_REF, "hash": COST_REGISTRY_HASH,
    },
    "authority_projection": "company-model-state-with-financial-notes:0.3",
    "prompt_contract": "company-model-spec-prompt:0.10",
    "structured_output_repair": "company-model-spec-repair:0.1",
})


class CompanyModelSpecError(ValueError):
    """The specification is malformed, or rests on something not in the filings."""

    def __init__(self, message: str, *, code: str = "semantic") -> None:
        super().__init__(message)
        self.code = code


def _statement_table(state: Mapping[str, Any]) -> str:
    """The filed structure as a table rather than as JSON.

    The same content as objects costs three times the bytes in repeated keys
    and nulls, and the router reserves budget against the size of the prompt.
    IBM's structure went from 31KB to under 10KB by writing it out this way,
    which is the difference between a call that is affordable and one refused
    before it is made. The ``concepts`` list is not repeated here at all --
    every concept appears below, and it exists on the state object for
    verification, not for reading.
    """

    lines: list[str] = []
    for statement in STATEMENTS:
        rows = state.get("statements", {}).get(statement) or []
        if not rows:
            continue
        lines.append(f"[{statement}]")
        for row in rows:
            mark = "*" if row.get("is_breakdown") else "-"
            parent = row.get("parent_concept") or ""
            axis = row.get("dimension_axis") or ""
            lines.append(
                f"{mark}{row.get('level', 0)}\t{row.get('concept')}\t"
                f"{row.get('label')}\t{parent}\t{axis}\t"
                f"{row.get('unit')}\t{row.get('period_kind')}"
            )
    return "\n".join(lines)


_NUMERIC_PERIOD_FIELDS = (
    "statement", "concept", "dimension_axis", "dimension_member",
    "period_start", "period_end", "period_shape", "duration_days", "value",
    "unit", "balance", "accession", "filing_form", "line_content_hash",
    "status", "ambiguity_ref",
)


def _numeric_period_cell_line(cell: Mapping[str, Any]) -> str:
    return "\t".join(
        json.dumps(cell.get(field), ensure_ascii=False, separators=(",", ":"))
        for field in _NUMERIC_PERIOD_FIELDS
    )


def _numeric_period_table_header(context: Mapping[str, Any]) -> str:
    metadata = {
        key: context.get(key) for key in (
            "schema_version", "policy", "available_cells",
            "after_series_limit_cells", "after_total_limit_cells", "included_cells",
            "omitted_by_series_limit", "omitted_by_total_limit",
            "omitted_by_prompt_limit", "prompt_byte_limit",
            "base_prompt_bytes", "prompt_bytes", "truncated",
            "content_hash",
        )
    }
    return "\n".join([
        "CONTEXT=" + json.dumps(metadata, ensure_ascii=False, sort_keys=True),
        "FILINGS=" + json.dumps(
            context.get("filing_authorities") or [],
            ensure_ascii=False, sort_keys=True,
        ),
        "FIELDS=statement\tconcept\tdimension_axis\tdimension_member\t"
        "period_start\tperiod_end\tperiod_shape\tduration_days\tvalue\tunit\t"
        "balance\taccession\tfiling_form\tline_content_hash\tstatus\t"
        "ambiguity_ref",
    ])


def _numeric_period_table(state: Mapping[str, Any]) -> str:
    context = state.get("numeric_context")
    if not isinstance(context, Mapping):
        return "NUMERIC CONTEXT UNAVAILABLE"
    lines = [_numeric_period_table_header(context)]
    for cell in context.get("cells") or []:
        lines.append(_numeric_period_cell_line(cell))
    return "\n".join(lines)


def _financial_note_table(state: Mapping[str, Any]) -> str:
    context = state.get("financial_note_context")
    if not isinstance(context, Mapping):
        return "FINANCIAL NOTE EVIDENCE UNAVAILABLE"
    from .financial_note_context import validate_financial_note_context

    held = validate_financial_note_context(
        context, company_ref=str(state.get("company_ref") or ""),
    )
    return canonical_json(held)


def build_prompt(state: Mapping[str, Any]) -> str:
    """The four questions, plus the driver template this kind of company gets.

    W4 / Chem retrospective §7.1: a commodity producer and a contracted
    compounder are not two answers to one question, they are two questions.
    The template is chosen from the dossier's ``industry_classification`` --
    carried on the state, so it is inside the hash the specification is keyed
    by -- and its basis concepts are filled from what this company actually
    filed. A company with no classification gets the generic template and the
    prompt says so in as many words, because a generic frame presented as a
    considered one is worse than no frame.
    """

    company = {
        key: state.get(key)
        for key in ("company_ref", "ticker", "entity_name", "cik", "filings")
    }
    template = prompt_block(state.get("industry_classification"),
                            state.get("concepts") or ())
    return (
        "You decide how one company should be modelled.\n\n"
        "Below is the bounded input this call actually has: company identity, filed "
        "statement structure, source-bound dated statement amounts, the selected industry "
        "driver templates, and any explicitly "
        "labelled market proxies. It cannot browse, retrieve missing filings, or inspect "
        "documents outside these blocks. Every line in the statements carries the structure "
        "the company itself disclosed -- which line rolls into which, and "
        "which lines are segment breakdowns.\n\n"
        "STATEMENTS is one line per row, tab separated:\n"
        "  <mark><level>\\t<concept>\\t<label>\\t<parent concept>\\t"
        "<dimension axis>\\t<unit>\\t<period kind>\n"
        "where the mark is '-' for a reported line and '*' for a segment or "
        "geographic breakdown, and the last two fields may be empty.\n\n"
        "NUMERIC_PERIODS contains exact strings read from the held statement ledger. "
        "Its filing and line hashes bind every amount to that authority. period_shape and "
        "duration_days describe only the dated window; they do not infer a fiscal year. "
        "Rows marked ambiguous are conflicting values in the same latest filing and period "
        "and cannot support a formula choice. The context reports every omission caused by "
        "its configured bounds. Do not infer an omitted or missing amount, treat it as zero, "
        "or infer note semantics from a concept label.\n\n"
        "Return a model specification. The frame is fixed; the judgement is "
        "yours. Four questions:\n\n"
        "1. What actually drives this company's revenue? Volume, price, mix, a "
        "segment, a contract book, something outside the company. Not "
        "'revenue' -- what moves it.\n"
        "Separately, return revenue_anchor_concept: the exact consolidated "
        "filed revenue concept used to start the arithmetic. It is not an "
        "economic driver and must not be described as one.\n"
        "2. What are its costs, and how do they behave? Split a filed line "
        "when its parts behave differently; an expense that follows revenue "
        "and one that follows headcount are different lines even when the "
        "filing shows one.\n"
        "3. Which statements need forecasting for THIS company? The income "
        "statement is always required. Whether the balance sheet and cash flow "
        "statement matter is a judgement -- say so either way, and say why. A "
        "capital-light business whose balance sheet is mostly working capital "
        "is a different case from one whose model turns on the cash it "
        "generates.\n"
        "4. Which operating metrics does the market watch that GAAP does not "
        "report? New bookings, book to bill, utilisation, attrition, "
        "headcount. Say whether the company discloses each one -- an "
        "undisclosed metric has to be estimated, and that changes how it is "
        "used.\n\n"
        "Then return financial_statement_structure: the exact duration income "
        "lines and arithmetic this company disclosed. Use consolidated filed "
        "concepts only; derived sums must follow this presentation and tie to "
        "a filed subtotal. Every derived line sets concept to null. Put its "
        "exact filed historical tie only in the associated formula's "
        "tie_out_concept. For example, the valid field placement is a line "
        "with {\"ref\":\"operating-income\",\"kind\":\"derived\","
        "\"concept\":null} and its formula with "
        "{\"output_ref\":\"operating-income\","
        "\"tie_out_concept\":\"us-gaap:OperatingIncomeLoss\"}. "
        "For each filed leaf choose quarterly_growth, "
        "share_of_line with an exact base ref, or unavailable. Derived lines "
        "use formula. Missing non-operating, tax, attribution, preferred-dividend, "
        "participating-security, convertible or share evidence stays unavailable; "
        "never treat it as zero. Diluted EPS divides the company's disclosed "
        "diluted_eps_numerator by diluted_weighted_average_shares. Parent net "
        "income is not automatically that numerator. Use "
        "company_presented_component for a filed company-specific bridge item "
        "and place it only through the formula where the filing presents it; "
        "use company_presented_subtotal for a derived subtotal tied to an exact "
        "filed concept. Both company-presented roles are currency amounts. "
        "Only diluted_weighted_average_shares uses shares; only diluted_eps uses "
        "a currency-per-share unit. Every other role uses the exact filed "
        "ISO-4217 currency. Basic EPS, basic shares, dividends per share, ratios "
        "and percentages remain in the source context; do not add them to this "
        "income calculation structure as company_presented_component or change "
        "their units to make them fit. At least one final earnings line -- continuing income, "
        "net income, or parent net income as this company presents it -- must be "
        "formula-derived and tied to that exact filed result. Filed gross profit, "
        "operating income, pretax income, net income, attribution totals, and EPS "
        "are historical/tie authorities: mark the filed copies unavailable and "
        "forecast their derived formula lines. Do not assign independent growth "
        "or shares to those totals as a substitute for the bridge. Every line "
        "also returns annual_forecast_method. It is null except for diluted "
        "weighted-average shares. Use unavailable there unless this exact company "
        "supports day_weighted_quarters and NUMERIC_PERIODS contains four "
        "positive contiguous quarter averages that tie to a direct annual share "
        "value. quarterly_growth does not itself authorize annual weighting.\n\n"
        "Then return cash_flow_companion separately from the income DAG. Select "
        "this company's exact filed operating-cash-flow and capital-expenditure "
        "concepts when they exist; capital expenditure must be the company's "
        "positive outflow amount because the declared formula subtracts it. "
        "For each selected line either declare "
        "share_of_line with an exact forecastable filed "
        "financial_statement_structure line ref as "
        "its company-specific forecast base, or unavailable. A missing source or "
        "unsupported forecast basis stays unavailable; never replace it with zero. "
        "The formula must explicitly preserve free cash flow as operating cash "
        "flow plus capital expenditure at coefficient -1.\n\n"
        "Rules:\n"
        "* Formula evidence_refs must copy exact filing accession values listed "
        "in COMPANY.filings. A financial-note ref may be copied only from the "
        "FINANCIAL_NOTE_EVIDENCE block and only when its cited passages support "
        "that formula for its exact applicability periods. Never invent one.\n"
        "* ``basis_concept`` must be a concept that appears in the statements "
        "below, copied exactly, or null. Do not invent one, and do not adapt "
        "a name to look right. A line with no filed counterpart uses null.\n"
        "* Every entry needs a reason specific to this company and grounded in the provided "
        "statement, classification, or labelled proxy evidence. State when the driver is an "
        "analytical inference. Restating the label or a general industry truth is not a reason. "
        "Choose the model structure you currently judge appropriate. In assessment, explain "
        "the key driver uncertainty and what observation would require a different structure.\n"
        "* Keep each because field, including horizon.because, within "
        f"{_BECAUSE['maxLength']} characters: give the specific reason for that line or horizon. "
        "Use assessment for the overall modelling judgement, within "
        f"{OUTPUT_SCHEMA['properties']['assessment']['maxLength']} characters. The output is "
        "a modelling specification; investment scenarios and tracking tasks have their own "
        "downstream consumers. Do not squeeze a full scenario discussion into every because.\n"
        "* Return compact JSON matching OUTPUT_SCHEMA and nothing else. Do not "
        "pretty-print or add indentation or insignificant whitespace; preserve every "
        "required semantic field. This reduces formatting overhead but does not change "
        "the output-token limit.\n\n"
        f"{template}\n\n"
        f"{cost_prompt_block(state.get('industry_classification'), state.get('concepts') or ())}\n\n"
        f"OUTPUT_SCHEMA:\n{json.dumps(OUTPUT_SCHEMA, ensure_ascii=False)}\n\n"
        f"COMPANY:\n{json.dumps(company, ensure_ascii=False, sort_keys=True)}\n\n"
        "MARKET PROXIES (never company actuals; preserve each proxy_gap when "
        "citing one):\n"
        f"{json.dumps(state.get('market_proxies') or [], ensure_ascii=False, sort_keys=True)}\n\n"
        f"STATEMENTS:\n{_statement_table(state)}\n"
        f"\nNUMERIC_PERIODS:\n{_numeric_period_table(state)}\n"
        f"\nFINANCIAL_NOTE_EVIDENCE:\n{_financial_note_table(state)}\n"
    )


def parse_response(text: Any) -> dict[str, Any]:
    """The model's JSON, or a refusal that says what was wrong with it."""

    if isinstance(text, Mapping):
        return dict(text)
    if not isinstance(text, str) or not text.strip():
        raise CompanyModelSpecError("model returned no specification", code="format")
    body = text.strip()
    if body.startswith("```"):
        body = body.split("\n", 1)[-1]
        if body.rstrip().endswith("```"):
            body = body.rstrip()[: -3]
    start, end = body.find("{"), body.rfind("}")
    if start < 0 or end <= start:
        raise CompanyModelSpecError(
            "model response contains no JSON object", code="format"
        )
    try:
        value = json.loads(body[start:end + 1])
    except json.JSONDecodeError as exc:
        raise CompanyModelSpecError(
            f"model response is not JSON: {exc}", code="format"
        ) from exc
    if not isinstance(value, dict):
        raise CompanyModelSpecError("model response is not an object", code="format")
    return value


def _text(
    value: Any, name: str, *, limit: int, repairable_length: bool = False,
) -> str:
    if not isinstance(value, str) or not value.strip():
        # Once JSON parsed, absent or empty semantic fields are a judgement
        # failure. In particular refs, slots and reasons must never become
        # eligible merely because their wire value was blank.
        raise CompanyModelSpecError(f"{name} must be a non-empty string")
    if len(value) > limit:
        raise CompanyModelSpecError(
            f"{name} is longer than {limit} characters",
            code="text_length" if repairable_length else "semantic",
        )
    return value.strip()


def _one_of(value: Any, allowed: Sequence[str], name: str) -> str:
    if value not in allowed:
        raise CompanyModelSpecError(f"{name} must be one of {', '.join(allowed)}")
    return str(value)


def _ref(value: Any, name: str, seen: set[str]) -> str:
    ref = _text(value, name, limit=60)
    if _REF_RE.fullmatch(ref) is None:
        raise CompanyModelSpecError(f"{name} must be a lowercase slug")
    if ref in seen:
        raise CompanyModelSpecError(f"{name} {ref!r} is used twice")
    seen.add(ref)
    return ref


def _basis(value: Any, concepts: set[str], name: str) -> str | None:
    """A concept the company actually filed, or nothing.

    This is the rule that keeps a model from resting on something nobody
    reported. A near-miss is refused rather than repaired: silently mapping
    ``us-gaap:Revenue`` onto ``us-gaap:Revenues`` is how a model comes to cite
    a line that does not exist.
    """

    if value is None:
        return None
    concept = _text(value, name, limit=160)
    if concept not in concepts:
        raise CompanyModelSpecError(
            f"{name} {concept!r} is not a concept in this company's filed statements"
        )
    return concept


def _cash_flow_companion(
    value: Any, *, concepts: set[str], cash_lines: Mapping[str, Mapping[str, Any]],
    structure: Mapping[str, Any],
    cash_importance: str,
) -> dict[str, Any]:
    """Validate the analyst's explicit, company-specific cash forecast basis."""

    if not isinstance(value, Mapping) or set(value) != {
        "schema_version", "lines", "formula",
    }:
        raise CompanyModelSpecError("cash_flow_companion has an invalid closed shape")
    if value.get("schema_version") != "0.1":
        raise CompanyModelSpecError("cash_flow_companion.schema_version is not 0.1")
    structure_lines = {
        str(item.get("ref")): item for item in (structure.get("lines") or [])
        if isinstance(item, Mapping) and item.get("ref")
    }
    raw_lines = value.get("lines")
    if not isinstance(raw_lines, list) or len(raw_lines) != len(CASH_FLOW_ROLES):
        raise CompanyModelSpecError("cash_flow_companion must answer for both cash lines")
    lines: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, item in enumerate(raw_lines):
        if not isinstance(item, Mapping) or set(item) != {
            "role", "concept", "forecast_method", "forecast_base_ref", "because",
        }:
            raise CompanyModelSpecError(
                f"cash_flow_companion.lines[{index}] has an invalid closed shape")
        role = _one_of(item.get("role"), CASH_FLOW_ROLES,
                       f"cash_flow_companion.lines[{index}].role")
        if role in seen:
            raise CompanyModelSpecError(f"cash_flow_companion names {role} twice")
        seen.add(role)
        concept = _basis(item.get("concept"), concepts,
                         f"cash_flow_companion.lines[{index}].concept")
        if concept is not None and concept not in cash_lines:
            raise CompanyModelSpecError(
                f"cash_flow_companion {role} concept is not a consolidated cash "
                "statement line")
        if concept is not None and any(
            concept in known_concepts for other_role, known_concepts in
            CASH_FLOW_ROLE_CONCEPTS.items() if other_role != role
        ):
            raise CompanyModelSpecError(
                f"cash_flow_companion {role} uses a concept with known other-role "
                "semantics")
        method = _one_of(
            item.get("forecast_method"), CASH_FORECAST_METHODS,
            f"cash_flow_companion.lines[{index}].forecast_method")
        base_ref = item.get("forecast_base_ref")
        if method == "share_of_line":
            if concept is None:
                raise CompanyModelSpecError(
                    f"cash_flow_companion {role} needs a filed concept")
            base_ref = _text(
                base_ref, f"cash_flow_companion.lines[{index}].forecast_base_ref",
                limit=60)
            if base_ref not in structure_lines:
                raise CompanyModelSpecError(
                    f"cash_flow_companion {role} base names no income structure line")
            base_line = structure_lines[base_ref]
            if (base_line.get("kind") != "filed"
                    or base_line.get("forecast_method") not in {
                        "quarterly_growth", "share_of_line",
                    }):
                raise CompanyModelSpecError(
                    f"cash_flow_companion {role} base is not a forecastable filed line")
            cash_unit = str(cash_lines[concept].get("unit") or "").casefold()
            base_unit = str(base_line.get("unit") or "").casefold()
            if not cash_unit or cash_unit != base_unit:
                raise CompanyModelSpecError(
                    f"cash_flow_companion {role} source and base units differ")
        elif base_ref is not None:
            raise CompanyModelSpecError(
                f"cash_flow_companion unavailable {role} cannot name a forecast base")
        if cash_importance == "not_material" and method != "unavailable":
            raise CompanyModelSpecError(
                "cash_flow_companion cannot forecast a statement marked not_material")
        lines.append({
            "role": role, "concept": concept, "forecast_method": method,
            "forecast_base_ref": base_ref,
            "because": _text(
                item.get("because"), f"cash_flow_companion.lines[{index}].because",
                limit=400, repairable_length=True),
        })
    if seen != set(CASH_FLOW_ROLES):
        raise CompanyModelSpecError("cash_flow_companion is missing a required cash line")
    lines.sort(key=lambda item: CASH_FLOW_ROLES.index(str(item["role"])))
    selected_concepts = [str(item["concept"]) for item in lines
                         if item["concept"] is not None]
    if len(selected_concepts) != len(set(selected_concepts)):
        raise CompanyModelSpecError(
            "cash_flow_companion cannot use one filed concept for both roles")
    formula = value.get("formula")
    expected_formula = {
        "output_ref": "free_cash_flow", "operator": "sum",
        "terms": [
            {"role": "operating_cash_flow", "coefficient": "1"},
            {"role": "capital_expenditure", "coefficient": "-1"},
        ],
    }
    if formula != expected_formula:
        raise CompanyModelSpecError(
            "cash_flow_companion formula must define operating cash flow minus capex")
    return {"schema_version": "0.1", "lines": lines, "formula": expected_formula}


def spec_from_response(
    state: Mapping[str, Any], response: Any, *, decided_by: str,
    note_evidence_resolver: Any | None = None,
) -> dict[str, Any]:
    """Verify one model specification against the company it claims to describe."""

    body = parse_response(response)
    if body.get("schema_version") != SCHEMA_VERSION:
        raise CompanyModelSpecError("model specification schema_version is not 0.4")
    company_ref = state.get("company_ref")
    if not isinstance(company_ref, str) or not company_ref:
        raise CompanyModelSpecError("company state carries no company_ref")
    concepts = {
        str(item) for item in (state.get("concepts") or [])
        if isinstance(item, str) and item
    }
    if not concepts:
        raise CompanyModelSpecError(
            "this company has no filed statements to model against"
        )
    revenue_anchor = _basis(
        body.get("revenue_anchor_concept"), concepts, "revenue_anchor_concept")
    if revenue_anchor is None:
        raise CompanyModelSpecError("revenue_anchor_concept must name a filed concept")
    from .model_forecast_driver import CONCEPT_ROLES, REVENUE
    anchor_rows = [
        row
        for row in ((state.get("statements") or {}).get("income") or [])
        if isinstance(row, Mapping)
        and row.get("concept") == revenue_anchor
        and not row.get("is_breakdown")
        and not row.get("dimension_axis")
    ]
    if CONCEPT_ROLES.get(revenue_anchor) != REVENUE or not anchor_rows:
        raise CompanyModelSpecError(
            "revenue_anchor_concept must name a consolidated filed revenue line"
        )

    drivers: list[dict[str, Any]] = []
    driver_refs: set[str] = set()
    raw_drivers = body.get("revenue_drivers")
    if not isinstance(raw_drivers, list) or not raw_drivers:
        raise CompanyModelSpecError("a model needs at least one revenue driver")
    if len(raw_drivers) > MAX_REVENUE_DRIVERS:
        raise CompanyModelSpecError(
            f"a model may name at most {MAX_REVENUE_DRIVERS} revenue drivers")
    for index, item in enumerate(raw_drivers):
        if not isinstance(item, Mapping):
            raise CompanyModelSpecError(f"revenue_drivers[{index}] must be an object")
        drivers.append({
            "ref": _ref(item.get("ref"), f"revenue_drivers[{index}].ref", driver_refs),
            "label": _text(item.get("label"), f"revenue_drivers[{index}].label", limit=120),
            "kind": _one_of(item.get("kind"), DRIVER_KINDS, f"revenue_drivers[{index}].kind"),
            "basis_concept": _basis(
                item.get("basis_concept"), concepts,
                f"revenue_drivers[{index}].basis_concept"),
            "unit": _text(item.get("unit"), f"revenue_drivers[{index}].unit", limit=40),
            "because": _text(item.get("because"), f"revenue_drivers[{index}].because",
                             limit=400, repairable_length=True),
        })

    expenses: list[dict[str, Any]] = []
    expense_refs: set[str] = set()
    raw_expenses = body.get("expense_lines")
    if not isinstance(raw_expenses, list) or not raw_expenses:
        raise CompanyModelSpecError("a model needs at least one expense line")
    if len(raw_expenses) > MAX_EXPENSE_LINES:
        raise CompanyModelSpecError(
            f"a model may name at most {MAX_EXPENSE_LINES} expense lines")
    for index, item in enumerate(raw_expenses):
        if not isinstance(item, Mapping):
            raise CompanyModelSpecError(f"expense_lines[{index}] must be an object")
        driver_ref = item.get("driver_ref")
        if driver_ref is not None:
            driver_ref = _text(driver_ref, f"expense_lines[{index}].driver_ref", limit=60)
            if driver_ref not in driver_refs:
                raise CompanyModelSpecError(
                    f"expense_lines[{index}].driver_ref {driver_ref!r} names no revenue driver"
                )
        expense = {
            "ref": _ref(item.get("ref"), f"expense_lines[{index}].ref", expense_refs),
            "label": _text(item.get("label"), f"expense_lines[{index}].label", limit=120),
            "basis_concept": _basis(
                item.get("basis_concept"), concepts,
                f"expense_lines[{index}].basis_concept"),
            "behaviour": _one_of(item.get("behaviour"), EXPENSE_BEHAVIOURS,
                                 f"expense_lines[{index}].behaviour"),
            "driver_ref": driver_ref,
            "because": _text(item.get("because"), f"expense_lines[{index}].because",
                             limit=400, repairable_length=True),
        }
        if "cost_driver_slot" in item:
            slot = item.get("cost_driver_slot")
            reason = item.get("cost_driver_unbound_reason")
            if slot is None:
                expense["cost_driver_slot"] = None
                expense["cost_driver_unbound_reason"] = _text(
                    reason, f"expense_lines[{index}].cost_driver_unbound_reason",
                    limit=400, repairable_length=True)
            else:
                slot = _text(slot, f"expense_lines[{index}].cost_driver_slot", limit=60)
                if slot not in cost_slot_ids(state.get("industry_classification")):
                    raise CompanyModelSpecError(
                        f"expense_lines[{index}].cost_driver_slot {slot!r} is not in "
                        "this classification's cost template")
                if reason is not None:
                    raise CompanyModelSpecError(
                        f"expense_lines[{index}] binds a cost slot and cannot carry "
                        "cost_driver_unbound_reason")
                expense["cost_driver_slot"] = slot
        elif "cost_driver_unbound_reason" in item:
            raise CompanyModelSpecError(
                f"expense_lines[{index}].cost_driver_unbound_reason requires cost_driver_slot")
        expenses.append(expense)

    raw_statements = body.get("forecast_statements")
    if not isinstance(raw_statements, list):
        raise CompanyModelSpecError("forecast_statements must be a list")
    statements: dict[str, dict[str, Any]] = {}
    for index, item in enumerate(raw_statements):
        if not isinstance(item, Mapping):
            raise CompanyModelSpecError(f"forecast_statements[{index}] must be an object")
        name = _one_of(item.get("statement"), STATEMENTS,
                       f"forecast_statements[{index}].statement")
        if name in statements:
            raise CompanyModelSpecError(f"forecast_statements names {name} twice")
        statements[name] = {
            "statement": name,
            "importance": _one_of(item.get("importance"), IMPORTANCE,
                                  f"forecast_statements[{index}].importance"),
            "because": _text(item.get("because"),
                             f"forecast_statements[{index}].because", limit=400,
                             repairable_length=True),
        }
    missing = [name for name in STATEMENTS if name not in statements]
    if missing:
        raise CompanyModelSpecError(
            f"forecast_statements must answer for every statement; missing {', '.join(missing)}"
        )
    # The one judgement that is not the brain's. A company whose revenue and
    # margin you cannot forecast is a company you are not modelling.
    if statements["income"]["importance"] != "required":
        raise CompanyModelSpecError("the income statement is always required")

    metrics: list[dict[str, Any]] = []
    metric_refs: set[str] = set()
    raw_metrics = body.get("operating_metrics")
    if not isinstance(raw_metrics, list):
        raise CompanyModelSpecError("operating_metrics must be a list")
    if len(raw_metrics) > MAX_OPERATING_METRICS:
        raise CompanyModelSpecError(
            f"a model may name at most {MAX_OPERATING_METRICS} operating metrics")
    for index, item in enumerate(raw_metrics):
        if not isinstance(item, Mapping):
            raise CompanyModelSpecError(f"operating_metrics[{index}] must be an object")
        disclosed = item.get("disclosed")
        if not isinstance(disclosed, bool):
            raise CompanyModelSpecError(
                f"operating_metrics[{index}].disclosed must be true or false")
        metrics.append({
            "ref": _ref(item.get("ref"), f"operating_metrics[{index}].ref", metric_refs),
            "label": _text(item.get("label"), f"operating_metrics[{index}].label", limit=120),
            "unit": _text(item.get("unit"), f"operating_metrics[{index}].unit", limit=40),
            "periodicity": _one_of(item.get("periodicity"), PERIODICITY,
                                   f"operating_metrics[{index}].periodicity"),
            "disclosed": disclosed,
            "because": _text(item.get("because"), f"operating_metrics[{index}].because",
                             limit=400, repairable_length=True),
        })

    raw_horizon = body.get("horizon")
    if not isinstance(raw_horizon, Mapping):
        raise CompanyModelSpecError("horizon must be an object")
    historical = raw_horizon.get("historical_quarters")
    forecast = raw_horizon.get("forecast_quarters")
    for value, name, low, high in (
        (historical, "historical_quarters", 1, MAX_HISTORICAL_QUARTERS),
        (forecast, "forecast_quarters", MIN_FORECAST_QUARTERS, MAX_FORECAST_QUARTERS),
    ):
        if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
            raise CompanyModelSpecError(f"horizon.{name} must be an integer {low}..{high}")
    horizon = {
        "historical_quarters": int(historical),
        "forecast_quarters": int(forecast),
        "because": _text(raw_horizon.get("because"), "horizon.because", limit=400,
                         repairable_length=True),
    }

    if not isinstance(decided_by, str) or not decided_by.startswith(
        ("human:", "automation:")
    ):
        raise CompanyModelSpecError(
            "decided_by must use the human: or automation: namespace")

    from .financial_note_context import (
        FinancialNoteContextError, validate_financial_note_context,
    )
    try:
        note_evidence = []
        context = state.get("financial_note_context")
        if isinstance(context, Mapping):
            held_context = validate_financial_note_context(
                context, company_ref=company_ref,
            )
            known = {
                item["binding"]["ref"]: item["binding"]
                for item in held_context["records"]
            }
            raw_structure = body.get("financial_statement_structure")
            cited = {
                ref
                for formula in (
                    raw_structure.get("formulas", [])
                    if isinstance(raw_structure, Mapping) else []
                )
                if isinstance(formula, Mapping)
                for ref in (formula.get("evidence_refs") or [])
                if isinstance(ref, str) and ref in known
            }
            note_evidence = [known[ref] for ref in sorted(cited)]
        statement_structure = validate_structure_proposal(
            body.get("financial_statement_structure"), state,
            revenue_anchor_concept=revenue_anchor, expense_lines=expenses,
            note_evidence=note_evidence,
            note_evidence_resolver=note_evidence_resolver,
        )
    except (FinancialStatementStructureError, FinancialNoteContextError) as exc:
        raise CompanyModelSpecError(
            f"financial_statement_structure is invalid: {exc}"
        ) from exc
    cash_companion = _cash_flow_companion(
        body.get("cash_flow_companion"), concepts=concepts,
        cash_lines={
            str(row.get("concept")): row
            for row in ((state.get("statements") or {}).get("cash") or [])
            if isinstance(row, Mapping) and row.get("concept")
            and not row.get("is_breakdown") and not row.get("dimension_axis")
        },
        structure=statement_structure,
        cash_importance=statements["cash"]["importance"],
    )

    spec = {
        "schema_version": SCHEMA_VERSION,
        "company_ref": company_ref,
        "state_hash": state.get("state_hash"),
        "assessment": _text(body.get("assessment"), "assessment", limit=1200,
                            repairable_length=True),
        "revenue_anchor_concept": revenue_anchor,
        "revenue_drivers": drivers,
        "expense_lines": expenses,
        "forecast_statements": [statements[name] for name in STATEMENTS],
        "operating_metrics": metrics,
        "horizon": horizon,
        "financial_statement_structure": statement_structure,
        "cash_flow_companion": cash_companion,
        "decided_by": decided_by,
        "task_hash": TASK_HASH,
    }
    if any("cost_driver_slot" in item for item in expenses):
        selected = template_for(state.get("industry_classification"))["classification"]
        spec["cost_driver_template"] = {
            "registry_ref": COST_REGISTRY_REF,
            "registry_hash": COST_REGISTRY_HASH,
            "classification": selected,
        }
    spec["content_hash"] = content_hash(spec)
    return spec


def spec_template_gaps(
    spec: Mapping[str, Any], state: Mapping[str, Any]
) -> list[str]:
    """Driver-template slots this specification does not model.

    Reported, never enforced. A model specification is a judgement about one
    company and the template is a prior about a *kind* of company; a producer
    that genuinely has no cost-curve position worth modelling exists, and a
    check that refused the specification for it would be the table overruling
    the analyst. What the reader gets instead is the list of questions the
    frame expected and the answer did not contain.
    """

    return spec_gaps(spec, state.get("industry_classification"))


def forecast_statements(spec: Mapping[str, Any]) -> list[str]:
    """The statements this company's model actually has to produce."""

    return [
        item["statement"] for item in spec.get("forecast_statements", [])
        if item.get("importance") in ("required", "supporting")
    ]


def undisclosed_metrics(spec: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Metrics the model needs that the company does not report.

    Worth naming separately: these are the ones that have to be estimated, and
    an estimate presented beside a filed figure without that distinction is the
    quiet way a model stops being trustworthy.
    """

    return [item for item in spec.get("operating_metrics", [])
            if not item.get("disclosed")]


__all__ = [
    "CASH_FLOW_ROLES",
    "CASH_FORECAST_METHODS",
    "DRIVER_KINDS",
    "EXPENSE_BEHAVIOURS",
    "IMPORTANCE",
    "MAX_EXPENSE_LINES",
    "MAX_FORECAST_QUARTERS",
    "MAX_HISTORICAL_QUARTERS",
    "MAX_OPERATING_METRICS",
    "MAX_REVENUE_DRIVERS",
    "MIN_FORECAST_QUARTERS",
    "OUTPUT_SCHEMA",
    "PERIODICITY",
    "SCHEMA_VERSION",
    "LEGACY_SCHEMA_VERSION",
    "STATEMENTS",
    "TASK_HASH",
    "TASK_REF",
    "CompanyModelSpecError",
    "build_prompt",
    "spec_template_gaps",
    "template_for",
    "forecast_statements",
    "parse_response",
    "spec_from_response",
    "undisclosed_metrics",
]
