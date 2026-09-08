"""P11h: find a metric in an issuer's own XBRL taxonomy, whatever it calls it.

The same figure is tagged differently by different filers -- ``Revenues``,
``RevenueFromContractWithCustomerExcludingAssessedTax``,
``SalesRevenueServicesNet`` -- and the list cannot be enumerated in advance.
The SEC lane carried a hardcoded three-concept candidate list, so an issuer that
tags revenue any other way simply produced nothing.

An LLM is not needed for this.  The issuer has already tagged its own line item
with a standard concept, so the display label in the filing ("Net revenues" vs
"Total revenue") is irrelevant: the resolution is over the concepts the issuer
actually declares, which the company-facts payload already carries.

What is needed is care about *which* match, because keyword matching alone is
not merely imprecise here, it is wrong.  Every one of these mentions revenue in
ACN's own taxonomy:

    CostOfRevenue                     -- an expense
    DeferredRevenueCurrent            -- a balance, not a period figure
    RevenueRemainingPerformanceObligation -- backlog
    OperatingLeasesIncomeStatementSubleaseRevenue -- sublease income
    ReimbursementRevenue              -- deprecated in 2018
    Revenues                          -- the one that is meant

So a match is scored, disqualifying terms are disqualifying rather than
low-scoring, and the alternatives are reported alongside the choice.  A silent
wrong pick is the failure that matters: it produces a number that is real,
cited, internally consistent and about something else entirely.

This covers what XBRL tags -- the three statements, largely.  Figures nobody
tags, like bookings or utilisation, are not here: those come out of prose, and
that is where a model has to read.
"""

from __future__ import annotations

import re
from typing import Any, Mapping, Sequence

SCHEMA_VERSION = "0.1"
DEFAULT_TAXONOMY = "us-gaap"

# Terms that change what a concept *is*, not merely how precise it is. A
# concept carrying one of these is not the metric being asked for, however well
# the rest of its name matches.
_DISQUALIFYING = (
    "costof", "deferred", "unearned", "remainingperformance", "sublease",
    "increasedecrease", "cumulativecatchup", "liabilityrevenuerecognized",
    "percentage", "pershare", "taxexpense", "accrued", "payable", "receivable",
    # A qualified variant is a different figure, and these are the ones that
    # look most like the answer. IBM does not tag OperatingIncomeLoss at all,
    # and without these the resolver quietly returned its discontinued
    # operations instead -- real, cited, and about something else.
    "disposalgroup", "discontinuedoperation", "proforma", "businessacquisition",
    "noncontrolling", "nonoperating", "availableforsale", "othercomprehensive",
    "segmentreporting", "relatedparty", "intersegment",
)
# Concepts the taxonomy has retired. A filer may still carry history under one,
# which is exactly how a resolver picks a concept that stopped being updated.
_DEPRECATED_MARKERS = ("deprecated",)


class ConceptResolutionError(ValueError):
    """The metric could not be resolved to a concept in this taxonomy."""


def _fold(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())


def _concept_facts(entry: Mapping[str, Any]) -> int:
    units = entry.get("units")
    if not isinstance(units, Mapping):
        return 0
    return sum(len(value) for value in units.values() if isinstance(value, list))


def _is_deprecated(entry: Mapping[str, Any]) -> bool:
    label = entry.get("label")
    text = label.lower() if isinstance(label, str) else ""
    return any(marker in text for marker in _DEPRECATED_MARKERS)


def score_concept(
    concept: str, entry: Mapping[str, Any], *, prefer: Sequence[str], require: Sequence[str],
) -> dict[str, Any] | None:
    """Score one concept for a metric, or refuse it outright.

    ``prefer`` is an ordered list of concept names or name fragments that mean
    this metric; earlier is better.  ``require`` is the wording a concept must
    contain at all.  A refusal names its reason so a resolution can be read
    afterwards and argued with.
    """

    folded = _fold(concept)
    if require and not any(_fold(term) in folded for term in require):
        return None
    for term in _DISQUALIFYING:
        if term in folded:
            return {"concept": concept, "rejected": f"contains {term}", "score": None}
    if _is_deprecated(entry):
        return {"concept": concept, "rejected": "deprecated in the taxonomy", "score": None}
    facts = _concept_facts(entry)
    if facts < 1:
        return {"concept": concept, "rejected": "no reported facts", "score": None}
    rank = None
    for index, term in enumerate(prefer):
        if folded == _fold(term):
            rank = index
            break
        if rank is None and _fold(term) in folded:
            rank = index + len(prefer)
    if rank is None:
        # Matches the required wording, is not disqualified, but is not a
        # named preference: usable only if nothing better exists.
        rank = 2 * len(prefer)
    return {"concept": concept, "rejected": None, "score": (rank, -facts), "facts": facts}


def resolve_concept(
    company_facts: Mapping[str, Any],
    *,
    prefer: Sequence[str],
    require: Sequence[str] = (),
    taxonomy: str = DEFAULT_TAXONOMY,
    max_alternatives: int = 5,
) -> dict[str, Any]:
    """Pick the concept an issuer uses for one metric, and show the working.

    Returns the chosen concept together with the alternatives considered and
    the reasons the rejected ones were rejected.  The reasons are the point: a
    resolution nobody can audit is a number nobody should trust.
    """

    facts = company_facts.get("facts")
    if not isinstance(facts, Mapping):
        raise ConceptResolutionError("company facts payload has no facts")
    concepts = facts.get(taxonomy)
    if not isinstance(concepts, Mapping) or not concepts:
        raise ConceptResolutionError(f"issuer declares no {taxonomy} concepts")
    if not prefer:
        raise ConceptResolutionError("a metric must name at least one preferred concept")
    scored: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    for concept, entry in concepts.items():
        if not isinstance(entry, Mapping):
            continue
        outcome = score_concept(concept, entry, prefer=prefer, require=require)
        if outcome is None:
            continue
        if outcome["score"] is None:
            rejected.append({"concept": outcome["concept"], "reason": outcome["rejected"]})
        else:
            scored.append(outcome)
    if not scored:
        raise ConceptResolutionError(
            "no concept in this issuer's taxonomy matches the metric; "
            f"{len(rejected)} were considered and refused"
        )
    scored.sort(key=lambda item: item["score"])
    chosen = scored[0]
    return {
        "schema_version": SCHEMA_VERSION,
        "taxonomy": taxonomy,
        "chosen_concept": chosen["concept"],
        "chosen_facts": chosen["facts"],
        # Everything that could have been chosen, so a wrong pick is visible
        # rather than being the only thing anyone ever sees.
        "alternatives": [
            {"concept": item["concept"], "facts": item["facts"]}
            for item in scored[1:1 + max_alternatives]
        ],
        "rejected": sorted(rejected, key=lambda item: item["concept"])[:max_alternatives],
    }


# What each metric is called across filers, best first. These are *preferences
# over an issuer's own concepts*, not a list of every name in existence: an
# issuer tagging revenue as something unlisted still resolves, it just ranks
# below the named ones instead of being invisible.
METRIC_CONCEPTS: Mapping[str, dict[str, Any]] = {
    "metric:revenue": {
        "prefer": (
            "Revenues",
            "RevenueFromContractWithCustomerExcludingAssessedTax",
            "RevenueFromContractWithCustomerIncludingAssessedTax",
            "SalesRevenueNet",
            "SalesRevenueServicesNet",
        ),
        "require": ("revenue", "sales"),
    },
    "metric:net-income": {
        "prefer": (
            "NetIncomeLoss",
            "ProfitLoss",
            "NetIncomeLossAvailableToCommonStockholdersBasic",
        ),
        "require": ("netincome", "profitloss"),
    },
    "metric:operating-income": {
        "prefer": ("OperatingIncomeLoss",),
        "require": ("operatingincome",),
    },
}


def resolve_metric(
    company_facts: Mapping[str, Any], metric_ref: str, *, taxonomy: str = DEFAULT_TAXONOMY
) -> dict[str, Any]:
    """Resolve one declared metric against this issuer's taxonomy."""

    spec = METRIC_CONCEPTS.get(metric_ref)
    if spec is None:
        raise ConceptResolutionError(
            f"{metric_ref} has no XBRL concept preference; it is not an XBRL-tagged figure"
        )
    resolved = resolve_concept(
        company_facts, prefer=spec["prefer"], require=spec["require"], taxonomy=taxonomy
    )
    return {**resolved, "metric_ref": metric_ref}


__all__ = [
    "ConceptResolutionError",
    "DEFAULT_TAXONOMY",
    "METRIC_CONCEPTS",
    "resolve_concept",
    "resolve_metric",
    "score_concept",
]
