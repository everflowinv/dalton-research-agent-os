"""P12b: the closed list of things a Claim can be *about*.

Live, ``metric_or_aspect`` holds free text -- "demand environment", "Demand
environment", "AI/reinvention demand", "discretionary project demand" are four
strings for one subject -- and nothing groups them.  2,170 Claims with 1,400
distinct labels is a pile, not a file.

So an index entry carries an ``aspect`` from this list and nothing else.  The
list is deliberately identical to the sections of the company dossier the
blueprint names (P12a ``CompanyDossierVersion``), because the whole point of
tagging is that the dossier can be assembled by grouping: section *k* of a
company's file is exactly the Claims whose aspect is *k*.  A vocabulary that
drifted from the sections would mean a second mapping nobody maintains.

Twelve words: the ten dossier sections, plus ``industry`` for claims whose
subject is the industry rather than a company (the dossier is per company, so
those have no section), plus ``other`` for a claim that genuinely is not one of
the eleven.  ``other`` is not a dustbin for "the tagger was unsure": a refused
batch stays untagged and is looked at again, whereas ``other`` is an assertion.

The one-line definitions below are the definitions the tagging prompt shows the
model, verbatim.  Changing a word here changes what was asked, so the task hash
in ``claim_index_tagging`` is computed over this table.
"""

from __future__ import annotations

from types import MappingProxyType
from typing import Any, Mapping

SCHEMA_VERSION = "0.1"

# The ten dossier sections, in the order P12a lists them, then the two the
# dossier has no section for.
ASPECTS: tuple[str, ...] = (
    "business_model",
    "segments_and_mix",
    "demand_drivers",
    "supply_and_cost",
    "competitive_position",
    "management_and_capital_allocation",
    "guidance_style",
    "kpi_dictionary",
    "catalyst_calendar",
    "history_of_price_drivers",
    "industry",
    "other",
)

ASPECT_SET: frozenset[str] = frozenset(ASPECTS)

# One line each, written to be told apart from its neighbours rather than to be
# complete.  These are shown to the model exactly as they are.
DEFINITIONS: Mapping[str, str] = MappingProxyType({
    "business_model": (
        "how the company earns money: what it sells, to whom, on what contract "
        "shape, and how revenue is recognised"
    ),
    "segments_and_mix": (
        "the split of the business -- segments, geographies, service lines, "
        "customer types -- and how that mix is shifting"
    ),
    "demand_drivers": (
        "what makes customers spend more or less: end-market conditions, "
        "budgets, discretionary versus committed work, pipeline, bookings"
    ),
    "supply_and_cost": (
        "what it costs to deliver: headcount, wages, utilisation, attrition, "
        "capacity, subcontractors, input prices, margin structure"
    ),
    "competitive_position": (
        "who it competes with and how it wins or loses: share, pricing power, "
        "differentiation, partnerships, displacement"
    ),
    "management_and_capital_allocation": (
        "who runs it and what they do with the cash: leadership changes, M&A, "
        "buybacks, dividends, capex, balance-sheet decisions"
    ),
    "guidance_style": (
        "how management talks about the future: guidance given, how it was "
        "framed, how it compared with the eventual result, revisions"
    ),
    "kpi_dictionary": (
        "the definition of a measure this company or its market uses -- what "
        "the number counts, how it is computed, how the definition changed"
    ),
    "catalyst_calendar": (
        "a dated future event that could move the view: results dates, "
        "investor days, contract renewals, regulatory decisions, deal closes"
    ),
    "history_of_price_drivers": (
        "what has actually moved the stock or the market's view of it, and why"
    ),
    "industry": (
        "a statement about the industry or market as a whole rather than about "
        "one company"
    ),
    "other": (
        "a statement about the company that is genuinely none of the above"
    ),
})

# The aspect a claim whose subject is an industry always gets.  Deterministic:
# the subject decides it, and no model is asked.
INDUSTRY_ASPECT = "industry"
FALLBACK_ASPECT = "other"


class ClaimAspectError(ValueError):
    """A word that is not in the closed aspect vocabulary."""


def is_aspect(value: Any) -> bool:
    """Whether this is one of the twelve words."""

    return isinstance(value, str) and value in ASPECT_SET


def require_aspect(value: Any, name: str = "aspect") -> str:
    """The aspect, or a refusal that names what was allowed."""

    if not is_aspect(value):
        raise ClaimAspectError(
            f"{name} must be one of {', '.join(ASPECTS)}; got {value!r}"
        )
    return value


def definition(aspect: str) -> str:
    """The one line the tagging prompt shows for this aspect."""

    return DEFINITIONS[require_aspect(aspect)]


def vocabulary_table() -> str:
    """The vocabulary as a tab-separated table, for a prompt.

    A table rather than JSON for the reason ``company_model_spec`` gives: the
    same content as objects costs several times the bytes in repeated keys, and
    the router reserves budget against the size of the prompt.
    """

    return "\n".join(f"{word}\t{DEFINITIONS[word]}" for word in ASPECTS)


__all__ = [
    "ASPECTS",
    "ASPECT_SET",
    "DEFINITIONS",
    "FALLBACK_ASPECT",
    "INDUSTRY_ASPECT",
    "SCHEMA_VERSION",
    "ClaimAspectError",
    "definition",
    "is_aspect",
    "require_aspect",
    "vocabulary_table",
]
