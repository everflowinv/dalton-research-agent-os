"""P12e: the industry framework -- the causal chain, the drivers, five companies
side by side, and an honest list of what is still missing.

The blueprint's line for this deliverable is short: *Constitution causal chain +
industry-level Claims + a five-company cross-comparison; TAM, supply and
high-frequency-data gaps listed honestly, as the basis for connecting
Guidepoint and IR.*  Four decisions make that a versioned object rather than a
longer weekly brief.

**The sections are the Constitution's causal chain, one per link.**  Not a
shape the model chose and not a shape this module chose: ``method.causal_chain``
is human-published and hash-bound, and the industry framework is the document
whose whole subject *is* that chain.  A model that invents its own headings has
replaced the methodology with its own, and the version chain would record the
swap as prose.  A chain whose links this policy has not titled is
``causal_chain_unmapped`` rather than free-form -- the same refusal the dossier
makes, for the same reason.

**The cross-company table is computed, and the model writes only around it.**
Every cell comes out of ``company_model_inputs``' ModelInputTable by a frozen
formula, carries the filed accessions it was computed from, and is citable by
tag.  The alternative -- asking a model to read five companies' filings and
tabulate them -- is the one thing this repository has never allowed, because a
tabulation nobody can recompute is an assertion with a table's authority.

**The gap list is the S-line's input, so it is derived, not written.**  Each
checklist item names the driver-pack drivers it would inform and the
``SourceCapabilityMap`` content kind that would fill it; whether it is open is
read off what the record actually covers, and which source could fill it is
read off the capability map together with the mission's own connection status
and the governed daily quota.  A gap list a model wrote would list the gaps the
model found interesting.

**A version that learned nothing is refused.**  ADR-0008: a new version cites
at least one ref the current one does not, carries a ``change_reason`` from the
shared vocabulary, and never edits or deletes what an earlier version said.
There is one wrinkle this object has and the dossier does not: a filing landing
moves the comparison table without moving a word of prose, and that *is* new
evidence -- so the table's accessions are part of the evidence scope, and the
table's own hash is stored beside the body hash.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Mapping, Sequence
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from types import MappingProxyType
from typing import Any, Iterator

# The dossier's classification vocabulary, imported rather than restated: the
# brief asks for characteristics "aligned with the dossier's
# industry_classification", and two closed lists claiming to be the same list
# is how an alignment stops being one.
from .company_dossier import INDUSTRY_CLASSIFICATIONS
from .company_model_inputs import build_model_inputs
# ADR-0008's reason vocabulary, shared with Wave 1C's forecast models and the
# dossier for the same reason.
from .model_forecast_driver import CHANGE_REASONS
from .store import DaltonStore, canonical_json, content_hash

SCHEMA_VERSION = "0.1"
_SCHEMA_PATH = Path(__file__).with_name("industry_framework_schema.sql")
GENERATOR_REF = "generator:industry-framework:0.1"
# The framework is a mission deliverable of kind ``industry_framework``, which
# is what the mission's ``may_write`` word has to grant.  Deliberately not a
# new word: ``industry_framework`` has been in ``DELIVERABLE_KINDS`` since
# Phase 9 with no generator behind it, and this is the generator.
WRITE_SCOPE = "deliverable"
DELIVERABLE_KIND = "industry_framework"

# ---------------------------------------------------------------------------
# closed vocabularies
# ---------------------------------------------------------------------------

# The units a run may draft.  The causal-chain sections are named by position,
# so a chain that gained a link gained a unit and the policy has to title it.
STATIC_UNITS: tuple[str, ...] = (
    "characteristics", "long_term_drivers", "short_term_drivers",
)
_CHAIN_UNIT_RE = re.compile(r"^causal_chain:(\d+)$")

# What a causal-chain section answers.  Two slots, because a link of the chain
# has two things a reader needs and they are different questions: what the
# evidence says about the mechanism, and where the covered companies part
# company on it.  One slot would let a section answer whichever it found easier.
SECTION_SLOTS: tuple[str, ...] = ("state", "divergence")
SECTION_SLOT_PROMPTS: Mapping[str, str] = MappingProxyType({
    "state": "what the shown material says about this link of the chain today, "
             "in the material's own terms",
    "divergence": "where the covered companies differ on this link, from the "
                  "computed table and the shown statements; say so if they do not",
})

# The industry's own characteristics.  Five closed fields, each answered with
# one word plus a basis.  ``classification`` is the dossier's own vocabulary
# (Deep Insight Gate question 1) so a company filed as ``structural_growth``
# and an industry called ``structural_growth`` mean the same thing.
CYCLICALITY: tuple[str, ...] = (
    "high_cyclical", "moderately_cyclical", "defensive", "secular_growth",
    "insufficient_evidence",
)
REVENUE_VISIBILITY: tuple[str, ...] = (
    "contracted_annuity", "backlog_led", "book_and_bill", "spot",
    "insufficient_evidence",
)
CAPITAL_INTENSITY: tuple[str, ...] = (
    "asset_light", "moderate", "asset_heavy", "insufficient_evidence",
)
CONCENTRATION: tuple[str, ...] = (
    "fragmented", "consolidating", "concentrated", "insufficient_evidence",
)
CHARACTERISTIC_FIELDS: Mapping[str, tuple[str, ...]] = MappingProxyType({
    "classification": INDUSTRY_CLASSIFICATIONS,
    "cyclicality": CYCLICALITY,
    "revenue_visibility": REVENUE_VISIBILITY,
    "capital_intensity": CAPITAL_INTENSITY,
    "concentration": CONCENTRATION,
})
CHARACTERISTIC_PROMPTS: Mapping[str, str] = MappingProxyType({
    "classification": "which of the Deep Insight Gate's five kinds of business "
                      "this industry is, and the strongest fact against it",
    "cyclicality": "how demand behaves against the macro cycle, and over what lag",
    "revenue_visibility": "how far ahead revenue is visible, and what makes it so",
    "capital_intensity": "what it costs to add a unit of capacity here",
    "concentration": "how the share is distributed and which way it is moving",
})
CHARACTERISTIC_SLOTS: tuple[str, ...] = tuple(
    f"basis:{field}" for field in CHARACTERISTIC_FIELDS
)

# The driver-pack stance vocabulary, imported in spirit from
# ``industry_research.DRIVER_STANCES``.  Restated as a tuple because that
# module holds a frozenset and a structure has to have an order; the members
# are checked against it at import so the two cannot drift.
DRIVER_STANCES: tuple[str, ...] = ("positive", "neutral", "negative", "mixed", "unknown")

HORIZONS: tuple[str, ...] = ("long_term", "short_term")

# Why a part is empty.  Closed, because "the material said nothing" and "the
# authority that would answer this is not on this Core" are different facts and
# only the second is a roadmap item.
UNAVAILABLE_REASONS: tuple[str, ...] = (
    # no canonical Claim carries the industry aspect on this Core
    "no_industry_claims",
    # the Constitution's chain is not titled by the policy
    "causal_chain_unmapped",
    # no company in the universe has a model specification met by filings
    "no_model_inputs",
    # the debate map authority holds no chain for this industry
    "no_debate_map",
    # the draft came back outside its contract and was refused whole
    "refused_by_verification",
    # not drafted on this run and no prior version to carry forward
    "not_drafted_this_run",
)

# What a sentence may cite.  Five kinds because a framework rests on five
# different authorities and a reader has to know which door to open.
REF_KINDS: tuple[str, ...] = (
    "claim", "figure", "comparison_cell", "dossier_section", "debate",
)

# Bounds.  Per slot and per unit, both enforced.
SLOT_SENTENCE_CAP = 3
UNIT_SENTENCE_CAP = 8
MAX_SENTENCE_CHARS = 400
MAX_SOURCES_PER_UNIT = 40
MAX_GAPS_PER_UNIT = 6
MAX_GAP_CHARS = 300

# ---------------------------------------------------------------------------
# the cross-company table: frozen concepts, frozen arithmetic
# ---------------------------------------------------------------------------

# The metrics the table carries.  Four, and the fourth is here precisely
# because it is usually empty: none of the five specifications binds an
# operating-income concept, so ``operating_margin`` comes back unavailable with
# a reason, and that is a gap the reader should see rather than a row the table
# quietly omits.
COMPARISON_METRICS: tuple[str, ...] = (
    "revenue", "revenue_yoy_growth", "gross_margin", "operating_margin",
)
METRIC_UNITS: Mapping[str, str] = MappingProxyType({
    "revenue": "reported",
    "revenue_yoy_growth": "percent",
    "gross_margin": "percent",
    "operating_margin": "percent",
})

# Which filed concept plays which role, in preference order.  Frozen in code
# rather than in the policy for the same reason ``ValuationSnapshot``'s formulas
# are: a comparison whose definitions can be edited per deployment is not a
# comparison.  Preference order matters only where a filer reports two of them.
REVENUE_CONCEPTS: tuple[str, ...] = (
    "us-gaap:Revenues",
    "us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax",
    "us-gaap:RevenueFromContractWithCustomerIncludingAssessedTax",
    "us-gaap:SalesRevenueNet",
    "us-gaap:SalesRevenueServicesNet",
)
COST_OF_REVENUE_CONCEPTS: tuple[str, ...] = (
    "us-gaap:CostOfRevenue",
    "us-gaap:CostOfGoodsAndServicesSold",
    "us-gaap:CostOfServices",
    "us-gaap:CostOfGoodsAndServiceExcludingDepreciationDepletionAndAmortization",
    "us-gaap:CostOfGoodsAndServicesSoldExcludingDepreciationDepletionAndAmortization",
)
OPERATING_INCOME_CONCEPTS: tuple[str, ...] = (
    "us-gaap:OperatingIncomeLoss",
)
# Cost concepts that leave depreciation and amortisation out.  A gross margin
# computed against one of these is not the same number as one computed against
# a cost line that includes D&A, and a table that put them in one column
# without saying so would be inviting the reader to read a ranking off an
# artefact of two filers' presentation choices.
COST_EXCLUDING_DANDA: frozenset[str] = frozenset({
    "us-gaap:CostOfGoodsAndServiceExcludingDepreciationDepletionAndAmortization",
    "us-gaap:CostOfGoodsAndServicesSoldExcludingDepreciationDepletionAndAmortization",
})

# How many calendar quarters of the table to carry.  Eight is two years, which
# is the span over which a year-on-year growth column has anything in it.
DEFAULT_COMPARISON_QUARTERS = 8
MAX_COMPARISON_QUARTERS = 16
# Ratios are quantised so that a cell is a value rather than a float's shortest
# repr, and the quantum is the same everywhere the table is read.
RATIO_QUANTUM = Decimal("0.000001")

_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
# The citation scaffolding as it appears when it leaks into prose.  Same
# refusal as the dossier's: tags travel in refs, and a sentence whose subject
# is a tag becomes a sentence with no subject once the tag is gone.
_PROSE_TAG_RE = re.compile(r"(?<![A-Za-z0-9])[CNT]\d{1,3}(?![A-Za-z0-9])")
_CJK_TERMINATORS = "。！？；」』）"
_PRINCIPALS = ("human:", "automation:")


class IndustryFrameworkError(RuntimeError):
    """Base error for the industry framework authority."""


class IndustryFrameworkValidationError(IndustryFrameworkError, ValueError):
    """A closed field or argument is invalid."""


class IndustryFrameworkConflict(IndustryFrameworkError):
    """The stored chain and the record disagree, or the caller raced it."""


class IndustryFrameworkNotFound(IndustryFrameworkError, LookupError):
    """No such industry framework version."""


class FrameworkStructureUnmapped(IndustryFrameworkError):
    """This Constitution's causal chain has no titles in the policy."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _text(value: Any, name: str, *, maximum: int = 2000) -> str:
    if not isinstance(value, str) or not value.strip():
        raise IndustryFrameworkValidationError(f"{name} must be non-empty text")
    value = value.strip()
    if len(value) > maximum:
        raise IndustryFrameworkValidationError(
            f"{name} must be at most {maximum} characters")
    return value


def _one_of(value: Any, allowed: Sequence[str], name: str) -> str:
    if value not in allowed:
        raise IndustryFrameworkValidationError(
            f"{name} must be one of {', '.join(allowed)}; got {value!r}")
    return str(value)


def _sha256(value: Any, name: str) -> str:
    value = _text(value, name, maximum=64)
    if _HASH_RE.fullmatch(value) is None:
        raise IndustryFrameworkValidationError(f"{name} must be lowercase SHA-256")
    return value


def _principal(value: Any, name: str = "actor_ref") -> str:
    value = _text(value, name, maximum=200)
    if not value.startswith(_PRINCIPALS):
        raise IndustryFrameworkValidationError(
            f"{name} must use a principal namespace")
    return value


def industry_slug(industry_ref: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "-", industry_ref).strip("-").lower()


def framework_ref_for(industry_ref: str) -> str:
    return f"industry-framework:{industry_slug(industry_ref)}"


# ---------------------------------------------------------------------------
# structure: what the Constitution decides and what the policy titles
# ---------------------------------------------------------------------------

DEFAULT_POLICY_PATH = (
    Path(__file__).resolve().parents[2] / "deploy" / "phase9"
    / "p12e-industry-framework-policy-v1.json"
)

# Every criterion of the Constitution's ``output_rubric`` is either bound to a
# named structural check here or declared not applicable by the policy.  A
# criterion the policy does not mention at all fails the pre-publish check.
OUTPUT_RUBRIC_CHECKS: tuple[str, ...] = (
    # every figure in the prose is carried verbatim by a cited source
    "numbers_trace_to_refs",
    # this version cites something the last one did not (ADR-0008)
    "not_a_restatement",
    # no part asserts an investment conclusion; a framework is a file, not a call
    "no_investment_conclusion",
    # every open gap names a source kind that could fill it, or says none can
    "open_gaps_name_a_source",
)


def causal_chain_hash(chain: Sequence[str]) -> str:
    """The identity of one causal chain: the whole chain, in order.

    A chain that gained, lost or reordered a link is a different chain, and its
    policy entry no longer applies.  That is the intended behaviour: which
    section a new link is, and what it should be called, is a methodology
    question only a person can answer.
    """

    return content_hash([str(link) for link in chain])


def load_policy(path: str | Path | None = None) -> dict[str, Any]:
    """The P12e policy: chain titles, driver horizons, the gap checklist."""

    source = Path(path) if path is not None else DEFAULT_POLICY_PATH
    return validate_policy(json.loads(source.read_text(encoding="utf-8")))


def validate_policy(policy: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(policy, Mapping):
        raise IndustryFrameworkValidationError("policy must be an object")
    wire = dict(policy)
    expected = {"schema_version", "policy_ref", "causal_chain_titles",
                "driver_horizons", "gap_checklist", "output_rubric_bindings"}
    if set(wire) != expected:
        raise IndustryFrameworkValidationError(
            "policy has an invalid closed shape; "
            f"missing={sorted(expected - set(wire))}, "
            f"unknown={sorted(set(wire) - expected)}")
    if wire["schema_version"] != SCHEMA_VERSION:
        raise IndustryFrameworkValidationError("policy schema_version is not supported")
    wire["policy_ref"] = _text(wire["policy_ref"], "policy_ref")

    titles = []
    for index, item in enumerate(wire["causal_chain_titles"] or []):
        if not isinstance(item, Mapping) or set(item) != {
            "constitution_ref", "causal_chain_hash", "titles", "note"
        }:
            raise IndustryFrameworkValidationError(
                f"causal_chain_titles[{index}] has an invalid closed shape")
        rows = item["titles"]
        if not isinstance(rows, list) or not rows:
            raise IndustryFrameworkValidationError(
                f"causal_chain_titles[{index}].titles must be a non-empty array")
        titles.append({
            "constitution_ref": _text(item["constitution_ref"], "constitution_ref"),
            "causal_chain_hash": _sha256(item["causal_chain_hash"], "causal_chain_hash"),
            "titles": [_text(row, f"causal_chain_titles[{index}].titles[]", maximum=200)
                       for row in rows],
            "note": str(item["note"] or ""),
        })
    wire["causal_chain_titles"] = titles

    horizons = []
    for index, item in enumerate(wire["driver_horizons"] or []):
        if not isinstance(item, Mapping) or set(item) != {"driver_ref", "horizons", "note"}:
            raise IndustryFrameworkValidationError(
                f"driver_horizons[{index}] has an invalid closed shape")
        rows = item["horizons"]
        if not isinstance(rows, list) or not rows:
            raise IndustryFrameworkValidationError(
                f"driver_horizons[{index}].horizons must name at least one horizon")
        assigned = [_one_of(row, HORIZONS, f"driver_horizons[{index}].horizons[]")
                    for row in rows]
        if len(set(assigned)) != len(assigned):
            raise IndustryFrameworkValidationError(
                f"driver_horizons[{index}].horizons repeats a horizon")
        horizons.append({
            "driver_ref": _text(item["driver_ref"], "driver_ref"),
            "horizons": assigned,
            "note": str(item["note"] or ""),
        })
    if len({item["driver_ref"] for item in horizons}) != len(horizons):
        raise IndustryFrameworkValidationError("driver_horizons repeats a driver_ref")
    wire["driver_horizons"] = horizons

    checklist = []
    for index, item in enumerate(wire["gap_checklist"] or []):
        fields = {"gap_ref", "label", "what_is_missing", "content_kind",
                  "driver_refs", "cost_note", "blocks_links"}
        if not isinstance(item, Mapping) or set(item) != fields:
            raise IndustryFrameworkValidationError(
                f"gap_checklist[{index}] has an invalid closed shape")
        links = item["blocks_links"]
        if not isinstance(links, list) or any(
            isinstance(row, bool) or not isinstance(row, int) or row < 0
            for row in links
        ):
            raise IndustryFrameworkValidationError(
                f"gap_checklist[{index}].blocks_links must be chain link indexes")
        drivers = item["driver_refs"]
        if not isinstance(drivers, list):
            raise IndustryFrameworkValidationError(
                f"gap_checklist[{index}].driver_refs must be an array")
        checklist.append({
            "gap_ref": _text(item["gap_ref"], "gap_ref", maximum=120),
            "label": _text(item["label"], "label", maximum=200),
            "what_is_missing": _text(item["what_is_missing"], "what_is_missing",
                                     maximum=MAX_GAP_CHARS),
            "content_kind": _text(item["content_kind"], "content_kind", maximum=60),
            "driver_refs": [_text(row, "driver_refs[]") for row in drivers],
            "cost_note": _text(item["cost_note"], "cost_note", maximum=MAX_GAP_CHARS),
            "blocks_links": [int(row) for row in links],
        })
    if len({item["gap_ref"] for item in checklist}) != len(checklist):
        raise IndustryFrameworkValidationError("gap_checklist repeats a gap_ref")
    if not checklist:
        raise IndustryFrameworkValidationError(
            "the gap checklist is what the S line reads; an empty one is a "
            "policy that claims nothing is missing")
    wire["gap_checklist"] = checklist

    bindings = []
    for index, item in enumerate(wire["output_rubric_bindings"] or []):
        if not isinstance(item, Mapping) or set(item) != {
            "criterion_hash", "checks", "reason"
        }:
            raise IndustryFrameworkValidationError(
                f"output_rubric_bindings[{index}] has an invalid closed shape")
        # A list rather than the dossier's single ``check``, because one of
        # this Constitution's criteria genuinely wants two structural readings.
        # "A good research output reduces the open question set" is both "cite
        # something new" and "say what is still missing"; a framework version
        # that did the first and not the second has not reduced the open
        # question set, and a shape that made the policy pick one would have
        # made that unenforceable.
        checks = item["checks"]
        if not isinstance(checks, list):
            raise IndustryFrameworkValidationError(
                f"output_rubric_bindings[{index}].checks must be an array")
        named = [_one_of(check, OUTPUT_RUBRIC_CHECKS,
                         f"output_rubric_bindings[{index}].checks[]")
                 for check in checks]
        if len(set(named)) != len(named):
            raise IndustryFrameworkValidationError(
                f"output_rubric_bindings[{index}].checks repeats a check")
        if not named and not str(item["reason"] or "").strip():
            raise IndustryFrameworkValidationError(
                f"output_rubric_bindings[{index}] declares no check and no reason")
        bindings.append({
            "criterion_hash": _sha256(item["criterion_hash"], "criterion_hash"),
            "checks": named,
            "reason": str(item["reason"] or ""),
        })
    wire["output_rubric_bindings"] = bindings
    return wire


def policy_hash(policy: Mapping[str, Any]) -> str:
    return content_hash(validate_policy(policy))


def chain_titles(
    constitution: Mapping[str, Any], policy: Mapping[str, Any]
) -> list[str]:
    """One title per causal-chain link, per the policy.  Never inferred."""

    chain = list((constitution.get("method") or {}).get("causal_chain") or [])
    if not chain:
        raise FrameworkStructureUnmapped("this Constitution states no causal chain")
    digest = causal_chain_hash(chain)
    for item in validate_policy(policy)["causal_chain_titles"]:
        if item["causal_chain_hash"] != digest:
            continue
        if len(item["titles"]) != len(chain):
            raise IndustryFrameworkConflict(
                "the policy's titles do not cover this causal chain link for link")
        return list(item["titles"])
    raise FrameworkStructureUnmapped(
        f"causal chain {digest[:12]} of {constitution.get('constitution_ref')} has no "
        "titles; a person titles its links before these sections can be drafted")


def driver_horizons(policy: Mapping[str, Any]) -> dict[str, list[str]]:
    return {item["driver_ref"]: list(item["horizons"])
            for item in validate_policy(policy)["driver_horizons"]}


def drivers_for_horizon(
    driver_pack: Mapping[str, Any], policy: Mapping[str, Any], horizon: str
) -> list[dict[str, Any]]:
    """The driver-pack drivers the policy files under one horizon.

    The pack supplies the vocabulary -- ref, label, mechanism, metric refs --
    and the policy supplies only the horizon.  Nothing here invents a driver:
    a framework that discussed a driver the pack does not carry would be
    discussing something no Claim can ever be bound to.
    """

    _one_of(horizon, HORIZONS, "horizon")
    assigned = driver_horizons(policy)
    out = []
    for driver in driver_pack.get("drivers") or []:
        ref = str(driver.get("driver_ref"))
        if horizon not in assigned.get(ref, ()):
            continue
        out.append({
            "driver_ref": ref,
            "label": str(driver.get("label") or ref),
            "mechanism": str(driver.get("mechanism") or ""),
            "metric_refs": [str(item) for item in driver.get("metric_refs") or []],
        })
    return out


def units_for(
    constitution: Mapping[str, Any], policy: Mapping[str, Any]
) -> tuple[str, ...]:
    """Every unit a run of this framework may draft, in record order."""

    titles = chain_titles(constitution, policy)
    return tuple(f"causal_chain:{index}" for index in range(len(titles))) + STATIC_UNITS


def unit_slots(
    unit: str,
    *,
    constitution: Mapping[str, Any] | None = None,
    policy: Mapping[str, Any] | None = None,
    driver_pack: Mapping[str, Any] | None = None,
) -> tuple[dict[str, str], ...]:
    """The slots one unit's draft must fill, and nothing the model chose."""

    match = _CHAIN_UNIT_RE.fullmatch(unit)
    if match is not None:
        if constitution is None or policy is None:
            raise FrameworkStructureUnmapped(
                "a causal-chain section takes its structure from the Constitution")
        chain = list((constitution.get("method") or {}).get("causal_chain") or [])
        index = int(match.group(1))
        if not 0 <= index < len(chain):
            raise FrameworkStructureUnmapped(
                f"{unit} names a link this Constitution's chain does not have")
        return tuple(
            {"slot_id": slot, "prompt": SECTION_SLOT_PROMPTS[slot]}
            for slot in SECTION_SLOTS
        )
    if unit == "characteristics":
        return tuple(
            {"slot_id": f"basis:{field}", "prompt": CHARACTERISTIC_PROMPTS[field]}
            for field in CHARACTERISTIC_FIELDS
        )
    if unit in ("long_term_drivers", "short_term_drivers"):
        if driver_pack is None or policy is None:
            raise FrameworkStructureUnmapped(
                "a driver block takes its structure from the driver pack")
        horizon = "long_term" if unit == "long_term_drivers" else "short_term"
        drivers = drivers_for_horizon(driver_pack, policy, horizon)
        if not drivers:
            raise FrameworkStructureUnmapped(
                f"the policy files no driver under {horizon}")
        return tuple(
            {"slot_id": f"driver:{driver['driver_ref']}",
             "prompt": f"{driver['label']} -- {driver['mechanism']}"}
            for driver in drivers
        )
    raise IndustryFrameworkValidationError(f"unknown framework unit: {unit!r}")




# The stance vocabulary is the industry evidence pack's, and it is checked
# rather than trusted: two closed lists that claim to be the same list have to
# fail loudly when they stop being it.
def _check_stance_vocabulary() -> None:
    from .industry_research import DRIVER_STANCES as _PACK_STANCES

    if set(DRIVER_STANCES) != set(_PACK_STANCES):
        raise IndustryFrameworkValidationError(
            "the framework's driver stances have drifted from the industry "
            "evidence pack's; they are one vocabulary")


# ---------------------------------------------------------------------------
# the cross-company comparison: computed here, never drafted
# ---------------------------------------------------------------------------


def calendar_quarter(period_end: str) -> str:
    """The calendar quarter a fiscal period end falls in.

    The five covered filers close on four different calendars -- Accenture in
    February, May, August and November; Cognizant, EPAM and DXC on the calendar
    quarters; IBM half-yearly in the statements held -- so there is no column
    header that is literally the same period for all of them.  Mapping each
    period end onto its calendar quarter is the alignment an analyst actually
    uses, and every cell keeps its own ``period_end`` beside the column so the
    approximation is visible rather than implied.
    """

    text = _text(period_end, "period_end", maximum=32)
    try:
        year = int(text[0:4])
        month = int(text[5:7])
    except (ValueError, IndexError) as exc:
        raise IndustryFrameworkValidationError(
            f"period_end {period_end!r} is not an ISO date") from exc
    if not 1 <= month <= 12:
        raise IndustryFrameworkValidationError(
            f"period_end {period_end!r} has no month")
    return f"{year}Q{(month - 1) // 3 + 1}"


def quarter_minus_year(quarter: str) -> str:
    """The same calendar quarter one year earlier."""

    text = _text(quarter, "quarter", maximum=16)
    if len(text) != 6 or text[4] != "Q" or not text[:4].isdigit() or text[5] not in "1234":
        raise IndustryFrameworkValidationError(f"{quarter!r} is not a calendar quarter")
    return f"{int(text[:4]) - 1}Q{text[5]}"


def _decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _pick_concept(
    table: Mapping[str, Any], preferred: Sequence[str]
) -> dict[str, Any] | None:
    """The first filed line of this table that plays a role, in preference order."""

    by_concept = {
        str(line["concept"]): line
        for line in table.get("filed_lines") or []
        if line.get("status") == "filed" and (line.get("cells") or {})
    }
    for concept in preferred:
        line = by_concept.get(concept)
        if line is not None:
            return line
    return None


def cell_ref(company_ref: str, metric: str, quarter: str) -> str:
    """A comparison cell's citable name.  Stable across runs by construction."""

    return f"comparison-cell:{industry_slug(company_ref)}:{metric}:{quarter}"


def _format_ratio(value: Decimal) -> str:
    """A ratio as a percent, to one decimal -- the way it is read and cited.

    One rendering, used both in the cell's own text and in whatever prose cites
    it, so that ``numbers_trace_to_refs`` compares like with like.  A model that
    recomputed the percentage to two decimals would be caught, which is the
    point.
    """

    percent = (value * 100).quantize(Decimal("0.1"))
    return f"{percent}%"


def _cell(
    *, company_ref: str, ticker: str, metric: str, quarter: str,
    period_end: str | None, status: str, value: str | None = None,
    display: str | None = None, basis: str | None = None,
    reason: str | None = None, inputs: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    refs: list[str] = []
    for item in inputs:
        for accession in item.get("source_accessions") or ():
            if str(accession) not in refs:
                refs.append(str(accession))
    return {
        "ref": cell_ref(company_ref, metric, quarter),
        "company_ref": company_ref,
        "ticker": ticker,
        "metric": metric,
        "quarter": quarter,
        "period_end": period_end,
        "status": status,
        "value": value,
        "display": display,
        "unit": METRIC_UNITS[metric],
        "basis": basis,
        "reason": reason,
        "inputs": [
            {"concept": str(item.get("concept") or ""),
             "period_end": str(item.get("period_end") or ""),
             "value": None if item.get("value") is None else str(item["value"])}
            for item in inputs
        ],
        "source_accessions": refs,
    }


def _company_table(
    table: Mapping[str, Any], *, company_ref: str, ticker: str,
) -> dict[str, Any]:
    """One company's roles, resolved from its ModelInputTable."""

    revenue = _pick_concept(table, REVENUE_CONCEPTS)
    cost = _pick_concept(table, COST_OF_REVENUE_CONCEPTS)
    operating = _pick_concept(table, OPERATING_INCOME_CONCEPTS)
    return {
        "company_ref": company_ref,
        "ticker": ticker,
        "spec_ref": table.get("spec_ref"),
        "state_hash": table.get("state_hash"),
        "revenue_concept": None if revenue is None else str(revenue["concept"]),
        "cost_of_revenue_concept": None if cost is None else str(cost["concept"]),
        "operating_income_concept": (
            None if operating is None else str(operating["concept"])),
        "_lines": {"revenue": revenue, "cost": cost, "operating": operating},
    }


def build_comparison(
    tables: Sequence[Mapping[str, Any]],
    *,
    universe: Sequence[Mapping[str, Any]],
    quarters: int = DEFAULT_COMPARISON_QUARTERS,
) -> dict[str, Any]:
    """The five-company table, computed from the ModelInputTables.

    Rows are company x metric, columns are calendar quarters, and every cell
    that has a value names the filed accessions it came from.  A cell that
    cannot be computed says which input was missing rather than being absent:
    the reader of a comparison has to be able to tell "flat" from "not filed",
    and a blank cell says neither.
    """

    if isinstance(quarters, bool) or not isinstance(quarters, int) or quarters < 1:
        raise IndustryFrameworkValidationError("quarters must be a positive integer")
    quarters = min(quarters, MAX_COMPARISON_QUARTERS)
    tickers = {str(member.get("company_ref")): str(member.get("ticker") or "")
               for member in universe}
    companies = []
    for table in tables:
        company_ref = str(table.get("company_ref") or "")
        if not company_ref:
            raise IndustryFrameworkValidationError(
                "a model input table carries no company_ref")
        companies.append(_company_table(
            table, company_ref=company_ref,
            ticker=tickers.get(company_ref, company_ref)))
    if not companies:
        return {
            "status": "unavailable", "reason": "no_model_inputs",
            "metrics": list(COMPARISON_METRICS), "quarters": [],
            "companies": [], "cells": [], "comparability_notes": [],
        }

    # The columns: every calendar quarter any company reached, newest last, cut
    # to the window.  A quarter no filer reached is not a column.
    seen: set[str] = set()
    for company in companies:
        line = company["_lines"]["revenue"]
        if line is None:
            continue
        for end in (line.get("cells") or {}):
            seen.add(calendar_quarter(str(end)))
    columns = sorted(seen)[-quarters:]

    cells: list[dict[str, Any]] = []
    for company in companies:
        lines = company["_lines"]
        # period end -> the cell, keyed by calendar quarter, per role.
        by_quarter: dict[str, dict[str, Any]] = {}
        for role, line in lines.items():
            if line is None:
                continue
            for end, cell in (line.get("cells") or {}).items():
                by_quarter.setdefault(calendar_quarter(str(end)), {})[role] = {
                    "concept": line["concept"], "period_end": str(end), **dict(cell),
                }
        for quarter in columns:
            found = by_quarter.get(quarter) or {}
            revenue = found.get("revenue")
            period_end = None if revenue is None else revenue["period_end"]

            if revenue is None:
                for metric in COMPARISON_METRICS:
                    cells.append(_cell(
                        company_ref=company["company_ref"], ticker=company["ticker"],
                        metric=metric, quarter=quarter, period_end=None,
                        status="unavailable",
                        reason=("no revenue line is filed for this company"
                                if lines["revenue"] is None
                                else "this company filed no revenue for this quarter"),
                    ))
                continue

            revenue_value = _decimal(revenue.get("value"))
            cells.append(_cell(
                company_ref=company["company_ref"], ticker=company["ticker"],
                metric="revenue", quarter=quarter, period_end=period_end,
                status="computed" if revenue_value is not None else "unavailable",
                value=None if revenue_value is None else str(revenue_value),
                display=None if revenue_value is None else str(revenue_value),
                basis=str(revenue.get("basis") or "reported"),
                reason=None if revenue_value is not None else "the filed value is not a number",
                inputs=[revenue],
            ))

            prior = (by_quarter.get(quarter_minus_year(quarter)) or {}).get("revenue")
            prior_value = None if prior is None else _decimal(prior.get("value"))
            if revenue_value is None or prior_value in (None, Decimal(0)):
                cells.append(_cell(
                    company_ref=company["company_ref"], ticker=company["ticker"],
                    metric="revenue_yoy_growth", quarter=quarter,
                    period_end=period_end, status="unavailable",
                    reason=("the same quarter a year earlier is not in the filings held"
                            if prior is None else "the year-earlier revenue is not usable"),
                    inputs=[revenue],
                ))
            else:
                growth = ((revenue_value - prior_value) / prior_value).quantize(RATIO_QUANTUM)
                cells.append(_cell(
                    company_ref=company["company_ref"], ticker=company["ticker"],
                    metric="revenue_yoy_growth", quarter=quarter,
                    period_end=period_end, status="computed", value=str(growth),
                    display=_format_ratio(growth), basis="derived",
                    inputs=[revenue, prior],
                ))

            cost = found.get("cost")
            cost_value = None if cost is None else _decimal(cost.get("value"))
            if revenue_value in (None, Decimal(0)) or cost_value is None:
                cells.append(_cell(
                    company_ref=company["company_ref"], ticker=company["ticker"],
                    metric="gross_margin", quarter=quarter, period_end=period_end,
                    status="unavailable",
                    reason=("no cost-of-revenue line is filed for this company"
                            if lines["cost"] is None
                            else "the cost of revenue for this quarter is not filed"),
                    inputs=[revenue],
                ))
            else:
                margin = ((revenue_value - cost_value) / revenue_value).quantize(RATIO_QUANTUM)
                cells.append(_cell(
                    company_ref=company["company_ref"], ticker=company["ticker"],
                    metric="gross_margin", quarter=quarter, period_end=period_end,
                    status="computed", value=str(margin),
                    display=_format_ratio(margin), basis="derived",
                    inputs=[revenue, cost],
                ))

            operating = found.get("operating")
            operating_value = None if operating is None else _decimal(operating.get("value"))
            if revenue_value in (None, Decimal(0)) or operating_value is None:
                cells.append(_cell(
                    company_ref=company["company_ref"], ticker=company["ticker"],
                    metric="operating_margin", quarter=quarter, period_end=period_end,
                    status="unavailable",
                    reason=("this company's model specification binds no operating "
                            "income concept, so the margin cannot be computed from "
                            "the filings held" if lines["operating"] is None
                            else "operating income for this quarter is not filed"),
                    inputs=[revenue],
                ))
            else:
                margin = (operating_value / revenue_value).quantize(RATIO_QUANTUM)
                cells.append(_cell(
                    company_ref=company["company_ref"], ticker=company["ticker"],
                    metric="operating_margin", quarter=quarter, period_end=period_end,
                    status="computed", value=str(margin),
                    display=_format_ratio(margin), basis="derived",
                    inputs=[revenue, operating],
                ))

    notes = comparability_notes(companies)
    return {
        "status": "computed",
        "reason": None,
        "metrics": list(COMPARISON_METRICS),
        "quarters": columns,
        "companies": [
            {key: value for key, value in company.items() if key != "_lines"}
            for company in companies
        ],
        "cells": cells,
        "comparability_notes": notes,
    }


def comparability_notes(companies: Sequence[Mapping[str, Any]]) -> list[str]:
    """What makes two columns of this table not the same measurement.

    Derived from the concepts actually used, not written: the reason CTSH's and
    DXC's gross margin is not ACN's is that their filed cost line leaves
    depreciation and amortisation out, and a table that let a reader rank the
    five on that number without saying so would be publishing a presentation
    choice as a finding.
    """

    notes: list[str] = []
    excluding = sorted(
        str(company["ticker"]) for company in companies
        if company.get("cost_of_revenue_concept") in COST_EXCLUDING_DANDA
    )
    including = sorted(
        str(company["ticker"]) for company in companies
        if company.get("cost_of_revenue_concept") is not None
        and company["cost_of_revenue_concept"] not in COST_EXCLUDING_DANDA
    )
    if excluding and including:
        notes.append(
            f"gross_margin is not comparable across the set: {', '.join(excluding)} "
            f"file a cost of revenue that excludes depreciation and amortisation, "
            f"{', '.join(including)} file one that includes it")
    missing_revenue = sorted(
        str(company["ticker"]) for company in companies
        if company.get("revenue_concept") is None
    )
    if missing_revenue:
        notes.append(
            f"no revenue row at all for {', '.join(missing_revenue)}: the model "
            "specification binds no filed revenue concept, so every cell in that "
            "row is unavailable rather than zero")
    missing_operating = sorted(
        str(company["ticker"]) for company in companies
        if company.get("operating_income_concept") is None
    )
    if missing_operating:
        notes.append(
            f"operating_margin cannot be computed for {', '.join(missing_operating)}: "
            "no operating-income concept is bound by the specification")
    notes.append(
        "columns are calendar quarters; each cell carries its own fiscal "
        "period_end, which can be up to one month from the column")
    return notes


def comparison_hash(comparison: Mapping[str, Any]) -> str:
    """The table's own identity, independent of the prose around it."""

    return content_hash({
        "status": comparison.get("status"),
        "quarters": list(comparison.get("quarters") or []),
        "cells": [
            {key: cell.get(key) for key in
             ("ref", "metric", "quarter", "period_end", "status", "value", "basis",
              "source_accessions")}
            for cell in comparison.get("cells") or []
        ],
    })


def comparison_accessions(comparison: Mapping[str, Any]) -> list[str]:
    """Every filed accession the table rests on, sorted.

    Part of the evidence scope: a newly filed quarter moves this table without
    moving a word of prose, and ADR-0008 should call that a new version rather
    than a duplicate.
    """

    found: set[str] = set()
    for cell in comparison.get("cells") or []:
        found.update(str(ref) for ref in cell.get("source_accessions") or ())
    return sorted(found)


def render_comparison(comparison: Mapping[str, Any], *, limit: int = 6) -> str:
    """The table as the prompt and the report carry it: tab-separated rows."""

    if comparison.get("status") != "computed":
        return f"(no comparison table: {comparison.get('reason')})"
    columns = list(comparison.get("quarters") or [])[-limit:]
    by_key = {(cell["company_ref"], cell["metric"], cell["quarter"]): cell
              for cell in comparison.get("cells") or []}
    lines = ["company\tmetric\t" + "\t".join(columns)]
    for company in comparison.get("companies") or []:
        for metric in comparison.get("metrics") or []:
            row = [str(company["ticker"]), metric]
            for quarter in columns:
                cell = by_key.get((company["company_ref"], metric, quarter))
                if cell is None or cell["status"] != "computed":
                    row.append("-")
                else:
                    row.append(str(cell["display"]))
            lines.append("\t".join(row))
    for note in comparison.get("comparability_notes") or []:
        lines.append(f"# {note}")
    return "\n".join(lines)


def comparison_material(
    comparison: Mapping[str, Any], *, limit: int = 40
) -> list[dict[str, Any]]:
    """The computed cells a draft may cite, as material rows.

    Only cells with a value: a cell that says "not filed" is a fact about the
    filings and belongs in the gap list, not in a sentence citing a number that
    is not there.
    """

    rows = []
    for cell in comparison.get("cells") or []:
        if cell["status"] != "computed":
            continue
        rows.append({
            "kind": "comparison_cell",
            "ref": cell["ref"],
            "text": (f"{cell['ticker']} {cell['quarter']}"
                     f"（期末 {cell['period_end']}）{cell['metric']} {cell['display']}"),
            "period": cell["quarter"],
            "importance": cell["basis"],
        })
    return rows[-limit:]


# ---------------------------------------------------------------------------
# the gap list: derived from the checklist, the record and the capability map
# ---------------------------------------------------------------------------

GAP_STATUSES: tuple[str, ...] = ("open", "partially_covered", "covered")


def stated_driver_refs(*blocks: Mapping[str, Any] | None) -> set[str]:
    """Which drivers this framework actually says something about.

    A slot answered ``unknown`` does not count.  The whole value of the gap
    list is that "we have a heading for it" and "we know something about it"
    are different, and a checklist that read the headings would report a
    framework as complete the moment it had been drafted once.
    """

    found: set[str] = set()
    for block in blocks:
        if not block or block.get("status") != "drafted":
            continue
        for slot in block.get("slots") or ():
            slot_id = str(slot.get("slot_id") or "")
            if not slot_id.startswith("driver:") or not slot.get("sentences"):
                continue
            found.add(slot_id.split(":", 1)[1])
    return found


def candidate_sources(
    content_kind: str, *, mission: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Which connectors could yield this content kind, and what they cost.

    Straight off ``SourceCapabilityMap``: the slugs that yield the kind,
    specific sources before the two generic ones, each with the mission's own
    word for whether it is connected and the governed daily quota.  An
    unrecognised content kind is a refusal rather than an empty list -- a gap
    that names a kind the map has never heard of is a policy typo, and
    answering it with silence would file the typo as "nothing can fill this".
    """

    from .source_capability_map import capability, connection_status, sources_for

    status = connection_status(mission)
    out = []
    for slug in sources_for(content_kind):
        entry = capability(slug)
        out.append({
            "slug": slug,
            "source_ref": entry["source_ref"],
            "evidence_tier": entry["evidence_tier"],
            "in_inventory": entry["in_inventory"],
            "generic": entry["generic"],
            "connection_status": status.get(slug, "undeclared"),
            "daily_quota": [dict(row) for row in entry["quotas"]],
            "note": entry["note"],
        })
    return out


def assess_gaps(
    policy: Mapping[str, Any],
    *,
    long_term: Mapping[str, Any] | None = None,
    short_term: Mapping[str, Any] | None = None,
    mission: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """The checklist, answered against what this framework covers.

    Deterministic on purpose.  The blueprint makes this list the input to the
    S line -- "TAM / supply / high-frequency data gaps listed honestly, as the
    basis for connecting Guidepoint and IR" -- and a list a model wrote would
    be a list of the gaps the model found interesting on the day it ran.
    """

    stated = stated_driver_refs(long_term, short_term)
    rows = []
    for item in validate_policy(policy)["gap_checklist"]:
        named = list(item["driver_refs"])
        covered = sorted(set(named) & stated)
        if not named or not covered:
            status = "open"
        elif len(covered) == len(set(named)):
            status = "covered"
        else:
            status = "partially_covered"
        rows.append({
            "gap_ref": item["gap_ref"],
            "label": item["label"],
            "what_is_missing": item["what_is_missing"],
            "status": status,
            "driver_refs": named,
            "covered_driver_refs": covered,
            "content_kind": item["content_kind"],
            "candidate_sources": candidate_sources(item["content_kind"], mission=mission),
            "cost_note": item["cost_note"],
            "blocks_links": list(item["blocks_links"]),
        })
    return rows


def open_gaps(gaps: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    return [dict(gap) for gap in gaps if gap.get("status") != "covered"]


def _gap(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise IndustryFrameworkValidationError(f"{name} must be an object")
    wire = dict(value)
    fields = {"gap_ref", "label", "what_is_missing", "status", "driver_refs",
              "covered_driver_refs", "content_kind", "candidate_sources",
              "cost_note", "blocks_links"}
    if set(wire) != fields:
        raise IndustryFrameworkValidationError(
            f"{name} has an invalid closed shape; "
            f"missing={sorted(fields - set(wire))}, "
            f"unknown={sorted(set(wire) - fields)}")
    for field in ("gap_ref", "label", "content_kind"):
        wire[field] = _text(wire[field], f"{name}.{field}", maximum=200)
    for field in ("what_is_missing", "cost_note"):
        wire[field] = _text(wire[field], f"{name}.{field}", maximum=MAX_GAP_CHARS)
    wire["status"] = _one_of(wire["status"], GAP_STATUSES, f"{name}.status")
    for field in ("driver_refs", "covered_driver_refs"):
        rows = wire[field]
        if not isinstance(rows, list):
            raise IndustryFrameworkValidationError(f"{name}.{field} must be an array")
        wire[field] = [_text(row, f"{name}.{field}[]") for row in rows]
    if not set(wire["covered_driver_refs"]) <= set(wire["driver_refs"]):
        raise IndustryFrameworkValidationError(
            f"{name} reports a covered driver it does not name")
    links = wire["blocks_links"]
    if not isinstance(links, list) or any(
        isinstance(row, bool) or not isinstance(row, int) or row < 0 for row in links
    ):
        raise IndustryFrameworkValidationError(
            f"{name}.blocks_links must be chain link indexes")
    sources = wire["candidate_sources"]
    if not isinstance(sources, list):
        raise IndustryFrameworkValidationError(
            f"{name}.candidate_sources must be an array")
    checked = []
    for index, row in enumerate(sources):
        if not isinstance(row, Mapping) or set(row) != {
            "slug", "source_ref", "evidence_tier", "in_inventory", "generic",
            "connection_status", "daily_quota", "note",
        }:
            raise IndustryFrameworkValidationError(
                f"{name}.candidate_sources[{index}] has an invalid closed shape")
        checked.append({
            "slug": _text(row["slug"], "slug", maximum=120),
            "source_ref": _text(row["source_ref"], "source_ref", maximum=200),
            "evidence_tier": _text(row["evidence_tier"], "evidence_tier", maximum=60),
            "in_inventory": bool(row["in_inventory"]),
            "generic": bool(row["generic"]),
            "connection_status": _text(row["connection_status"], "connection_status",
                                       maximum=60),
            "daily_quota": [dict(entry) for entry in row["daily_quota"] or []],
            "note": str(row["note"] or ""),
        })
    wire["candidate_sources"] = checked
    return wire


# ---------------------------------------------------------------------------
# the debate summary: a projection of P12c's industry chain
# ---------------------------------------------------------------------------


def summarise_debates(version: Mapping[str, Any] | None) -> dict[str, Any]:
    """P12c's industry DebateMap, as this framework carries it.

    A projection and nothing more.  The debate map is its own authority with
    its own version chain; restating its positions here would create a second
    place where "what the argument is" is written, and the two would disagree
    within a week.  What the framework keeps is the shape of the argument --
    the question, where it stands, which way it is moving -- bound to the
    version it was read from.
    """

    if not version:
        return {"status": "unavailable", "reason": "no_debate_map",
                "map_ref": None, "version_ref": None, "version": None, "debates": []}
    rows = []
    for debate in version.get("debates") or []:
        shift = debate.get("last_shift_reason") or None
        rows.append({
            "debate_ref": str(debate.get("debate_ref") or ""),
            "question": str(debate.get("question") or ""),
            "status": str(debate.get("status") or ""),
            "driver_refs": [str(ref) for ref in debate.get("driver_refs") or ()],
            "our_side": str((debate.get("our_position") or {}).get("side") or ""),
            "market_lean": str((debate.get("market_position") or {}).get("lean") or ""),
            "bull_ref_count": len((debate.get("bull_position") or {}).get("claim_refs") or ()),
            "bear_ref_count": len((debate.get("bear_position") or {}).get("claim_refs") or ()),
            "last_shift_reason": None if not shift else str(shift.get("reason") or ""),
        })
    return {
        "status": "available",
        "reason": None,
        "map_ref": str(version.get("map_ref") or ""),
        "version_ref": str(version.get("id") or ""),
        "version": version.get("version"),
        "debates": rows,
    }


def _debates_summary(value: Any, name: str = "debates_summary") -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise IndustryFrameworkValidationError(f"{name} must be an object")
    wire = dict(value)
    fields = {"status", "reason", "map_ref", "version_ref", "version", "debates"}
    if set(wire) != fields:
        raise IndustryFrameworkValidationError(f"{name} has an invalid closed shape")
    status = _one_of(wire["status"], ("available", "unavailable"), f"{name}.status")
    if status == "unavailable":
        if wire["debates"]:
            raise IndustryFrameworkValidationError(
                f"{name} is unavailable and may not carry debates")
        return {"status": status,
                "reason": _one_of(wire["reason"], UNAVAILABLE_REASONS, f"{name}.reason"),
                "map_ref": None, "version_ref": None, "version": None, "debates": []}
    if wire["reason"] is not None:
        raise IndustryFrameworkValidationError(f"{name} is available and carries a reason")
    for field in ("map_ref", "version_ref"):
        wire[field] = _text(wire[field], f"{name}.{field}", maximum=512)
    if type(wire["version"]) is not int or wire["version"] < 1:
        raise IndustryFrameworkValidationError(f"{name}.version must be positive")
    rows = wire["debates"]
    if not isinstance(rows, list):
        raise IndustryFrameworkValidationError(f"{name}.debates must be an array")
    checked = []
    for index, row in enumerate(rows):
        if not isinstance(row, Mapping) or set(row) != {
            "debate_ref", "question", "status", "driver_refs", "our_side",
            "market_lean", "bull_ref_count", "bear_ref_count", "last_shift_reason",
        }:
            raise IndustryFrameworkValidationError(
                f"{name}.debates[{index}] has an invalid closed shape")
        checked.append({
            "debate_ref": _text(row["debate_ref"], "debate_ref", maximum=200),
            "question": _text(row["question"], "question"),
            "status": _text(row["status"], "status", maximum=40),
            "driver_refs": [_text(ref, "driver_refs[]") for ref in row["driver_refs"] or ()],
            "our_side": str(row["our_side"] or ""),
            "market_lean": str(row["market_lean"] or ""),
            "bull_ref_count": int(row["bull_ref_count"]),
            "bear_ref_count": int(row["bear_ref_count"]),
            "last_shift_reason": (None if row["last_shift_reason"] is None
                                  else _text(row["last_shift_reason"],
                                             "last_shift_reason")),
        })
    wire["debates"] = checked
    return wire


# ---------------------------------------------------------------------------
# the record
# ---------------------------------------------------------------------------

_RECORD_FIELDS = frozenset({
    "schema_version", "id", "created_at", "framework_ref", "industry_ref",
    "version", "prior_version_ref", "change_reason", "evidence_refs",
    "sections", "industry_characteristics", "long_term_drivers",
    "short_term_drivers", "cross_company_comparison", "debates_summary",
    "gaps", "drafted_at", "bindings", "generator_ref", "actor_ref",
    "body_hash", "content_hash",
})
# What the chain is *about*.  Not the reason it exists, not who asked, not
# when.  ``drafted_at`` is outside the body for the dossier's reason: a redraft
# producing the same prose must not count as a new version because the clock
# moved, or the identical-body duplicate rule would never fire again.
_BODY_EXCLUDED = frozenset({
    "id", "created_at", "version", "prior_version_ref", "change_reason",
    "evidence_refs", "body_hash", "content_hash", "drafted_at",
})
_BINDING_FIELDS = frozenset({
    "constitution_version", "playbook_version", "driver_pack_version",
    "mission_version_ref", "policy_ref", "policy_hash", "causal_chain_hash",
    "rubric_ref", "rubric_hash", "debate_map_version_ref", "comparison_hash",
})


def normalise_ref(value: Any, name: str) -> dict[str, Any]:
    """One citable thing: its kind, its ref, the text shown, its period."""

    if not isinstance(value, Mapping):
        raise IndustryFrameworkValidationError(f"{name} must be an object")
    wire = dict(value)
    if set(wire) - {"kind", "ref", "text", "period"} or not {"kind", "ref"} <= set(wire):
        raise IndustryFrameworkValidationError(f"{name} has an invalid closed shape")
    return {
        "kind": _one_of(wire.get("kind"), REF_KINDS, f"{name}.kind"),
        "ref": _text(wire.get("ref"), f"{name}.ref", maximum=512),
        # The text the model was shown, kept beside the ref: the number
        # discipline checks the prose against it, and a check that has to
        # re-fetch what was shown grades a different document.
        "text": _text(wire.get("text") or "-", f"{name}.text", maximum=1000),
        "period": (None if wire.get("period") is None
                   else _text(wire["period"], f"{name}.period", maximum=120)),
    }


def _sentence(value: Any, name: str, allowed_refs: set[str]) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != {"text", "refs"}:
        raise IndustryFrameworkValidationError(f"{name} must be exactly text and refs")
    text = _text(value["text"], f"{name}.text", maximum=MAX_SENTENCE_CHARS)
    tag = _PROSE_TAG_RE.search(text)
    if tag is not None:
        raise IndustryFrameworkValidationError(
            f"{name}.text writes the citation tag {tag.group(0)!r} into the prose; "
            "tags travel in refs, and a sentence whose subject is a tag becomes a "
            "sentence with no subject once the tag is gone")
    refs = value["refs"]
    if not isinstance(refs, list) or not refs:
        raise IndustryFrameworkValidationError(
            f"{name} cites nothing; every sentence of a framework names its evidence")
    seen: list[str] = []
    for position, ref in enumerate(refs):
        ref = _text(ref, f"{name}.refs[{position}]", maximum=512)
        if ref not in allowed_refs:
            raise IndustryFrameworkValidationError(
                f"{name} cites {ref}, which was not among the material shown")
        if ref not in seen:
            seen.append(ref)
    return {"text": text, "refs": seen}


def _slot(value: Any, name: str, *, expected_id: str, allowed_refs: set[str]) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise IndustryFrameworkValidationError(f"{name} must be an object")
    wire = dict(value)
    if wire.get("slot_id") != expected_id:
        raise IndustryFrameworkValidationError(
            f"{name}.slot_id must be {expected_id!r}; the structure is not the "
            "model's to choose")
    if set(wire) == {"slot_id", "unknown"}:
        return {"slot_id": expected_id,
                "unknown": _text(wire["unknown"], f"{name}.unknown",
                                 maximum=MAX_SENTENCE_CHARS)}
    if set(wire) != {"slot_id", "sentences"}:
        raise IndustryFrameworkValidationError(
            f"{name} must be slot_id with either sentences or unknown")
    rows = wire["sentences"]
    if not isinstance(rows, list) or not rows:
        raise IndustryFrameworkValidationError(
            f"{name}.sentences is empty; say 'unknown' rather than nothing")
    if len(rows) > SLOT_SENTENCE_CAP:
        raise IndustryFrameworkValidationError(
            f"{name} writes {len(rows)} sentences; the cap is {SLOT_SENTENCE_CAP}")
    return {
        "slot_id": expected_id,
        "sentences": [_sentence(row, f"{name}.sentences[{index}]", allowed_refs)
                      for index, row in enumerate(rows)],
    }


def _unit_gaps(value: Any, name: str) -> list[str]:
    rows = value or []
    if not isinstance(rows, list) or len(rows) > MAX_GAPS_PER_UNIT:
        raise IndustryFrameworkValidationError(
            f"{name} must be at most {MAX_GAPS_PER_UNIT} short strings")
    return [_text(row, f"{name}[]", maximum=MAX_GAP_CHARS) for row in rows]


def _drafted_block(
    wire: Mapping[str, Any], name: str, *, structure: Sequence[str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """The slots-and-sources half every drafted unit shares."""

    sources = wire["sources"]
    if not isinstance(sources, list) or not sources:
        raise IndustryFrameworkValidationError(f"{name} is drafted and cites nothing")
    if len(sources) > MAX_SOURCES_PER_UNIT:
        raise IndustryFrameworkValidationError(
            f"{name} shows {len(sources)} sources; the cap is {MAX_SOURCES_PER_UNIT}")
    checked_sources = [normalise_ref(row, f"{name}.sources[{index}]")
                       for index, row in enumerate(sources)]
    refs = {row["ref"] for row in checked_sources}
    if len(refs) != len(checked_sources):
        raise IndustryFrameworkValidationError(f"{name}.sources repeats a ref")
    slots = wire["slots"]
    if not isinstance(slots, list) or len(slots) != len(structure):
        raise IndustryFrameworkValidationError(
            f"{name} fills {len(slots or [])} slots of {len(structure)}; every slot "
            "is answered or explicitly unknown")
    checked = [
        _slot(row, f"{name}.slots[{index}]", expected_id=structure[index],
              allowed_refs=refs)
        for index, row in enumerate(slots)
    ]
    written = sum(len(slot.get("sentences") or ()) for slot in checked)
    if written > UNIT_SENTENCE_CAP:
        raise IndustryFrameworkValidationError(
            f"{name} writes {written} sentences; the cap is {UNIT_SENTENCE_CAP}")
    if not written:
        raise IndustryFrameworkValidationError(
            f"{name} is drafted but every slot is unknown; that unit is "
            "unavailable, not drafted")
    cited = {ref for slot in checked for row in slot.get("sentences") or ()
             for ref in row["refs"]}
    unused = refs - cited
    if unused:
        raise IndustryFrameworkValidationError(
            f"{name} lists sources no sentence cites: {sorted(unused)[:3]}")
    return checked, checked_sources


def validate_section(value: Any, name: str) -> dict[str, Any]:
    """One causal-chain section: two slots, or an honest reason it is empty."""

    if not isinstance(value, Mapping):
        raise IndustryFrameworkValidationError(f"{name} must be an object")
    wire = dict(value)
    fields = {"link_index", "link", "title", "status", "reason", "structure",
              "slots", "sources", "gaps"}
    if set(wire) != fields:
        raise IndustryFrameworkValidationError(
            f"{name} has an invalid closed shape; "
            f"missing={sorted(fields - set(wire))}, "
            f"unknown={sorted(set(wire) - fields)}")
    index = wire["link_index"]
    if isinstance(index, bool) or not isinstance(index, int) or index < 0:
        raise IndustryFrameworkValidationError(f"{name}.link_index must be a position")
    wire["link"] = _text(wire["link"], f"{name}.link")
    wire["title"] = _text(wire["title"], f"{name}.title", maximum=200)
    status = _one_of(wire["status"], ("drafted", "unavailable"), f"{name}.status")
    structure = wire["structure"]
    if not isinstance(structure, list) or any(
        not isinstance(slot, str) or not slot for slot in structure
    ):
        raise IndustryFrameworkValidationError(
            f"{name}.structure must be a list of slot ids")
    if status == "unavailable":
        if wire["slots"] or wire["sources"]:
            raise IndustryFrameworkValidationError(
                f"{name} is unavailable and may not carry a draft")
        return {
            "link_index": index, "link": wire["link"], "title": wire["title"],
            "status": status,
            "reason": _one_of(wire["reason"], UNAVAILABLE_REASONS, f"{name}.reason"),
            "structure": list(structure), "slots": [], "sources": [],
            "gaps": _unit_gaps(wire["gaps"], f"{name}.gaps"),
        }
    if wire["reason"] is not None:
        raise IndustryFrameworkValidationError(f"{name} is drafted and carries a reason")
    slots, sources = _drafted_block(wire, name, structure=structure)
    return {
        "link_index": index, "link": wire["link"], "title": wire["title"],
        "status": status, "reason": None, "structure": list(structure),
        "slots": slots, "sources": sources,
        "gaps": _unit_gaps(wire["gaps"], f"{name}.gaps"),
    }


def validate_characteristics(
    value: Any, name: str = "industry_characteristics"
) -> dict[str, Any]:
    """The five closed words, each with a basis that cites something."""

    if not isinstance(value, Mapping):
        raise IndustryFrameworkValidationError(f"{name} must be an object")
    wire = dict(value)
    fields = {"status", "reason", "values", "structure", "slots", "sources", "gaps"}
    if set(wire) != fields:
        raise IndustryFrameworkValidationError(f"{name} has an invalid closed shape")
    status = _one_of(wire["status"], ("drafted", "unavailable"), f"{name}.status")
    values = wire["values"]
    if not isinstance(values, Mapping) or set(values) != set(CHARACTERISTIC_FIELDS):
        raise IndustryFrameworkValidationError(
            f"{name}.values must answer exactly {sorted(CHARACTERISTIC_FIELDS)}")
    checked_values = {
        field: _one_of(values[field], allowed, f"{name}.values.{field}")
        for field, allowed in CHARACTERISTIC_FIELDS.items()
    }
    if status == "unavailable":
        if wire["slots"] or wire["sources"]:
            raise IndustryFrameworkValidationError(
                f"{name} is unavailable and may not carry a draft")
        if any(word != "insufficient_evidence" for word in checked_values.values()):
            raise IndustryFrameworkValidationError(
                f"{name} is unavailable and still classifies the industry")
        return {
            "status": status,
            "reason": _one_of(wire["reason"], UNAVAILABLE_REASONS, f"{name}.reason"),
            "values": checked_values, "structure": [], "slots": [], "sources": [],
            "gaps": _unit_gaps(wire["gaps"], f"{name}.gaps"),
        }
    if wire["reason"] is not None:
        raise IndustryFrameworkValidationError(f"{name} is drafted and carries a reason")
    structure = list(wire["structure"] or [])
    if structure != list(CHARACTERISTIC_SLOTS):
        raise IndustryFrameworkValidationError(
            f"{name}.structure must be {list(CHARACTERISTIC_SLOTS)}; the structure "
            "is not the model's to choose")
    slots, sources = _drafted_block(wire, name, structure=structure)
    # A word that is not "insufficient_evidence" has to have a basis that says
    # something.  Otherwise the classification is an assertion with a slot
    # beside it saying nobody knows why.
    by_slot = {slot["slot_id"]: slot for slot in slots}
    for field, word in checked_values.items():
        if word == "insufficient_evidence":
            continue
        if not (by_slot.get(f"basis:{field}") or {}).get("sentences"):
            raise IndustryFrameworkValidationError(
                f"{name} answers {field} with {word!r} and gives no basis for it")
    return {
        "status": status, "reason": None, "values": checked_values,
        "structure": structure, "slots": slots, "sources": sources,
        "gaps": _unit_gaps(wire["gaps"], f"{name}.gaps"),
    }


def validate_driver_block(value: Any, name: str) -> dict[str, Any]:
    """One horizon's drivers: the pack's refs, a stance each, a basis each."""

    if not isinstance(value, Mapping):
        raise IndustryFrameworkValidationError(f"{name} must be an object")
    wire = dict(value)
    fields = {"horizon", "status", "reason", "structure", "stances", "slots",
              "sources", "gaps"}
    if set(wire) != fields:
        raise IndustryFrameworkValidationError(
            f"{name} has an invalid closed shape; "
            f"missing={sorted(fields - set(wire))}, "
            f"unknown={sorted(set(wire) - fields)}")
    horizon = _one_of(wire["horizon"], HORIZONS, f"{name}.horizon")
    status = _one_of(wire["status"], ("drafted", "unavailable"), f"{name}.status")
    structure = list(wire["structure"] or [])
    if any(not isinstance(slot, str) or not slot.startswith("driver:")
           for slot in structure):
        raise IndustryFrameworkValidationError(
            f"{name}.structure must be driver slot ids")
    stances = wire["stances"]
    if not isinstance(stances, Mapping):
        raise IndustryFrameworkValidationError(f"{name}.stances must be an object")
    expected_drivers = [slot.split(":", 1)[1] for slot in structure]
    if status == "unavailable":
        if wire["slots"] or wire["sources"] or stances:
            raise IndustryFrameworkValidationError(
                f"{name} is unavailable and may not carry a draft")
        return {
            "horizon": horizon, "status": status,
            "reason": _one_of(wire["reason"], UNAVAILABLE_REASONS, f"{name}.reason"),
            "structure": structure, "stances": {}, "slots": [], "sources": [],
            "gaps": _unit_gaps(wire["gaps"], f"{name}.gaps"),
        }
    if wire["reason"] is not None:
        raise IndustryFrameworkValidationError(f"{name} is drafted and carries a reason")
    if not structure:
        raise IndustryFrameworkValidationError(
            f"{name} is drafted and names no driver")
    if sorted(stances) != sorted(expected_drivers):
        raise IndustryFrameworkValidationError(
            f"{name}.stances must answer exactly the drivers in its structure")
    checked_stances = {
        driver: _one_of(stances[driver], DRIVER_STANCES, f"{name}.stances.{driver}")
        for driver in sorted(expected_drivers)
    }
    slots, sources = _drafted_block(wire, name, structure=structure)
    by_slot = {slot["slot_id"]: slot for slot in slots}
    for driver, stance in checked_stances.items():
        if stance == "unknown":
            continue
        if not (by_slot.get(f"driver:{driver}") or {}).get("sentences"):
            raise IndustryFrameworkValidationError(
                f"{name} takes the stance {stance!r} on {driver} and gives no basis")
    return {
        "horizon": horizon, "status": status, "reason": None,
        "structure": structure, "stances": checked_stances, "slots": slots,
        "sources": sources, "gaps": _unit_gaps(wire["gaps"], f"{name}.gaps"),
    }


def validate_comparison(value: Any, name: str = "cross_company_comparison") -> dict[str, Any]:
    """The computed table, checked for shape rather than for arithmetic.

    The arithmetic is checked by recomputing it -- ``build_comparison`` is a
    pure function of the ModelInputTables -- and by the ``comparison_hash`` the
    record binds.  What this refuses is a table that has been reshaped: a cell
    naming a metric outside the frozen list, a value where the status says
    there is none, or a ref that is not the one ``cell_ref`` derives.
    """

    if not isinstance(value, Mapping):
        raise IndustryFrameworkValidationError(f"{name} must be an object")
    wire = dict(value)
    fields = {"status", "reason", "metrics", "quarters", "companies", "cells",
              "comparability_notes"}
    if set(wire) != fields:
        raise IndustryFrameworkValidationError(
            f"{name} has an invalid closed shape; "
            f"missing={sorted(fields - set(wire))}, "
            f"unknown={sorted(set(wire) - fields)}")
    status = _one_of(wire["status"], ("computed", "unavailable"), f"{name}.status")
    if list(wire["metrics"] or []) != list(COMPARISON_METRICS):
        raise IndustryFrameworkValidationError(
            f"{name}.metrics is frozen; it must be {list(COMPARISON_METRICS)}")
    notes = wire["comparability_notes"] or []
    if not isinstance(notes, list):
        raise IndustryFrameworkValidationError(
            f"{name}.comparability_notes must be an array")
    wire["comparability_notes"] = [_text(row, f"{name}.comparability_notes[]",
                                         maximum=500) for row in notes]
    if status == "unavailable":
        if wire["cells"] or wire["companies"] or wire["quarters"]:
            raise IndustryFrameworkValidationError(
                f"{name} is unavailable and may not carry a table")
        return {
            "status": status,
            "reason": _one_of(wire["reason"], UNAVAILABLE_REASONS, f"{name}.reason"),
            "metrics": list(COMPARISON_METRICS), "quarters": [], "companies": [],
            "cells": [], "comparability_notes": wire["comparability_notes"],
        }
    if wire["reason"] is not None:
        raise IndustryFrameworkValidationError(f"{name} is computed and carries a reason")
    quarters = [_text(row, f"{name}.quarters[]", maximum=16)
                for row in wire["quarters"] or []]
    if quarters != sorted(set(quarters)):
        raise IndustryFrameworkValidationError(
            f"{name}.quarters must be unique and in order")
    companies = []
    for index, row in enumerate(wire["companies"] or []):
        if not isinstance(row, Mapping) or set(row) != {
            "company_ref", "ticker", "spec_ref", "state_hash", "revenue_concept",
            "cost_of_revenue_concept", "operating_income_concept",
        }:
            raise IndustryFrameworkValidationError(
                f"{name}.companies[{index}] has an invalid closed shape")
        companies.append({
            "company_ref": _text(row["company_ref"], "company_ref", maximum=512),
            "ticker": _text(row["ticker"], "ticker", maximum=32),
            "spec_ref": None if row["spec_ref"] is None else str(row["spec_ref"]),
            "state_hash": None if row["state_hash"] is None else str(row["state_hash"]),
            "revenue_concept": (None if row["revenue_concept"] is None
                                else str(row["revenue_concept"])),
            "cost_of_revenue_concept": (None if row["cost_of_revenue_concept"] is None
                                        else str(row["cost_of_revenue_concept"])),
            "operating_income_concept": (None if row["operating_income_concept"] is None
                                         else str(row["operating_income_concept"])),
        })
    if not companies:
        raise IndustryFrameworkValidationError(
            f"{name} is computed and names no company")
    known = {row["company_ref"] for row in companies}
    cells = []
    seen_refs: set[str] = set()
    for index, row in enumerate(wire["cells"] or []):
        cell_fields = {"ref", "company_ref", "ticker", "metric", "quarter",
                       "period_end", "status", "value", "display", "unit",
                       "basis", "reason", "inputs", "source_accessions"}
        if not isinstance(row, Mapping) or set(row) != cell_fields:
            raise IndustryFrameworkValidationError(
                f"{name}.cells[{index}] has an invalid closed shape")
        company_ref = _text(row["company_ref"], "company_ref", maximum=512)
        if company_ref not in known:
            raise IndustryFrameworkValidationError(
                f"{name}.cells[{index}] names a company the table does not carry")
        metric = _one_of(row["metric"], COMPARISON_METRICS, f"{name}.cells[{index}].metric")
        quarter = _text(row["quarter"], "quarter", maximum=16)
        if quarter not in quarters:
            raise IndustryFrameworkValidationError(
                f"{name}.cells[{index}] names a quarter that is not a column")
        expected_ref = cell_ref(company_ref, metric, quarter)
        if row["ref"] != expected_ref:
            raise IndustryFrameworkValidationError(
                f"{name}.cells[{index}].ref must be {expected_ref!r}; a cell's name "
                "is derived, not chosen")
        if expected_ref in seen_refs:
            raise IndustryFrameworkValidationError(
                f"{name} carries {expected_ref} twice")
        seen_refs.add(expected_ref)
        cell_status = _one_of(row["status"], ("computed", "unavailable"),
                              f"{name}.cells[{index}].status")
        if cell_status == "computed":
            if row["value"] is None or row["display"] is None:
                raise IndustryFrameworkValidationError(
                    f"{name}.cells[{index}] is computed and carries no value")
            if row["reason"] is not None:
                raise IndustryFrameworkValidationError(
                    f"{name}.cells[{index}] is computed and carries a reason")
        else:
            if row["value"] is not None or row["display"] is not None:
                raise IndustryFrameworkValidationError(
                    f"{name}.cells[{index}] is unavailable and carries a value")
            if not str(row["reason"] or "").strip():
                raise IndustryFrameworkValidationError(
                    f"{name}.cells[{index}] is unavailable and does not say why")
        if row["unit"] != METRIC_UNITS[metric]:
            raise IndustryFrameworkValidationError(
                f"{name}.cells[{index}] carries the wrong unit for {metric}")
        cells.append({
            "ref": expected_ref, "company_ref": company_ref,
            "ticker": _text(row["ticker"], "ticker", maximum=32),
            "metric": metric, "quarter": quarter,
            "period_end": (None if row["period_end"] is None
                           else _text(row["period_end"], "period_end", maximum=32)),
            "status": cell_status,
            "value": None if row["value"] is None else str(row["value"]),
            "display": None if row["display"] is None else str(row["display"]),
            "unit": str(row["unit"]),
            "basis": None if row["basis"] is None else str(row["basis"]),
            "reason": None if row["reason"] is None else str(row["reason"]),
            "inputs": [dict(item) for item in row["inputs"] or []],
            "source_accessions": [str(item) for item in row["source_accessions"] or []],
        })
    return {
        "status": status, "reason": None, "metrics": list(COMPARISON_METRICS),
        "quarters": quarters, "companies": companies, "cells": cells,
        "comparability_notes": wire["comparability_notes"],
    }


def _drafted_at(value: Any, name: str = "drafted_at") -> dict[str, str]:
    """When each unit was last written, by unit.

    Carried forward unchanged for the units a version did not touch, which is
    what makes "has anything arrived since *this part* was written" answerable
    per part rather than per document.
    """

    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise IndustryFrameworkValidationError(f"{name} must be an object")
    out: dict[str, str] = {}
    for unit, when in value.items():
        if unit not in STATIC_UNITS and _CHAIN_UNIT_RE.fullmatch(str(unit)) is None:
            raise IndustryFrameworkValidationError(
                f"{name} names {unit!r}, which is not a framework unit")
        out[str(unit)] = _text(when, f"{name}[{unit}]", maximum=64)
    return dict(sorted(out.items()))


def _bindings(value: Any, name: str = "bindings") -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != _BINDING_FIELDS:
        raise IndustryFrameworkValidationError(
            f"{name} has an invalid closed shape; "
            f"missing={sorted(_BINDING_FIELDS - set(value or {}))}, "
            f"unknown={sorted(set(value or {}) - _BINDING_FIELDS)}")
    wire = dict(value)
    out: dict[str, Any] = {}
    for field in ("constitution_version", "playbook_version", "driver_pack_version"):
        item = wire[field]
        if not isinstance(item, Mapping) or set(item) != {"ref", "hash"}:
            raise IndustryFrameworkValidationError(f"{name}.{field} must be ref and hash")
        out[field] = {
            "ref": _text(item["ref"], f"{name}.{field}.ref", maximum=512),
            "hash": _sha256(item["hash"], f"{name}.{field}.hash"),
        }
    out["mission_version_ref"] = (
        None if wire["mission_version_ref"] is None
        else _text(wire["mission_version_ref"], f"{name}.mission_version_ref",
                   maximum=512))
    out["debate_map_version_ref"] = (
        None if wire["debate_map_version_ref"] is None
        else _text(wire["debate_map_version_ref"], f"{name}.debate_map_version_ref",
                   maximum=512))
    out["policy_ref"] = _text(wire["policy_ref"], f"{name}.policy_ref")
    out["policy_hash"] = _sha256(wire["policy_hash"], f"{name}.policy_hash")
    out["causal_chain_hash"] = _sha256(wire["causal_chain_hash"],
                                       f"{name}.causal_chain_hash")
    out["rubric_ref"] = _text(wire["rubric_ref"], f"{name}.rubric_ref")
    out["rubric_hash"] = _sha256(wire["rubric_hash"], f"{name}.rubric_hash")
    out["comparison_hash"] = _sha256(wire["comparison_hash"], f"{name}.comparison_hash")
    return out


def validate_framework_version(value: Mapping[str, Any]) -> dict[str, Any]:
    """One IndustryFrameworkVersion, closed and self-consistent."""

    if not isinstance(value, Mapping):
        raise IndustryFrameworkValidationError("framework version must be an object")
    wire = dict(value)
    if set(wire) != _RECORD_FIELDS or wire.get("schema_version") != SCHEMA_VERSION:
        raise IndustryFrameworkValidationError(
            "industry framework version has an invalid closed shape; "
            f"missing={sorted(_RECORD_FIELDS - set(wire))}, "
            f"unknown={sorted(set(wire) - _RECORD_FIELDS)}")
    for field in ("id", "created_at", "framework_ref", "industry_ref", "generator_ref"):
        wire[field] = _text(wire[field], field, maximum=512)
    if wire["framework_ref"] != framework_ref_for(wire["industry_ref"]):
        raise IndustryFrameworkConflict("framework_ref is not derived from the industry")
    wire["actor_ref"] = _principal(wire["actor_ref"])
    wire["change_reason"] = _one_of(wire["change_reason"], CHANGE_REASONS, "change_reason")
    if type(wire["version"]) is not int or wire["version"] < 1:
        raise IndustryFrameworkValidationError("version must be a positive integer")
    if wire["prior_version_ref"] is not None:
        wire["prior_version_ref"] = _text(wire["prior_version_ref"],
                                          "prior_version_ref", maximum=512)
    evidence = wire["evidence_refs"]
    if not isinstance(evidence, list) or not evidence:
        raise IndustryFrameworkValidationError(
            "a version must name the evidence that occasioned it")
    wire["evidence_refs"] = [normalise_ref(row, f"evidence_refs[{index}]")
                             for index, row in enumerate(evidence)]

    sections = wire["sections"]
    if not isinstance(sections, list) or not sections:
        raise IndustryFrameworkValidationError(
            "a framework has one section per causal-chain link")
    checked = []
    for index, section in enumerate(sections):
        row = validate_section(section, f"sections[{index}]")
        if row["link_index"] != index:
            raise IndustryFrameworkValidationError(
                f"sections[{index}] claims link {row['link_index']}; the sections "
                "are the chain, in the chain's order")
        checked.append(row)
    wire["sections"] = checked
    wire["industry_characteristics"] = validate_characteristics(
        wire["industry_characteristics"])
    for field, horizon in (("long_term_drivers", "long_term"),
                           ("short_term_drivers", "short_term")):
        block = validate_driver_block(wire[field], field)
        if block["horizon"] != horizon:
            raise IndustryFrameworkValidationError(
                f"{field} must carry the {horizon} horizon")
        wire[field] = block
    wire["cross_company_comparison"] = validate_comparison(
        wire["cross_company_comparison"])
    wire["debates_summary"] = _debates_summary(wire["debates_summary"])
    rows = wire["gaps"]
    if not isinstance(rows, list) or not rows:
        raise IndustryFrameworkValidationError(
            "the gap list is this deliverable's point; an empty one is a claim "
            "that nothing is missing")
    wire["gaps"] = [_gap(row, f"gaps[{index}]") for index, row in enumerate(rows)]
    if len({row["gap_ref"] for row in wire["gaps"]}) != len(wire["gaps"]):
        raise IndustryFrameworkValidationError("gaps repeat a gap_ref")
    wire["drafted_at"] = _drafted_at(wire["drafted_at"])
    wire["bindings"] = _bindings(wire["bindings"])
    if wire["bindings"]["comparison_hash"] != comparison_hash(
        wire["cross_company_comparison"]
    ):
        raise IndustryFrameworkConflict(
            "the bound comparison_hash is not this table's hash")
    wire["body_hash"] = _sha256(wire["body_hash"], "body_hash")
    wire["content_hash"] = _sha256(wire["content_hash"], "content_hash")
    if body_hash(wire) != wire["body_hash"]:
        raise IndustryFrameworkConflict("industry framework body_hash is not its body")
    base = {key: item for key, item in wire.items() if key != "content_hash"}
    if content_hash(base) != wire["content_hash"]:
        raise IndustryFrameworkConflict("industry framework content_hash drifted")
    return wire


def body_hash(record: Mapping[str, Any]) -> str:
    """What this version says about the industry, and nothing else."""

    return content_hash({
        key: value for key, value in record.items()
        if key in _RECORD_FIELDS and key not in _BODY_EXCLUDED
    })


def evidence_scope(record: Mapping[str, Any]) -> list[str]:
    """Every ref this version rests on, sorted.  ADR-0008 reads this.

    The comparison table's filed accessions are in here, and that is the one
    place this differs from the dossier: a newly filed quarter moves the table
    and no prose, and a system that called that a duplicate would stop
    versioning the industry the moment the drafting stalled.
    """

    refs: set[str] = set()
    for section in record.get("sections") or []:
        refs.update(row["ref"] for row in section.get("sources") or [])
    for key in ("industry_characteristics", "long_term_drivers", "short_term_drivers"):
        for row in (record.get(key) or {}).get("sources") or []:
            refs.add(row["ref"])
    refs.update(comparison_accessions(record.get("cross_company_comparison") or {}))
    return sorted(refs)


def new_refs(record: Mapping[str, Any], prior: Mapping[str, Any] | None) -> list[str]:
    """The refs this version cites that the current one did not."""

    if prior is None:
        return evidence_scope(record)
    return sorted(set(evidence_scope(record)) - set(evidence_scope(prior)))


# ---------------------------------------------------------------------------
# readable projections
# ---------------------------------------------------------------------------


def unit_body(block: Mapping[str, Any]) -> str:
    """The prose a reader sees, assembled from the sentence rows.

    Assembled here rather than written by the model: the tags never enter the
    text, so nothing is left behind when they are stripped, because they were
    never in it.
    """

    body = ""
    for slot in block.get("slots") or []:
        for row in slot.get("sentences") or ():
            if body and body[-1] not in _CJK_TERMINATORS:
                body += " "
            body += row["text"]
    return body


def _artefact_parts(record: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Every drafted part, as one list of scoreable sections."""

    def one(title: str, block: Mapping[str, Any]) -> dict[str, Any]:
        sources = block.get("sources") or []
        return {
            "title": title,
            "body": unit_body(block),
            "claim_refs": [row["ref"] for row in sources if row["kind"] == "claim"],
            "numbers": [
                {"text": row["text"],
                 "claim_version_ref": row["ref"] if row["kind"] == "claim" else "",
                 "period": row.get("period")}
                for row in sources
            ],
            "gaps": block.get("gaps") or [],
        }

    out = [one(f"causal_chain:{section['link_index']}", section)
           for section in record.get("sections") or []]
    for title in ("industry_characteristics", "long_term_drivers", "short_term_drivers"):
        block = record.get(title)
        if block:
            out.append(one(title, block))
    return out


def framework_artefact(
    record: Mapping[str, Any], *, prior: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    """The framework in Q1's scoreable artefact shape.

    Built here rather than through ``research_quality_score.artefact`` because
    that function's ``ARTEFACT_KINDS`` is a closed tuple in a module this branch
    does not own, and ``industry_framework`` is not in it yet.  The shape is the
    same shape -- a test asserts the keys match what ``artefact()`` produces --
    so ``run_deterministic`` reads it exactly as it reads a dossier.  Adding the
    kind and an ``artefact_from_industry_framework`` adapter is an integration
    item; see the report.

    ``claim_version_ref`` carries only claim-kind refs, because Q1's
    ``claim_refs_resolve`` resolves refs against ``claim_versions``: a
    comparison-cell ref there would be reported as an unresolvable Claim.
    """

    parts = _artefact_parts(record)
    return {
        "artefact_kind": "industry_framework",
        "ref": str(record["id"]),
        "hash": str(record["content_hash"]),
        "title": str(record.get("framework_ref") or ""),
        "subject_ref": record.get("industry_ref"),
        "sections": parts,
        "gaps": [gap["what_is_missing"] for gap in record.get("gaps") or []
                 if gap.get("status") != "covered"],
        "expected_sections": [part["title"] for part in parts],
        "shown_claims": [],
        "cited_tags": [],
        "confidence": None,
        "question": None,
        "prior": None if prior is None else {"sections": _artefact_parts(prior)},
    }


def deliverable_sections(record: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The framework as ``mission_deliverable`` sections.

    ``industry_framework`` has been a ``DELIVERABLE_KIND`` since Phase 9 with
    nothing behind it.  This is the projection that lets the published version
    also be a document a person reads in the Cockpit, without the record and
    the document being two separately drafted things.

    Only claim-kind refs travel in ``claim_refs`` and ``numbers``, because the
    deliverable authority resolves them against live claim versions; the
    comparison table's figures are carried in a section of their own whose
    numbers list is empty and whose body is the rendered table, so that the
    figure discipline reads the table's own rows rather than reporting every
    cell as an unsourced number.
    """

    sections = []
    for part in _artefact_parts(record):
        if not part["body"]:
            continue
        sections.append({
            "title": part["title"],
            "body": part["body"],
            "claim_refs": part["claim_refs"],
            "numbers": [row for row in part["numbers"] if row["claim_version_ref"]],
            "gaps": part["gaps"],
        })
    comparison = record.get("cross_company_comparison") or {}
    sections.append({
        "title": "cross_company_comparison",
        "body": render_comparison(comparison),
        "claim_refs": [],
        "numbers": [],
        "gaps": list(comparison.get("comparability_notes") or []),
    })
    sections.append({
        "title": "gaps",
        "body": "",
        "claim_refs": [],
        "numbers": [],
        "gaps": [f"{gap['gap_ref']}: {gap['what_is_missing']}"
                 for gap in record.get("gaps") or [] if gap["status"] != "covered"],
    })
    return sections


# ---------------------------------------------------------------------------
# the Constitution's output_rubric, as a pre-publish structural check
# ---------------------------------------------------------------------------

# Words that turn a file into a call.  Same list as the dossier's, and for the
# same reason: a framework says what is true about an industry, and what to do
# about it is a Thesis, admitted by a person (ADR-0001).
_CONCLUSION_PATTERNS = (
    "买入", "卖出", "增持", "减持", "目标价", "低估", "高估",
    "should buy", "should sell", "price target", "undervalued", "overvalued",
)


def output_rubric_findings(
    record: Mapping[str, Any],
    *,
    constitution: Mapping[str, Any],
    policy: Mapping[str, Any],
    prior: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Run every ``method.output_rubric`` criterion the policy bound to a check.

    A criterion the policy neither binds nor declares inapplicable is itself a
    finding: the Constitution is a published standard, and a consumer that
    reads the criteria it happens to understand is a consumer of its own list.
    """

    from .mission_deliverable import unsourced_numbers

    bindings = {item["criterion_hash"]: item
                for item in validate_policy(policy)["output_rubric_bindings"]}
    criteria = list(((constitution.get("method") or {}).get("output_rubric") or {})
                    .get("criteria") or [])
    findings: list[dict[str, Any]] = []
    parts = _artefact_parts(record)
    for index, criterion in enumerate(criteria):
        digest = content_hash(str(criterion))
        binding = bindings.get(digest)
        if binding is None:
            findings.append({
                "code": "unmapped_output_rubric_criterion",
                "criterion_index": index, "criterion": str(criterion)[:200],
                "criterion_hash": digest,
            })
            continue
        for check in binding["checks"]:
            if check == "numbers_trace_to_refs":
                for part in parts:
                    for token in unsourced_numbers(part["body"], part["numbers"]):
                        findings.append({
                            "code": "number_without_source", "criterion_index": index,
                            "section": part["title"], "figure": token,
                        })
            elif check == "not_a_restatement":
                if prior is not None and not new_refs(record, prior):
                    findings.append({"code": "no_new_evidence",
                                     "criterion_index": index})
            elif check == "no_investment_conclusion":
                for part in parts:
                    for pattern in _CONCLUSION_PATTERNS:
                        if pattern in part["body"]:
                            findings.append({
                                "code": "investment_conclusion",
                                "criterion_index": index,
                                "section": part["title"], "phrase": pattern,
                            })
            elif check == "open_gaps_name_a_source":
                for gap in record.get("gaps") or []:
                    if gap["status"] == "covered":
                        continue
                    if not gap["candidate_sources"] and (
                        "no source" not in gap["cost_note"]
                    ):
                        findings.append({
                            "code": "gap_without_a_source",
                            "criterion_index": index, "gap_ref": gap["gap_ref"],
                        })
    return findings


# ---------------------------------------------------------------------------
# the authority
# ---------------------------------------------------------------------------

SOURCE_VERSION_KEY = "computed_from_version_ref"
_UNSET = object()


class IndustryFrameworkAuthority:
    """Append-only IndustryFrameworkVersions, one chain per industry.

    It publishes what it is handed and refuses what has not learned anything.
    It never decides that a framework should be rewritten: ADR-0008's entry
    points take a ``change_reason`` and evidence refs from their caller, and
    the judgement about whether the file should move lives one layer up.
    """

    def __init__(self, store: DaltonStore):
        self.store = store
        self.connection = store.connection
        self._authorized = False
        self.connection.create_function(
            "dalton_industry_framework_authorized", 0, lambda: int(self._authorized)
        )
        self.connection.executescript(_SCHEMA_PATH.read_text(encoding="utf-8"))

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Cursor]:
        if self._authorized:
            raise RuntimeError("IndustryFrameworkAuthority operation cannot be nested")
        self._authorized = True
        try:
            with self.store._transaction() as cur:
                yield cur
        finally:
            self._authorized = False

    def publish(self, body: Mapping[str, Any]) -> dict[str, Any]:
        """Store one framework version, or say why it is not one.

        Two ways to be a ``duplicate`` and they are different findings: the
        body is byte-for-byte what the chain already holds, or the body moved
        but every ref it rests on was already cited.  The second is the one
        ADR-0008 was written for.
        """

        body = dict(body)
        source = body.pop(SOURCE_VERSION_KEY, _UNSET)
        for field in _BODY_EXCLUDED - {"change_reason", "evidence_refs", "drafted_at"}:
            body.pop(field, None)
        industry_ref = _text(body.get("industry_ref"), "industry_ref", maximum=512)
        framework_ref = framework_ref_for(industry_ref)
        body["framework_ref"] = framework_ref
        body.setdefault("schema_version", SCHEMA_VERSION)
        body.setdefault("generator_ref", GENERATOR_REF)
        change_reason = _one_of(body.get("change_reason"), CHANGE_REASONS, "change_reason")
        if not body.get("evidence_refs"):
            raise IndustryFrameworkValidationError(
                "a version must name the evidence that occasioned it")
        digest = body_hash(body)
        latest_row = self.connection.execute(
            "SELECT version_id FROM industry_framework_versions WHERE framework_ref=? "
            "ORDER BY version_number DESC LIMIT 1", (framework_ref,),
        ).fetchone()
        latest = None if latest_row is None else self.framework(latest_row["version_id"])
        if latest is not None and latest["body_hash"] == digest:
            return {**latest, "status": "duplicate", "duplicate_reason": "identical_body"}
        if latest is not None and not new_refs(body, latest):
            return {
                **latest, "status": "duplicate",
                "duplicate_reason": "no_new_evidence",
                "detail": ("this draft cites nothing the current version does not, "
                           "and the comparison table has not moved either; ADR-0008 "
                           "refuses it rather than storing a rewrite"),
            }
        head = None if latest is None else str(latest["id"])
        if source is not _UNSET and source != head:
            raise IndustryFrameworkConflict(
                f"this framework is now at {head or 'no version'}, and this body was "
                f"computed from {source or 'no version'}")
        version = 1 if latest is None else int(latest["version"]) + 1
        record = {
            **body,
            "id": f"industry-framework-version:{industry_slug(industry_ref)}:{version}",
            "created_at": _now(),
            "version": version,
            "prior_version_ref": head,
            "change_reason": change_reason,
            "body_hash": digest,
        }
        record["drafted_at"] = _drafted_at(record.get("drafted_at"))
        record["content_hash"] = content_hash(record)
        wire = validate_framework_version(record)
        scope_hash = content_hash(evidence_scope(wire))
        table_hash = comparison_hash(wire["cross_company_comparison"])
        with self._transaction() as cur:
            if cur.execute(
                "SELECT 1 FROM industry_framework_versions WHERE version_id=?",
                (wire["id"],),
            ).fetchone():
                raise IndustryFrameworkConflict(
                    "industry framework version id already exists")
            cur.execute(
                "INSERT INTO industry_framework_versions"
                "(version_id,framework_ref,version_number,prior_version_id,industry_ref,"
                "change_reason,body_hash,evidence_scope_hash,comparison_hash,record_json,"
                "content_hash,actor_ref,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    wire["id"], framework_ref, version, head, industry_ref,
                    change_reason, digest, scope_hash, table_hash,
                    canonical_json(wire), wire["content_hash"], wire["actor_ref"],
                    wire["created_at"],
                ),
            )
        stored = self.framework(wire["id"])
        if stored["content_hash"] != wire["content_hash"]:
            raise IndustryFrameworkConflict(
                "industry framework did not read back as written")
        return {**stored, "status": "fresh"}

    def revise(self, body: Mapping[str, Any], **kwargs: Any) -> dict[str, Any]:
        """ADR-0008's revision entry point.  Same rules; a named door."""

        return self.publish({**dict(body), **kwargs})

    def framework(self, version_ref: str) -> dict[str, Any]:
        version_ref = _text(version_ref, "version_ref", maximum=512)
        row = self.connection.execute(
            "SELECT * FROM industry_framework_versions WHERE version_id=?",
            (version_ref,),
        ).fetchone()
        if row is None:
            raise IndustryFrameworkNotFound("industry framework version was not found")
        wire = validate_framework_version(json.loads(row["record_json"]))
        if (
            wire["id"] != row["version_id"]
            or wire["framework_ref"] != row["framework_ref"]
            or wire["version"] != row["version_number"]
            or wire["prior_version_ref"] != row["prior_version_id"]
            or wire["industry_ref"] != row["industry_ref"]
            or wire["change_reason"] != row["change_reason"]
            or wire["body_hash"] != row["body_hash"]
            or wire["content_hash"] != row["content_hash"]
            or content_hash(evidence_scope(wire)) != row["evidence_scope_hash"]
            or comparison_hash(wire["cross_company_comparison"]) != row["comparison_hash"]
        ):
            raise IndustryFrameworkConflict("industry framework authority drifted")
        return wire

    def latest(self, industry_ref: str) -> dict[str, Any] | None:
        industry_ref = _text(industry_ref, "industry_ref", maximum=512)
        row = self.connection.execute(
            "SELECT version_id FROM industry_framework_versions WHERE industry_ref=? "
            "ORDER BY version_number DESC LIMIT 1", (industry_ref,),
        ).fetchone()
        return None if row is None else self.framework(row["version_id"])

    def versions(self, industry_ref: str) -> list[dict[str, Any]]:
        industry_ref = _text(industry_ref, "industry_ref", maximum=512)
        rows = self.connection.execute(
            "SELECT version_id FROM industry_framework_versions WHERE industry_ref=? "
            "ORDER BY version_number", (industry_ref,),
        ).fetchall()
        return [self.framework(row["version_id"]) for row in rows]

    def industries(self) -> list[str]:
        return [str(row["industry_ref"]) for row in self.connection.execute(
            "SELECT DISTINCT industry_ref FROM industry_framework_versions "
            "ORDER BY industry_ref").fetchall()]

    def replay_link(self, industry_ref: str, link_index: int) -> list[dict[str, Any]]:
        """What every version said about one link of the chain, and why.

        ADR-0008's replay requirement: "what did we know and what did we
        conclude as of version N" has to be answerable from the chain alone.
        """

        out = []
        for record in self.versions(industry_ref):
            section = next((item for item in record["sections"]
                            if item["link_index"] == link_index), None)
            if section is None:
                continue
            out.append({
                "version_ref": record["id"], "version": record["version"],
                "created_at": record["created_at"],
                "change_reason": record["change_reason"],
                "title": section["title"], "status": section["status"],
                "reason": section["reason"], "body": unit_body(section),
                "refs": [row["ref"] for row in section.get("sources") or []],
            })
        return out

    def gap_report(self, industry_ref: str) -> dict[str, Any]:
        """The current version's open gaps, as the S line reads them."""

        record = self.latest(industry_ref)
        if record is None:
            return {"projection_kind": "industry_framework_gap_report",
                    "industry_ref": industry_ref, "version_ref": None, "gaps": []}
        return {
            "projection_kind": "industry_framework_gap_report",
            "industry_ref": industry_ref,
            "version_ref": record["id"],
            "version": record["version"],
            "gaps": open_gaps(record["gaps"]),
        }


_check_stance_vocabulary()


__all__ = [
    "CAPITAL_INTENSITY",
    "CHANGE_REASONS",
    "CHARACTERISTIC_FIELDS",
    "CHARACTERISTIC_PROMPTS",
    "CHARACTERISTIC_SLOTS",
    "COMPARISON_METRICS",
    "CONCENTRATION",
    "COST_EXCLUDING_DANDA",
    "COST_OF_REVENUE_CONCEPTS",
    "CYCLICALITY",
    "DEFAULT_COMPARISON_QUARTERS",
    "DELIVERABLE_KIND",
    "DRIVER_STANCES",
    "GAP_STATUSES",
    "GENERATOR_REF",
    "HORIZONS",
    "METRIC_UNITS",
    "OPERATING_INCOME_CONCEPTS",
    "OUTPUT_RUBRIC_CHECKS",
    "REF_KINDS",
    "REVENUE_CONCEPTS",
    "REVENUE_VISIBILITY",
    "SCHEMA_VERSION",
    "SECTION_SLOTS",
    "SECTION_SLOT_PROMPTS",
    "SLOT_SENTENCE_CAP",
    "SOURCE_VERSION_KEY",
    "STATIC_UNITS",
    "UNAVAILABLE_REASONS",
    "UNIT_SENTENCE_CAP",
    "WRITE_SCOPE",
    "FrameworkStructureUnmapped",
    "IndustryFrameworkAuthority",
    "IndustryFrameworkConflict",
    "IndustryFrameworkError",
    "IndustryFrameworkNotFound",
    "IndustryFrameworkValidationError",
    "assess_gaps",
    "body_hash",
    "build_comparison",
    "calendar_quarter",
    "candidate_sources",
    "causal_chain_hash",
    "cell_ref",
    "chain_titles",
    "comparability_notes",
    "comparison_accessions",
    "comparison_hash",
    "comparison_material",
    "deliverable_sections",
    "driver_horizons",
    "drivers_for_horizon",
    "evidence_scope",
    "framework_artefact",
    "framework_ref_for",
    "industry_slug",
    "load_policy",
    "new_refs",
    "normalise_ref",
    "open_gaps",
    "output_rubric_findings",
    "policy_hash",
    "quarter_minus_year",
    "render_comparison",
    "stated_driver_refs",
    "summarise_debates",
    "unit_body",
    "unit_slots",
    "units_for",
    "validate_characteristics",
    "validate_comparison",
    "validate_driver_block",
    "validate_framework_version",
    "validate_policy",
    "validate_section",
]
