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

import copy
import json
import re
from typing import Any, Mapping, Sequence

from .company_model_inputs import CASH_FLOW_ROLE_CONCEPTS, NET_INCOME_CONCEPTS

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

MAX_ASSESSMENT_CHARS = 2400
PRIOR_ASSESSMENT_CHARS = 1200
ASSESSMENT_COMPATIBILITY_REF = "model-spec-assessment-length-compatible-replay:0.1"

MAX_REVENUE_DRIVERS = 8
MAX_EXPENSE_LINES = 14
MAX_OPERATING_METRICS = 10
# Five years of quarters back, three forward. The back window bounds what the
# statements lane may be asked to fetch; the forward one bounds the model.
MAX_HISTORICAL_QUARTERS = 20
MAX_FORECAST_QUARTERS = 12
MIN_FORECAST_QUARTERS = 8

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
        "financial_statement_structure", "cash_flow_companion", "driver_formulas",
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
            "type": "string", "minLength": 1, "maxLength": MAX_ASSESSMENT_CHARS,
            "description": (
                "What this company's economics actually turn on, and therefore "
                "what this model has to get right."
            ),
        },
        "driver_formulas": {"type": "array", "items": _entry({
            "output_ref": _REF, "operator": {"enum": ["sum", "product", "contract_conversion"]},
            "driver_refs": {"type": "array", "items": _REF, "minItems": 1}, "because": _BECAUSE,
        }, ("output_ref", "operator", "driver_refs", "because")),
            "description": "Every declared economic driver must enter an executable formula: segment sum, volume × price, or contract book × conversion rate. Missing execution blocks publication."},
        "fcf_definition": _entry({
            "source_ref": {"type": "string", "minLength": 1},
            "components": {"type": "array", "minItems": 2, "items": _entry({
                "role": {"enum": ["operating_cash_flow", "capital_expenditure", "finance_lease_principal", "capitalized_software", "capitalized_contract_costs"]},
                "concept": {"type": "string", "minLength": 1}, "coefficient": {"enum": ["1", "-1"]}}, ("role", "concept", "coefficient"))},
            "one_off_items": {"type": "array", "items": _entry({
                "period_end": {"type": "string", "format": "date"}, "value": {"type": "string"},
                "direction": {"enum": ["inflow", "outflow"]}, "source_ref": {"type": "string", "minLength": 1},
                "because": _BECAUSE}, ("period_end", "value", "direction", "source_ref", "because"))},
        }, ("source_ref", "components", "one_off_items")),
        "revenue_drivers": {
            "type": "array", "minItems": 1, "maxItems": MAX_REVENUE_DRIVERS,
            "description": "What moves the top line. Not the top line itself.",
            "items": _entry(
                {
                    "ref": _REF, "label": _LABEL,
                    "kind": {"enum": list(DRIVER_KINDS)},
                    "basis_concept": _BASIS,
                    "basis_identity": {"type":"object","additionalProperties":False,"required":["statement","dimensions"],"properties":{"statement":{"enum":["income","balance","cash"]},"dimensions":{"type":"object","minProperties":1,"additionalProperties":{"type":"string","minLength":1}}}},
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

EPS_NUMERATOR_BRIDGE_RULE_REF = "company-model-spec-accounting-eps-bridge:0.1"
EPS_NUMERATOR_BRIDGE_GUIDANCE = (
    "Keep accounting net_income separate from diluted_eps_numerator. When "
    "net income is calculated from pretax income, taxes and filed company "
    "components, retain that line and its formula with role net_income; do "
    "not retag it diluted_eps_numerator. Add a separate derived numerator "
    "bridge using the exact net_income or parent_net_income line and only "
    "disclosed dilution adjustments. If the filed diluted EPS proves that "
    "net income itself is the numerator, the bridge is sum(+1 * net-income), "
    "and the EPS divide refers to this bridge and the filed diluted shares. "
    "A numerator sum cannot directly use pretax_income or income_tax_expense. "
    "Tie the accounting total and numerator to their exact filed authorities "
    "and validate the numerator through filed diluted EPS in every period. "
    "Missing dilution evidence remains unavailable, never zero."
)

_TASK_IDENTITY = {
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
    "authority_projection": "company-model-state-with-financial-notes:0.4",
    "prompt_contract": "company-model-spec-prompt:0.17",
    "eps_numerator_bridge": {
        "ref": EPS_NUMERATOR_BRIDGE_RULE_REF,
        "guidance": EPS_NUMERATOR_BRIDGE_GUIDANCE,
    },
    "structured_output_repair": "company-model-spec-repair:0.1",
}
TASK_HASH = content_hash(_TASK_IDENTITY)
PRIOR_ASSESSMENT_TASK_HASH = "497833a7b632a3e195fc16c60c20c99315c5ba5630d2292b33feee894cc3b0ea"


def assessment_compatibility_contract() -> dict[str, Any]:
    """Exactly one published task change, never a generic old-answer bridge."""
    prior = copy.deepcopy(_TASK_IDENTITY)
    prior["output"]["properties"]["assessment"]["maxLength"] = PRIOR_ASSESSMENT_CHARS
    if content_hash(prior) != PRIOR_ASSESSMENT_TASK_HASH:
        raise CompanyModelSpecError("assessment-compatible prior task contract drifted")
    return {
        "ref": ASSESSMENT_COMPATIBILITY_REF,
        "prior_task_hash": PRIOR_ASSESSMENT_TASK_HASH,
        "current_task_hash": TASK_HASH,
        "only_change": {"path": "output.properties.assessment.maxLength",
                        "old": PRIOR_ASSESSMENT_CHARS, "new": MAX_ASSESSMENT_CHARS},
    }


def prior_assessment_output_schema() -> dict[str, Any]:
    assessment_compatibility_contract()
    schema = copy.deepcopy(OUTPUT_SCHEMA)
    schema["properties"]["assessment"]["maxLength"] = PRIOR_ASSESSMENT_CHARS
    return schema


CASH_FLOW_OPERATING_BASE_SHAPE_CODE = "cash_flow_operating_base_shape"


class CompanyModelSpecError(ValueError):
    """The specification is malformed, or rests on something not in the filings."""

    def __init__(self, message: str, *, code: str = "semantic", proof: Any = None) -> None:
        super().__init__(message)
        self.code = code
        if proof is not None:
            self.proof = proof


# The structure rules a model may be asked to reconsider once, as a closed
# allow-list rather than a category.
#
# ``validate_structure_proposal`` refuses for two very different sorts of
# reason.  Some are *wiring*: this line is derived so it may not also claim a
# filed concept; a filed subtotal has to be actual, a tie or unavailable; the
# diluted-share line needs a duration direct_annual basis; a sum formula's
# roles have to match the statement it sums.  Every one of those is a choice
# among shapes the model was already shown, and telling it exactly which rule
# it broke is enough for it to pick the right one -- 12 live refusals between
# 2026-09-11 and 09-13 were of exactly this kind, and each one cost a whole
# specification.
#
# Others are *arithmetic about the filings*: "this formula does not tie to
# filed history", "structure omits expense concepts selected by the company
# spec".  Handing those back would be inviting the model to make the numbers
# agree, which is the one thing it must never be asked to do.  So the list is
# an allow-list keyed on the rule's own message, anything unrecognised stays
# ``semantic``, and adding to it is a reviewed decision with a version.
REPAIRABLE_STRUCTURE_RULES_REF = "rule:company-model-spec-repairable-structure:0.4"
REPAIRABLE_STRUCTURE_RULES: tuple[str, ...] = (
    "a derived line cannot claim a filed concept",
    "a filed subtotal may only be actual/tie authority or unavailable",
    "diluted weighted-average shares require duration direct_annual shares",
    "only diluted weighted-average shares may carry annual_forecast_method",
    "Only diluted weighted-average shares may select",
    "sum formula roles do not match its company statement output",
    "EPS denominator must be diluted weighted-average shares",
    # 0.2, live 2026-09: the *numerator* half of the same divide-formula rule
    # was missing, so a specification that named the right denominator and the
    # wrong numerator role was a whole refusal while its mirror image was a
    # repair.  Both are the same kind of choice among roles the model was shown
    # -- ``diluted_eps_numerator`` is one of them -- and neither is arithmetic
    # about the filings.
    "EPS numerator must use the company-specific diluted EPS numerator role",
    # 0.3, live 2026-09-18 09:24Z (GOOGL, Hyperscaler): a line carrying the
    # company-presented *component* role must be ``filed`` and one carrying the
    # company-presented *subtotal* role must be ``derived``.  Both halves are
    # enumerated fields the model chose from a list it was shown, and the rule
    # names which one it got wrong; it is the same kind of wiring choice as "a
    # derived line cannot claim a filed concept", which has been repairable
    # since 0.1.  Nothing here asks the model to make a number agree.
    "company-presented components must be filed and subtotals must be derived",
    # 0.4, live 2026-09-27 (DXC 09:36Z, IBM 09:12Z): the diluted_eps line's
    # ``annual_semantics`` was ``not_applicable``.  The schema the model was
    # shown enumerates the two admissible values (``direct_annual`` for a filed
    # annual EPS, ``annual_ratio`` for a derived divide); picking a third is
    # the same kind of enum choice as the diluted-share rule above, repairable
    # since 0.1.  DXC's refusal had no repair at all because this rule was
    # read as ``semantic``.  Nothing here asks the model to make a number agree.
    "diluted EPS cannot be aggregated from quarterly EPS",
)


def structure_error_code(message: str) -> str:
    """``structure`` when the refusal is a wiring rule, else ``semantic``.

    Fail-closed on the unknown: a rule this list has not seen is not eligible
    for a repair, however much it looks like one.
    """

    return ("structure"
            if any(rule in message for rule in REPAIRABLE_STRUCTURE_RULES)
            else "semantic")


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


def _numeric_period_cell_line(cell: Mapping[str, Any], *, catalog: bool = False) -> str:
    row="\t".join(
        json.dumps(cell.get(field), ensure_ascii=False, separators=(",", ":"))
        for field in _NUMERIC_PERIOD_FIELDS
    )
    if "dimensions" in cell:
        source = {k:cell[k] for k in ("dimensions","statement_context_ref","source_unit_ref","unit_definition_hash") if k in cell}
        if catalog and cell.get("status") == "filed":
            from .company_model_identity_catalog import cell_identity_ref
            source["catalog_ref"] = cell_identity_ref(cell)
        row+="\tSOURCE_IDENTITY="+json.dumps(source,ensure_ascii=False,sort_keys=True,separators=(",",":"))
    return row


def _numeric_period_table_header(context: Mapping[str, Any]) -> str:
    from .model_revenue_group_candidates import CONTRACT_REF as GROUP_CHOICES_REF
    group_choices = context.get("revenue_group_candidates")
    typed_guidance = (
        "FIELD REQUIREMENTS: operating_metrics.periodicity is exactly quarterly or annual, never quarterly_or_annual. "
        "An acquisitions cash line cannot use zero_one_off; select a supported cash forecast method and its exact base, "
        "or unavailable with null base. A segment driver with a filed basis must select a distinct complete "
        "basis_identity from one coherent disclosed revenue group. Do not bind independent segments to the same "
        "group total, mix groups, or invent a residual. If no exact dimensional source is provided, keep that base "
        "explicitly unavailable instead of using consolidated revenue.\n\n"
        if isinstance(group_choices, Mapping) and group_choices.get("contract_ref") == GROUP_CHOICES_REF else ""
    )
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
    for key in ("identity_catalog_contract", "selection_contract", "revenue_group_candidates"):
        if key in context:
            metadata[key] = context[key]
    return "\n".join([
        *([typed_guidance.rstrip()] if typed_guidance else []),
        "CONTEXT=" + json.dumps(metadata, ensure_ascii=False, sort_keys=True),
        "FILINGS=" + json.dumps(
            context.get("filing_authorities") or [],
            ensure_ascii=False, sort_keys=True,
        ),
        "FIELDS=statement\tconcept\tdimension_axis\tdimension_member\t"
        "period_start\tperiod_end\tperiod_shape\tduration_days\tvalue\tunit\t"
        "balance\taccession\tfiling_form\tline_content_hash\tstatus\t"
        "ambiguity_ref",
        *(["SOURCE_IDENTITY suffix, when present, binds complete dimensions and exact context version; never infer missing axes from labels or amounts."] if any("dimensions" in c for c in context.get("cells") or []) else []),
        *(["For a dimensional revenue driver, select the row's catalog_ref as basis_identity={statement:<that row's statement>,dimensions:{catalog_ref:<exact ref>}}. The system binds all original axes; do not hand-copy a subset. A ref must belong to the selected basis_concept in this input."] if context.get("identity_catalog_contract") else []),
        *(["Revenue group candidates are exact-source disclosure choices, not an automatic modeling choice. Select and explain one coherent group using its member identity refs; a not_closed result requires original reconciliation evidence. Do not invent a residual member or mix totals from different groups or filings."] if context.get("revenue_group_candidates") else []),
    ])


def _numeric_period_table(state: Mapping[str, Any]) -> str:
    context = state.get("numeric_context")
    if not isinstance(context, Mapping):
        return "NUMERIC CONTEXT UNAVAILABLE"
    lines = [_numeric_period_table_header(context)]
    for cell in context.get("cells") or []:
        lines.append(_numeric_period_cell_line(cell, catalog=bool(context.get("identity_catalog_contract"))))
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


def build_prompt(state: Mapping[str, Any], *,
                 output_schema: Mapping[str, Any] | None = None) -> str:
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

    output_schema = OUTPUT_SCHEMA if output_schema is None else output_schema
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
        "driver_formulas is required. Every revenue_drivers ref must enter an explicit "
        "formula whose output_ref is an exact filed financial_statement_structure leaf. "
        "For sum, identify every source-disclosed member needed to tie the total; never omit "
        "a residual member or infer an allocation. For product, use quantity × price; for "
        "contract_conversion, use a genuine contract book and an admitted conversion basis. "
        "A segment's reported revenue is a segment sum input, not a contract book. "
        "A qualitative factor (for example FX without an admitted numerical mechanism) belongs "
        "in assessment or an explicitly unavailable operating metric, not an unexecuted "
        "revenue_drivers declaration. Never fabricate a member, historical amount, unit or "
        "conversion basis to fill a formula. Where original bases are unavailable, state "
        "the exact missing material in assessment; such plans cannot publish a forecast "
        "until the normal material-acquisition and source-admission gates resolve it.\n"
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
        "Every financial_statement_structure.lines entry must include every required "
        "OUTPUT_SCHEMA field, including a nonempty source-grounded label; do not omit "
        "fields because they seem repetitive. Include null for a nullable field that "
        "does not apply (annual_forecast_method is null for all non-share lines). "
        "Then return financial_statement_structure: the exact duration income "
        "lines and arithmetic this company disclosed, plus any filed cash-flow "
        "leaves (share_repurchase, dividends_paid, acquisitions) and balance-sheet "
        "leaves (cash_and_equivalents, debt, working capital) the model needs. "
        "Use consolidated filed "
        "concepts only; derived sums must follow this presentation and tie to "
        "a filed subtotal. Every derived line sets concept to null. Put its "
        "exact filed historical tie only in the associated formula's "
        "tie_out_concept. For example, the valid field placement is a line "
        "with {\"ref\":\"operating-income\",\"kind\":\"derived\","
        "\"concept\":null} and its formula with "
        "{\"output_ref\":\"operating-income\","
        "\"tie_out_concept\":\"us-gaap:OperatingIncomeLoss\"}. "
        "Every non-null expense_lines[].basis_concept selected above must also "
        "appear as the concept of a filed line in "
        "financial_statement_structure. Every filed line with at least eight "
        "quarters of history needs a forecast method: quarterly_growth for "
        "revenue and diluted shares without significant seasonality; "
        "year_over_year_growth or seasonal_factor for stable quarterly seasonal patterns "
        "(average quarterly_growth is prohibited there); share_of_line with an exact base ref for "
        "costs (base revenue), income tax (base pretax income) and "
        "noncontrolling interest (base net income); zero_one_off for a line "
        "whose behaviour is one_off; rate_times_balance (base a balance-sheet "
        "line) or carry_forward_mean for interest and other non-operating "
        "items; carry_forward_mean for other recurring income-statement lines, never "
        "for balance-sheet instants; unavailable only for a line with less history. "
        "Every balance-sheet leaf has statement balance, "
        "period_kind instant and annual_semantics not_applicable: accounts_receivable, "
        "accounts_payable, deferred_revenue, debt and other asset/liability leaves use "
        "hold_last, or share_of_line with a legitimate exact revenue base, or unavailable "
        "when its actual basis is missing. Only cash_and_equivalents may additionally "
        "use roll_forward. A balance-sheet leaf never uses carry_forward_mean. "
        "Derived lines use formula. Missing "
        "preferred-dividend, participating-security or convertible evidence "
        "stays unavailable; never treat it as zero. Include "
        "diluted_weighted_average_shares and a derived diluted_eps when the "
        "company files them. Diluted EPS divides the company's disclosed "
        "diluted_eps_numerator by diluted_weighted_average_shares. Parent net "
        "income is not automatically that numerator. "
        f"{EPS_NUMERATOR_BRIDGE_GUIDANCE} Use "
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
        "formula-derived and tied to that exact filed result. A filed standard "
        "subtotal (gross profit, operating income, pretax income, net income, or a "
        "company total such as total operating expenses) is tie authority only and "
        "never a formula term and never given independent growth or shares: "
        "forecast it as a derived line whose sum formula adds its filed "
        "components and ties to the filed total (a company total is then a "
        "company_presented_subtotal). Pretax income is operating income plus "
        "every non-operating item; tax is a rate of pretax income; net income, "
        "noncontrolling interest and attributable net income follow. Every "
        "formula term must itself be forecastable. Choose the structure backward "
        "from a final earnings bridge that ties in every applicable source "
        "period. The code completes missing methods and replaces a filed subtotal "
        "used as a term with its tied sum of components when the history proves "
        "one, but a structure whose earnings chain still cannot be computed is "
        "refused. "
        "A result tied to income including noncontrolling interests uses net_income; "
        "income_from_continuing_operations is reserved for the continuing-income "
        "result before any separate noncontrolling-interest attribution bridge. "
        "The only tie tolerance is the filed presentation rounding of a period; "
        "never an arbitrary or global tolerance, rewriting a filed amount, or "
        "inventing a balancing term; "
        "per-share division uses each period's disclosed filed precision. Do not assign independent "
        "growth or shares to formula-derived totals as a substitute for the bridge. "
        "A historically replay-ready formula does not make unavailable inputs forecastable. "
        "When the company directly presents pretax income as gross profit less "
        "company-specific expense totals, the pretax sum may contain exactly one "
        "gross_profit term with coefficient 1 and one or more "
        "company_presented_component or company_presented_subtotal terms. "
        "When the company instead directly presents pretax income as revenue less "
        "company-specific total costs, the same narrow bridge may use exactly one "
        "revenue term with coefficient 1. Do not mix either direct bridge with "
        "operating_income or other standard roles, "
        "relabel a standard filed fact as company-presented, or invent an untied "
        "subtotal. Each derived subtotal and the pretax result still needs its own "
        "exact filed tie. "
        "Every line "
        "also returns annual_forecast_method. It is null except for diluted "
        "weighted-average shares. Use unavailable there unless this exact company "
        "supports day_weighted_quarters and NUMERIC_PERIODS contains four "
        "positive contiguous quarter averages that tie to a direct annual share "
        "value. Alternatively annual_growth_from_filed explicitly projects annual "
        "shares from at least two consecutive directly filed 10-K annual diluted "
        "share values; this is a forecast assumption, never a historical quarter-to-year tie. "
        "quarterly_growth does not itself authorize annual weighting.\n\n"
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
        f"{output_schema['properties']['assessment']['maxLength']} characters. The output is "
        "a modelling specification; investment scenarios and tracking tasks have their own "
        "downstream consumers. Do not squeeze a full scenario discussion into every because.\n"
        "* Return compact JSON matching OUTPUT_SCHEMA and nothing else. Do not "
        "pretty-print or add indentation or insignificant whitespace; preserve every "
        "required semantic field. This reduces formatting overhead but does not change "
        "the output-token limit.\n\n"
        f"{template}\n\n"
        f"{cost_prompt_block(state.get('industry_classification'), state.get('concepts') or ())}\n\n"
        f"OUTPUT_SCHEMA:\n{json.dumps(output_schema, ensure_ascii=False)}\n\n"
        f"COMPANY:\n{json.dumps(company, ensure_ascii=False, sort_keys=True)}\n\n"
        "MARKET PROXIES (never company actuals; preserve each proxy_gap when "
        "citing one):\n"
        f"{json.dumps(state.get('market_proxies') or [], ensure_ascii=False, sort_keys=True)}\n\n"
        f"STATEMENTS:\n{_statement_table(state)}\n"
        f"\nNUMERIC_PERIODS:\n{_numeric_period_table(state)}\n"
        f"\nFINANCIAL_NOTE_EVIDENCE:\n{_financial_note_table(state)}\n"
        + ("\nMATERIAL FORECAST DEVIATION REVIEW (frozen forecasts remain unchanged):\n"
           "Reassess the affected methods and assumptions using the exact comparison sources below. "
           "Do not copy a newer model as proof of correction, invent missing evidence, or change old forecasts. "
           "Use the same required output schema and source/financial validation rules.\n"
           + canonical_json(state["model_redraft_context"]) + "\n"
           if state.get("model_redraft_context") else "")
    )


# What a specification cannot be written without, per statement.  Named here
# rather than discovered from a traceback: "financial_statement_structure is
# invalid" tells an operator that something is wrong and nothing about what to
# go and fetch, and the answer is nearly always that one filing has not been
# read yet.
REQUIRED_STATEMENT_ROLES: dict[str, tuple[tuple[str, str], ...]] = {
    "income": (
        ("revenue", "营业收入"),
        ("operating income", "营业利润"),
        ("net income", "净利润"),
        ("weighted average", "稀释加权平均股数"),
    ),
    "balance": (
        ("total assets", "资产总计"),
        ("total liabilities", "负债总计"),
        ("equity", "股东权益"),
    ),
    "cash": (
        ("operating activities", "经营活动现金流"),
        ("capital expenditure", "资本开支"),
    ),
}


# A filed pretax subtotal answers "operating income" for a company that
# presents none.  IBM's income statement runs Revenue, Cost, Gross profit,
# "Expense and other (income)" (SG&A, R&D, IP income, other (income) and
# expense, interest expense), "Total expense and other (income)"
# (``ibm:ExpenseAndIncomeOther``) and then "Income from continuing operations
# before income taxes"; no ``us-gaap:OperatingIncomeLoss`` exists in any of its
# nine held filings.  The structure contract bridges exactly that presentation
# (gross profit or revenue less company-presented totals = pretax income), so
# no structure rule depends on an operating subtotal IBM does not file, and the
# refusal of 2026-09-27 09:12Z blaming one sent the reader after a line that
# does not exist.  Pretax income is not *called* operating income here; it is
# the line the company presents in that position.  Exact concepts only.
PRETAX_INCOME_CONCEPTS: tuple[str, ...] = (
    "us-gaap:IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest",
    "us-gaap:IncomeLossFromContinuingOperationsBeforeIncomeTaxesMinorityInterestAndIncomeLossFromEquityMethodInvestments",
)

# Exact concepts that answer a required line whatever the filer calls it, and
# the statements they may be filed on. A label is the filer's wording, and two
# of the most common are misses for the needles above: Amazon's capital
# expenditure is "Purchases of property and equipment" under
# ``PaymentsToAcquireProductiveAssets``, and its ``NetIncomeLoss`` is parsed
# onto the cash-flow statement only. The concept is the filer's claim about
# what the number *is*; a label is not, so concepts are matched exactly.
REQUIRED_ROLE_CONCEPTS: dict[tuple[str, str], tuple[tuple[str, ...], tuple[str, ...]]] = {
    ("income", "operating income"): (
        ("us-gaap:OperatingIncomeLoss", *PRETAX_INCOME_CONCEPTS), ("income",)),
    ("income", "net income"): (NET_INCOME_CONCEPTS, ("income", "cash")),
    ("cash", "capital expenditure"): (
        CASH_FLOW_ROLE_CONCEPTS["capital_expenditure"], ("cash",)),
}
# Wordings that mean capital expenditure and nothing else. Anchored at the
# start so "Proceeds from property and equipment sales" is never read as an
# outflow; diagnostic only -- a value is only ever taken by exact concept.
_CAPEX_LABEL = re.compile(
    r"^\s*(?:purchases?|additions?|payments?|acquisitions?)\s+(?:of|to|for)\s+"
    r"(?:property|premises|fixed assets|capital assets|productive assets)"
    r"|capital expenditures?",
    re.IGNORECASE,
)
_ROLE_LABELS: dict[tuple[str, str], re.Pattern[str]] = {
    ("cash", "capital expenditure"): _CAPEX_LABEL,
}


def _role_answered(state: Mapping[str, Any], statement: str, needle: str) -> bool:
    """Whether an exact concept or an unambiguous wording answers this line."""

    statements = state.get("statements") or {}
    concepts, where = REQUIRED_ROLE_CONCEPTS.get((statement, needle), ((), ()))
    pattern = _ROLE_LABELS.get((statement, needle))
    for name in where or (statement,):
        for row in statements.get(name) or []:
            if (not isinstance(row, Mapping) or row.get("is_breakdown")
                    or row.get("dimension_axis")):
                continue
            if row.get("concept") in concepts:
                return True
            if (pattern is not None and name == statement
                    and pattern.search(str(row.get("label") or ""))):
                return True
    return False


def financial_line_gaps(state: Mapping[str, Any]) -> list[dict[str, str]]:
    """Which company, which statement, which line item is not there yet.

    A refusal an operator can act on.  ``financial_statement_structure is
    invalid: diluted weighted-average shares require duration direct_annual
    shares`` is true and useless; "Accenture's income statement carries no
    weighted-average share line -- the 10-K has not been read" is the same
    fact with the next action attached.

    Matched on the filed *label* rather than on a concept name, because the
    concept namespace is the filer's and ``us-gaap:Revenues`` versus
    ``acn:RevenuesNet`` is exactly the sort of near-miss this report exists to
    stop anyone from papering over.
    """

    company_ref = str(state.get("company_ref") or "(unknown company)")
    statements = state.get("statements") or {}
    gaps: list[dict[str, str]] = []
    for statement in STATEMENTS:
        rows = statements.get(statement) or []
        if not rows:
            gaps.append({
                "company_ref": company_ref, "statement": statement,
                "line": "(the whole statement)",
                "detail": f"{company_ref} 的{statement}报表在已读的申报里一行也没有；"
                          "先把这家公司的年报/季报抽取完成，再谈建模",
            })
            continue
        labels = " \n".join(
            str(row.get("label") or "").lower() for row in rows
            if isinstance(row, Mapping))
        concepts = " \n".join(
            str(row.get("concept") or "").lower() for row in rows
            if isinstance(row, Mapping))
        for needle, chinese in REQUIRED_STATEMENT_ROLES[statement]:
            if needle in labels or needle.replace(" ", "") in concepts.replace(" ", ""):
                continue
            if _role_answered(state, statement, needle):
                continue
            gaps.append({
                "company_ref": company_ref, "statement": statement,
                "line": needle,
                "detail": f"{company_ref} 的{statement}报表里找不到「{chinese}」"
                          f"（匹配词 {needle!r}）；这一行缺失时，依赖它的结构规则只能拒绝",
            })
    return gaps


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
                        "quarterly_growth", "year_over_year_growth", "seasonal_factor", "share_of_line",
                    }):
                raise CompanyModelSpecError(
                    f"cash_flow_companion {role} base is not a forecastable filed line",
                    code=(CASH_FLOW_OPERATING_BASE_SHAPE_CODE
                          if role == "operating_cash_flow" else "semantic"))
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
    from .model_spec_typed_field_repair import revenue_identity_scope, IDENTITY_CODE
    try:
        identity_scope = revenue_identity_scope(state, body)
    except ValueError as exc:
        raise CompanyModelSpecError(str(exc)) from exc
    if identity_scope is not None:
        raise CompanyModelSpecError(
            "dimensional revenue drivers must select distinct exact source-bound member identities",
            code=IDENTITY_CODE, proof=identity_scope)
    for index, item in enumerate(raw_drivers):
        if not isinstance(item, Mapping):
            raise CompanyModelSpecError(f"revenue_drivers[{index}] must be an object")
        if item.get("basis_identity") is not None:
            from .company_model_identity_catalog import resolve_identity
            try:
                identity=resolve_identity(state, item.get("basis_concept"), item["basis_identity"])
            except ValueError as exc: raise CompanyModelSpecError(str(exc)) from exc
        drivers.append({
            **({"basis_identity":identity} if item.get("basis_identity") is not None else {}),
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
        if item.get("periodicity") not in PERIODICITY:
            from .model_spec_typed_field_repair import proof, PERIODICITY_CODE
            raise CompanyModelSpecError(
                f"operating_metrics[{index}].periodicity must be quarterly or annual",
                code=PERIODICITY_CODE, proof=proof(body, PERIODICITY_CODE,
                    [["operating_metrics", index, "periodicity"]],
                    detail="Correct this temporal enum only; do not change metric, source, period, or any other field."))
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
        import re
        match = re.fullmatch(r"lines\[(\d+)\] acquisitions cannot use zero_one_off", str(exc))
        if isinstance(exc, FinancialStatementStructureError) and match:
            from .model_spec_typed_field_repair import proof, ACQUISITIONS_CODE
            index = int(match.group(1))
            raise CompanyModelSpecError(str(exc), code=ACQUISITIONS_CODE,
                proof=proof(body, ACQUISITIONS_CODE,
                    [["financial_statement_structure", "lines", index, field]
                     for field in ("forecast_method", "forecast_base_ref")],
                    detail="Choose a supported cash-line method and base, or unavailable with null base; preserve every filed authority and amount.")) from exc
        raise CompanyModelSpecError(
            f"financial_statement_structure is invalid: {exc}",
            code=structure_error_code(str(exc)),
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
        "assessment": _text(body.get("assessment"), "assessment", limit=MAX_ASSESSMENT_CHARS,
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
    if "driver_formulas" not in body:
        # A single total/mix identity is already executable and remains
        # compatible with old producers; disaggregated declarations need plans.
        identity = (len(drivers) == 1 and drivers[0]["kind"] == "mix"
                    and drivers[0].get("basis_concept") == revenue_anchor)
        if not identity:
            raise CompanyModelSpecError(
                "driver_formulas is required for declared economic driver execution; "
                "missing driver refs: " + ", ".join(d["ref"] for d in drivers))
    if "driver_formulas" in body:
        from .forecast_financial_execution import validate_driver_formulas
        try:
            spec["driver_formulas"] = validate_driver_formulas(body["driver_formulas"], drivers, statement_structure)
            used = {ref for formula in spec["driver_formulas"] for ref in formula["driver_refs"]}
            missing = sorted({driver["ref"] for driver in drivers} - used)
            if missing:
                raise ValueError("driver_formulas does not execute every declared economic driver; missing driver refs: " + ", ".join(missing))
        except ValueError as exc:
            raise CompanyModelSpecError(str(exc)) from exc
    if "fcf_definition" in body:
        from .forecast_company_fcf import validate_fcf_definition
        try:
            spec["fcf_definition"] = validate_fcf_definition(body["fcf_definition"], concepts)
        except ValueError as exc:
            raise CompanyModelSpecError(str(exc)) from exc
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
    "REPAIRABLE_STRUCTURE_RULES",
    "REPAIRABLE_STRUCTURE_RULES_REF",
    "structure_error_code",
    "REQUIRED_STATEMENT_ROLES",
    "financial_line_gaps",
    "build_prompt",
    "spec_template_gaps",
    "template_for",
    "forecast_statements",
    "parse_response",
    "spec_from_response",
    "undisclosed_metrics",
]
