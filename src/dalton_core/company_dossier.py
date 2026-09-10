"""P12a: the company dossier -- ten sections, one chain, nothing self-published.

What this layer is for is written in the blueprint in one line: an analyst who
has read a company carries a *file* on it, not a pile of quotations.  The
Ledger holds 2,170 atomic Claims and nobody can read a company out of them.

Four decisions make this a file rather than a longer Initial Screen.

**The sections are the aspect vocabulary, verbatim.**  P12b tagged every Claim
with one of twelve closed words, ten of which are these sections, precisely so
that section *k* of a company's dossier is "the canonical Claims whose aspect
is *k*" and no second mapping exists to drift.

**Two sections do not choose their own shape.**  ``demand_drivers`` and
``supply_and_cost`` take their slots from the Constitution's
``method.causal_chain`` (the plan's C3): the industry's causal chain is a
human-published, hash-bound statement of how demand reaches earnings, and the
model's job is to say what the evidence shows about each link -- or that it
shows nothing.  A model that invents its own structure has quietly replaced
the methodology with its own, and the version chain would record the swap as
prose.  When a Constitution's chain is not mapped to sections by the policy,
those two sections are ``unavailable`` rather than free-form: an unmapped
chain is a question for a person, not a licence to improvise.

**Every sentence carries its own refs, and no tag is ever written in prose.**
P13ap's lesson from the published Initial Screens is that a body which uses
citation tags as words cannot survive their removal -- live screens carry
sentences whose subject left with the tag.  So the model returns sentences as
*rows*, ``{text, refs}``, the tags never enter the text, and the body a reader
sees is assembled here.  Per-sentence provenance and clean prose stop being in
tension.

**A version that learned nothing is refused.**  ADR-0008: a new version cites
at least one ref the current one did not, carries a ``change_reason`` from the
closed vocabulary and the evidence refs that occasioned it, and is otherwise a
``duplicate``.  The authority exposes ``publish`` and ``revise`` and never
calls them itself; whether the arrival of a document *means* the file should
change is a judgement, and it lives in the lane and, later, in the event layer.

Sections that cannot be filled are ``unavailable`` with a reason from a closed
list -- never padded.  ``catalyst_calendar`` before C1 lands and
``history_of_price_drivers`` before the price series exists are the two the
roadmap already knows about, and a dossier that wrote something there anyway
would be inventing the part of the file the reader most needs to trust.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Mapping, Sequence
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from types import MappingProxyType
from typing import Any, Iterator

from .claim_aspect_vocabulary import ASPECTS, DEFINITIONS
# One vocabulary for why a version exists, shared with Wave 1C's forecast
# models rather than copied: ADR-0008 is a contract over every output-class
# authority, and two closed lists claiming to be the same list is how a
# contract stops being one.
from .model_forecast_driver import CHANGE_REASONS
from .store import (
    DaltonStore, authorization_flag, authorized_flag, canonical_json, content_hash,
)

SCHEMA_VERSION = "0.1"
_SCHEMA_PATH = Path(__file__).with_name("company_dossier_schema.sql")
GENERATOR_REF = "generator:company-dossier:0.1"
WRITE_SCOPE = "dossier"

# The ten dossier sections are the first ten words of the P12b aspect
# vocabulary, in its order.  ``industry`` and ``other`` are the two that have
# no section: the dossier is per company.
SECTIONS: tuple[str, ...] = ASPECTS[:10]
SECTION_SET = frozenset(SECTIONS)

# The two extra drafted units.  They are not sections -- they have their own
# shapes in the record -- but they are drafted by the same machinery.
CLASSIFICATION_UNIT = "industry_classification"
VARIANT_UNIT = "variant_view"
UNITS: tuple[str, ...] = SECTIONS + (CLASSIFICATION_UNIT, VARIANT_UNIT)

# Deep Insight Gate question 1, as a closed vocabulary.  The Playbook asks
# "是商品周期、资本周期、合同型 compounder、结构成长还是转型公司？分类依据和反例
# 是什么？" -- so the answer is one of five words plus its basis *and* a
# counterexample, and "the evidence does not say" is a sixth answer rather
# than a shrug dressed as a classification.
INDUSTRY_CLASSIFICATIONS: tuple[str, ...] = (
    "commodity_cycle",
    "capital_cycle",
    "contract_compounder",
    "structural_growth",
    "turnaround",
    "insufficient_evidence",
)
CLASSIFICATION_DEFINITIONS: Mapping[str, str] = MappingProxyType({
    "commodity_cycle": (
        "price of an undifferentiated output drives earnings; the cycle is in "
        "the price and the marginal cost curve"
    ),
    "capital_cycle": (
        "industry capacity additions and retirements drive returns; the cycle "
        "is in the supply response to profitability"
    ),
    "contract_compounder": (
        "contracted or annuity-like revenue reinvested at a stable return; the "
        "question is the reinvestment rate, not the cycle"
    ),
    "structural_growth": (
        "a durable shift in end demand outgrows the cycle; the question is how "
        "long the shift lasts and what ends it"
    ),
    "turnaround": (
        "earnings are depressed against their own history and the thesis rests "
        "on a self-help or restructuring path"
    ),
    "insufficient_evidence": (
        "the material shown does not settle the classification; say what is "
        "missing rather than choosing"
    ),
})

# Why a section is empty.  Closed, because "we had nothing to say" and "the
# authority that would answer this does not exist yet" are different facts and
# the second one is a roadmap item, not a gap in the research.
UNAVAILABLE_REASONS: tuple[str, ...] = (
    # no canonical Claim carries this aspect for this company
    "no_canonical_claims",
    # P11a's price series / P11d's events are not on this Core
    "no_market_data",
    # C1's CatalystCalendarVersion is not on this Core
    "no_catalyst_calendar_authority",
    # the Constitution's causal chain is not mapped to sections by the policy
    "causal_chain_unmapped",
    # the draft came back outside its contract and was refused whole
    "refused_by_verification",
    # the section was not drafted on this run and has no prior version to carry
    "not_drafted_this_run",
)

# What a sentence may cite.  Three kinds because a dossier rests on three
# different authorities and a reader has to know which door to open.
REF_KINDS: tuple[str, ...] = ("claim", "figure", "forecast_cell")

# Bounds.  Per slot and per section, both enforced: a six-link causal chain
# with three sentences a link is a section nobody reads.
SLOT_SENTENCE_CAP = 3
SECTION_SENTENCE_CAP = 8
MAX_SENTENCE_CHARS = 400
MAX_SOURCES_PER_SECTION = 40
MAX_GAPS = 6
MAX_GAP_CHARS = 300
MAX_SIGNALS = 6

# The variant view's slots.  ``market_view`` is present only when material
# about the market's view was shown; without it the block records
# ``available: false`` and a reason, which is the honest shape of "nobody has
# told us what the street thinks".
VARIANT_SLOTS: tuple[str, ...] = (
    "our_view", "market_view", "where_market_is_wrong",
    "convergence_pathway", "observable_signals",
)
VARIANT_SLOT_PROMPTS: Mapping[str, str] = MappingProxyType({
    "our_view": "what this file concludes about the company, in the shown evidence's own terms",
    "market_view": "what the market is paying for, from consensus, ratings, sales notes or crowd narrative",
    "where_market_is_wrong": "the exact point of disagreement -- a fact, a timing, a transmission or a multiple",
    "convergence_pathway": "what would have to happen, and by when, for the market to come round",
    "observable_signals": "the observations that would show it happening, or show us wrong",
})
CLASSIFICATION_SLOTS: tuple[str, ...] = ("basis", "counterexample")
CLASSIFICATION_SLOT_PROMPTS: Mapping[str, str] = MappingProxyType({
    "basis": "why this classification and not its nearest neighbour",
    "counterexample": "the strongest fact in the shown material that argues against it",
})

_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
# The citation scaffolding, as it appears when it leaks into prose. The
# drafting prompt forbids it; the authority refuses it, because a rule that
# lives only in a prompt is a rule the next drafter will not have read. This
# is the exact shape ``research_quality_score`` finds in the published Initial
# Screens, and the whole point of carrying refs per sentence is that a body
# can be free of it.
_PROSE_TAG_RE = re.compile(r"(?<![A-Za-z0-9])[CN]\d{1,3}(?![A-Za-z0-9])")
_CJK_TERMINATORS = "。！？；」』）"
_PRINCIPALS = ("human:", "automation:")


class CompanyDossierError(RuntimeError):
    """Base error for the company dossier authority."""


class CompanyDossierValidationError(CompanyDossierError, ValueError):
    """A closed field or argument is invalid."""


class CompanyDossierConflict(CompanyDossierError):
    """The stored chain and the record disagree, or the caller raced it."""


class CompanyDossierNotFound(CompanyDossierError):
    """No such dossier version."""


class DossierStructureUnmapped(CompanyDossierError):
    """This Constitution's causal chain has no section mapping in the policy."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _text(value: Any, name: str, *, maximum: int = 2000) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CompanyDossierValidationError(f"{name} must be non-empty text")
    value = value.strip()
    if len(value) > maximum:
        raise CompanyDossierValidationError(f"{name} must be at most {maximum} characters")
    return value


def _one_of(value: Any, allowed: Sequence[str], name: str) -> str:
    if value not in allowed:
        raise CompanyDossierValidationError(
            f"{name} must be one of {', '.join(allowed)}; got {value!r}"
        )
    return str(value)


def _principal(value: Any, name: str = "actor_ref") -> str:
    value = _text(value, name, maximum=200)
    if not value.startswith(_PRINCIPALS):
        raise CompanyDossierValidationError(f"{name} must use a principal namespace")
    return value


def company_slug(company_ref: str) -> str:
    """A ref-safe short name, the way the forecast models make theirs."""

    return re.sub(r"[^A-Za-z0-9]+", "-", company_ref).strip("-").lower()


def dossier_ref_for(company_ref: str) -> str:
    return f"company-dossier:{company_slug(company_ref)}"


# ---------------------------------------------------------------------------
# structure: what the Constitution decides and what the policy declares
# ---------------------------------------------------------------------------

DEFAULT_POLICY_PATH = (
    Path(__file__).resolve().parents[2] / "deploy" / "phase9"
    / "p12a-dossier-policy-v1.json"
)
# Every criterion of the Constitution's ``output_rubric`` is either bound to a
# named structural check here or declared not applicable by the policy.  A
# criterion the policy does not mention at all fails the pre-publish check:
# silently ignoring a published standard is the failure this consumer exists
# to remove.
OUTPUT_RUBRIC_CHECKS: tuple[str, ...] = (
    # every figure in the prose is carried verbatim by a cited source
    "numbers_trace_to_refs",
    # this version cites something the last one did not (ADR-0008)
    "not_a_restatement",
    # no section asserts an investment conclusion; a dossier is a file, not a call
    "no_investment_conclusion",
)


def load_policy(path: str | Path | None = None) -> dict[str, Any]:
    """The dossier policy: causal-chain maps and output-rubric bindings."""

    source = Path(path) if path is not None else DEFAULT_POLICY_PATH
    policy = json.loads(source.read_text(encoding="utf-8"))
    return validate_policy(policy)


def validate_policy(policy: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(policy, Mapping):
        raise CompanyDossierValidationError("policy must be an object")
    wire = dict(policy)
    if set(wire) != {"schema_version", "policy_ref", "causal_chain_maps",
                     "output_rubric_bindings"}:
        raise CompanyDossierValidationError("policy has an invalid closed shape")
    if wire["schema_version"] != SCHEMA_VERSION:
        raise CompanyDossierValidationError("policy schema_version is not supported")
    _text(wire["policy_ref"], "policy_ref")
    maps = []
    for index, item in enumerate(wire["causal_chain_maps"] or []):
        if not isinstance(item, Mapping) or set(item) != {
            "constitution_ref", "causal_chain_hash", "sections", "note"
        }:
            raise CompanyDossierValidationError(
                f"causal_chain_maps[{index}] has an invalid closed shape")
        assignment = item["sections"]
        if not isinstance(assignment, list) or not assignment:
            raise CompanyDossierValidationError(
                f"causal_chain_maps[{index}].sections must be a non-empty array")
        for position, word in enumerate(assignment):
            _one_of(word, ("demand_drivers", "supply_and_cost", "none"),
                    f"causal_chain_maps[{index}].sections[{position}]")
        maps.append({
            "constitution_ref": _text(item["constitution_ref"], "constitution_ref"),
            "causal_chain_hash": _sha256(item["causal_chain_hash"], "causal_chain_hash"),
            "sections": list(assignment),
            "note": str(item["note"] or ""),
        })
    wire["causal_chain_maps"] = maps
    bindings = []
    for index, item in enumerate(wire["output_rubric_bindings"] or []):
        if not isinstance(item, Mapping) or set(item) != {
            "criterion_hash", "check", "reason"
        }:
            raise CompanyDossierValidationError(
                f"output_rubric_bindings[{index}] has an invalid closed shape")
        check = item["check"]
        if check is not None:
            _one_of(check, OUTPUT_RUBRIC_CHECKS, f"output_rubric_bindings[{index}].check")
        elif not str(item["reason"] or "").strip():
            raise CompanyDossierValidationError(
                f"output_rubric_bindings[{index}] declares no check and no reason")
        bindings.append({
            "criterion_hash": _sha256(item["criterion_hash"], "criterion_hash"),
            "check": check,
            "reason": str(item["reason"] or ""),
        })
    wire["output_rubric_bindings"] = bindings
    return wire


def _sha256(value: Any, name: str) -> str:
    value = _text(value, name, maximum=64)
    if _HASH_RE.fullmatch(value) is None:
        raise CompanyDossierValidationError(f"{name} must be lowercase SHA-256")
    return value


def policy_hash(policy: Mapping[str, Any]) -> str:
    return content_hash(validate_policy(policy))


def causal_chain_hash(chain: Sequence[str]) -> str:
    """The identity of one causal chain.

    The whole chain, in order: a mapping is by *position*, so a chain that
    gained a link is a chain whose mapping no longer applies.  That is the
    intended behaviour -- a new link is a new question about which section it
    belongs to, and only a person can answer it.
    """

    return content_hash([str(link) for link in chain])


def chain_assignment(
    constitution: Mapping[str, Any], policy: Mapping[str, Any]
) -> list[str]:
    """Which section each causal-chain link belongs to, per the policy."""

    chain = list((constitution.get("method") or {}).get("causal_chain") or [])
    if not chain:
        raise DossierStructureUnmapped("this Constitution states no causal chain")
    digest = causal_chain_hash(chain)
    for item in validate_policy(policy)["causal_chain_maps"]:
        if item["causal_chain_hash"] != digest:
            continue
        if len(item["sections"]) != len(chain):
            raise CompanyDossierConflict(
                "the policy's section assignment does not cover this causal chain")
        return list(item["sections"])
    raise DossierStructureUnmapped(
        f"causal chain {digest[:12]} of {constitution.get('constitution_ref')} has no "
        "section mapping; a person maps its links before these sections can be drafted"
    )


def section_slots(
    aspect: str,
    *,
    constitution: Mapping[str, Any] | None = None,
    policy: Mapping[str, Any] | None = None,
) -> tuple[dict[str, str], ...]:
    """The slots this section's draft must fill, and nothing the model chose.

    Eight sections have one slot -- the aspect's own definition, which is the
    same line P12b showed the tagger, so a Claim filed under an aspect and a
    dossier section about it are answering the same question.  The two the
    Constitution owns have one slot per mapped causal-chain link.
    """

    _one_of(aspect, SECTIONS, "aspect")
    if aspect not in ("demand_drivers", "supply_and_cost"):
        return ({"slot_id": aspect, "prompt": DEFINITIONS[aspect]},)
    if constitution is None or policy is None:
        raise DossierStructureUnmapped(
            f"{aspect} takes its structure from the Constitution's causal chain")
    chain = list((constitution.get("method") or {}).get("causal_chain") or [])
    assignment = chain_assignment(constitution, policy)
    slots = [
        {"slot_id": f"causal_chain:{index}", "prompt": str(link)}
        for index, (link, section) in enumerate(zip(chain, assignment))
        if section == aspect
    ]
    if not slots:
        raise DossierStructureUnmapped(
            f"the policy assigns no causal-chain link to {aspect}")
    return tuple(slots)


def unit_slots(
    unit: str,
    *,
    constitution: Mapping[str, Any] | None = None,
    policy: Mapping[str, Any] | None = None,
    market_view_available: bool = True,
) -> tuple[dict[str, str], ...]:
    """The slots of any drafted unit, section or otherwise."""

    if unit in SECTION_SET:
        return section_slots(unit, constitution=constitution, policy=policy)
    if unit == CLASSIFICATION_UNIT:
        return tuple(
            {"slot_id": slot, "prompt": CLASSIFICATION_SLOT_PROMPTS[slot]}
            for slot in CLASSIFICATION_SLOTS
        )
    if unit == VARIANT_UNIT:
        return tuple(
            {"slot_id": slot, "prompt": VARIANT_SLOT_PROMPTS[slot]}
            for slot in VARIANT_SLOTS
            if market_view_available or slot != "market_view"
        )
    raise CompanyDossierValidationError(f"unknown dossier unit: {unit!r}")


# ---------------------------------------------------------------------------
# the record
# ---------------------------------------------------------------------------

_RECORD_FIELDS = frozenset({
    "schema_version", "id", "created_at", "dossier_ref", "company_ref", "version",
    "prior_version_ref", "change_reason", "evidence_refs", "decision",
    "sections", "industry_classification", "variant_view", "drafted_at",
    "bindings", "generator_ref", "actor_ref", "body_hash", "content_hash",
})
# What the chain is *about*.  Not the reason it exists, not who asked, not
# when: two records with the same body say the same thing about the company.
_BODY_EXCLUDED = frozenset({
    "id", "created_at", "version", "prior_version_ref", "change_reason",
    "evidence_refs", "decision", "body_hash", "content_hash",
    # When each part was last written is not part of what the file says about
    # the company. It has to be outside the body or a redraft producing the
    # same prose would count as a different dossier purely because the clock
    # moved -- and the identical-body duplicate rule would never fire again.
    "drafted_at",
})
_BINDING_FIELDS = frozenset({
    "constitution_version", "playbook_version", "mission_version_ref",
    "policy_ref", "policy_hash", "causal_chain_hash", "rubric_ref", "rubric_hash",
})


def normalise_ref(value: Any, name: str) -> dict[str, Any]:
    """One citable thing: its kind, its ref, the text shown, its period."""

    if not isinstance(value, Mapping):
        raise CompanyDossierValidationError(f"{name} must be an object")
    wire = dict(value)
    if set(wire) - {"kind", "ref", "text", "period"} or not {"kind", "ref"} <= set(wire):
        raise CompanyDossierValidationError(f"{name} has an invalid closed shape")
    return {
        "kind": _one_of(wire.get("kind"), REF_KINDS, f"{name}.kind"),
        "ref": _text(wire.get("ref"), f"{name}.ref", maximum=512),
        # The text the model was shown, kept beside the ref: the number
        # discipline checks the prose against it, and a check that has to
        # re-fetch what was shown is a check that grades a different document.
        "text": _text(wire.get("text") or "-", f"{name}.text", maximum=1000),
        "period": (None if wire.get("period") is None
                   else _text(wire["period"], f"{name}.period", maximum=120)),
    }


def _sentence(value: Any, name: str, allowed_refs: set[str]) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != {"text", "refs"}:
        raise CompanyDossierValidationError(
            f"{name} must be exactly text and refs")
    text = _text(value["text"], f"{name}.text", maximum=MAX_SENTENCE_CHARS)
    tag = _PROSE_TAG_RE.search(text)
    if tag is not None:
        raise CompanyDossierValidationError(
            f"{name}.text writes the citation tag {tag.group(0)!r} into the prose; "
            "tags travel in refs, and a sentence whose subject is a tag becomes "
            "a sentence with no subject once the tag is gone")
    refs = value["refs"]
    if not isinstance(refs, list) or not refs:
        raise CompanyDossierValidationError(
            f"{name} cites nothing; every sentence of a dossier names its evidence")
    seen: list[str] = []
    for position, ref in enumerate(refs):
        ref = _text(ref, f"{name}.refs[{position}]", maximum=512)
        if ref not in allowed_refs:
            raise CompanyDossierValidationError(
                f"{name} cites {ref}, which was not among the material shown")
        if ref not in seen:
            seen.append(ref)
    return {"text": text, "refs": seen}


def _slot(
    value: Any, name: str, *, expected_id: str, allowed_refs: set[str]
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise CompanyDossierValidationError(f"{name} must be an object")
    wire = dict(value)
    if wire.get("slot_id") != expected_id:
        raise CompanyDossierValidationError(
            f"{name}.slot_id must be {expected_id!r}; the structure is not the "
            "model's to choose")
    if set(wire) == {"slot_id", "unknown"}:
        return {"slot_id": expected_id,
                "unknown": _text(wire["unknown"], f"{name}.unknown", maximum=MAX_SENTENCE_CHARS)}
    if set(wire) != {"slot_id", "sentences"}:
        raise CompanyDossierValidationError(
            f"{name} must be slot_id with either sentences or unknown")
    rows = wire["sentences"]
    if not isinstance(rows, list) or not rows:
        raise CompanyDossierValidationError(
            f"{name}.sentences is empty; say 'unknown' rather than nothing")
    if len(rows) > SLOT_SENTENCE_CAP:
        raise CompanyDossierValidationError(
            f"{name} writes {len(rows)} sentences; the cap is {SLOT_SENTENCE_CAP}")
    return {
        "slot_id": expected_id,
        "sentences": [
            _sentence(row, f"{name}.sentences[{index}]", allowed_refs)
            for index, row in enumerate(rows)
        ],
    }


def _gaps(value: Any, name: str) -> list[str]:
    rows = value or []
    if not isinstance(rows, list) or len(rows) > MAX_GAPS:
        raise CompanyDossierValidationError(f"{name} must be at most {MAX_GAPS} short strings")
    return [_text(row, f"{name}[]", maximum=MAX_GAP_CHARS) for row in rows]


def validate_section(value: Any, name: str) -> dict[str, Any]:
    """One section: drafted against a declared structure, or unavailable."""

    if not isinstance(value, Mapping):
        raise CompanyDossierValidationError(f"{name} must be an object")
    wire = dict(value)
    if set(wire) != {"aspect", "status", "reason", "structure", "slots",
                     "sources", "gaps", "profile"}:
        raise CompanyDossierValidationError(f"{name} has an invalid closed shape")
    aspect = _one_of(wire["aspect"], SECTIONS, f"{name}.aspect")
    status = _one_of(wire["status"], ("drafted", "unavailable"), f"{name}.status")
    structure = wire["structure"]
    if not isinstance(structure, list) or any(
        not isinstance(slot, str) or not slot for slot in structure
    ):
        raise CompanyDossierValidationError(f"{name}.structure must be a list of slot ids")
    if status == "unavailable":
        if wire["slots"] or wire["sources"]:
            raise CompanyDossierValidationError(
                f"{name} is unavailable and may not carry a draft")
        return {
            "aspect": aspect, "status": status,
            "reason": _one_of(wire["reason"], UNAVAILABLE_REASONS, f"{name}.reason"),
            "structure": list(structure), "slots": [], "sources": [],
            "gaps": _gaps(wire["gaps"], f"{name}.gaps"),
            "profile": _profile(wire["profile"], f"{name}.profile", aspect),
        }
    if wire["reason"] is not None:
        raise CompanyDossierValidationError(f"{name} is drafted and carries a reason")
    sources = wire["sources"]
    if not isinstance(sources, list) or not sources:
        raise CompanyDossierValidationError(
            f"{name} is drafted and cites nothing")
    if len(sources) > MAX_SOURCES_PER_SECTION:
        raise CompanyDossierValidationError(
            f"{name} shows {len(sources)} sources; the cap is {MAX_SOURCES_PER_SECTION}")
    checked_sources = [
        normalise_ref(row, f"{name}.sources[{index}]")
        for index, row in enumerate(sources)
    ]
    refs = {row["ref"] for row in checked_sources}
    if len(refs) != len(checked_sources):
        raise CompanyDossierValidationError(f"{name}.sources repeats a ref")
    slots = wire["slots"]
    if not isinstance(slots, list) or len(slots) != len(structure):
        raise CompanyDossierValidationError(
            f"{name} fills {len(slots or [])} slots of {len(structure)}; every slot "
            "is answered or explicitly unknown")
    checked = [
        _slot(row, f"{name}.slots[{index}]", expected_id=structure[index],
              allowed_refs=refs)
        for index, row in enumerate(slots)
    ]
    written = sum(len(slot.get("sentences") or ()) for slot in checked)
    if written > SECTION_SENTENCE_CAP:
        raise CompanyDossierValidationError(
            f"{name} writes {written} sentences; the cap is {SECTION_SENTENCE_CAP}")
    if not written:
        raise CompanyDossierValidationError(
            f"{name} is drafted but every slot is unknown; that section is "
            "unavailable, not drafted")
    cited = {ref for slot in checked for row in slot.get("sentences") or ()
             for ref in row["refs"]}
    unused = refs - cited
    if unused:
        raise CompanyDossierValidationError(
            f"{name} lists sources no sentence cites: {sorted(unused)[:3]}")
    return {
        "aspect": aspect, "status": status, "reason": None,
        "structure": list(structure), "slots": checked, "sources": checked_sources,
        "gaps": _gaps(wire["gaps"], f"{name}.gaps"),
        "profile": _profile(wire["profile"], f"{name}.profile", aspect),
    }


def _profile(value: Any, name: str, aspect: str) -> dict[str, Any] | None:
    """P12f's computed guidance table, carried by its own section only."""

    if value is None:
        return None
    if aspect != "guidance_style":
        raise CompanyDossierValidationError(
            f"{name} belongs to the guidance_style section")
    from .guidance_profile import validate_profile

    return validate_profile(value)


def validate_classification(value: Any, name: str = "industry_classification") -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise CompanyDossierValidationError(f"{name} must be an object")
    wire = dict(value)
    if set(wire) != {"classification", "slots", "sources", "gaps"}:
        raise CompanyDossierValidationError(f"{name} has an invalid closed shape")
    classification = _one_of(
        wire["classification"], INDUSTRY_CLASSIFICATIONS, f"{name}.classification")
    sources = [normalise_ref(row, f"{name}.sources[{index}]")
               for index, row in enumerate(wire["sources"] or [])]
    refs = {row["ref"] for row in sources}
    slots = wire["slots"] or []
    if len(slots) != len(CLASSIFICATION_SLOTS):
        raise CompanyDossierValidationError(
            f"{name} fills {len(slots)} slots of {len(CLASSIFICATION_SLOTS)}")
    checked = [
        _slot(row, f"{name}.slots[{index}]",
              expected_id=CLASSIFICATION_SLOTS[index], allowed_refs=refs)
        for index, row in enumerate(slots)
    ]
    if classification != "insufficient_evidence" and not refs:
        raise CompanyDossierValidationError(
            f"{name} classifies the company and cites nothing")
    return {"classification": classification, "slots": checked, "sources": sources,
            "gaps": _gaps(wire["gaps"], f"{name}.gaps")}


def validate_variant_view(value: Any, name: str = "variant_view") -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise CompanyDossierValidationError(f"{name} must be an object")
    wire = dict(value)
    if set(wire) != {"status", "reason", "market_view_available",
                     "market_view_reason", "structure", "slots", "sources", "gaps"}:
        raise CompanyDossierValidationError(f"{name} has an invalid closed shape")
    status = _one_of(wire["status"], ("drafted", "unavailable"), f"{name}.status")
    available = wire["market_view_available"]
    if not isinstance(available, bool):
        raise CompanyDossierValidationError(f"{name}.market_view_available must be boolean")
    if status == "unavailable":
        if wire["slots"] or wire["sources"]:
            raise CompanyDossierValidationError(
                f"{name} is unavailable and may not carry a draft")
        return {
            "status": status,
            "reason": _one_of(wire["reason"], UNAVAILABLE_REASONS, f"{name}.reason"),
            "market_view_available": available,
            "market_view_reason": (None if wire["market_view_reason"] is None
                                   else _text(wire["market_view_reason"],
                                              f"{name}.market_view_reason")),
            "structure": [], "slots": [], "sources": [],
            "gaps": _gaps(wire["gaps"], f"{name}.gaps"),
        }
    expected = [slot for slot in VARIANT_SLOTS if available or slot != "market_view"]
    structure = list(wire["structure"] or [])
    if structure != expected:
        raise CompanyDossierValidationError(
            f"{name}.structure must be {expected}; the structure is not the "
            "model's to choose")
    if not available and wire["market_view_reason"] is None:
        raise CompanyDossierValidationError(
            f"{name} has no market view and does not say why")
    sources = [normalise_ref(row, f"{name}.sources[{index}]")
               for index, row in enumerate(wire["sources"] or [])]
    refs = {row["ref"] for row in sources}
    slots = wire["slots"] or []
    if len(slots) != len(structure):
        raise CompanyDossierValidationError(
            f"{name} fills {len(slots)} slots of {len(structure)}")
    checked = [
        _slot(row, f"{name}.slots[{index}]", expected_id=structure[index],
              allowed_refs=refs)
        for index, row in enumerate(slots)
    ]
    signals = next((slot for slot in checked if slot["slot_id"] == "observable_signals"), None)
    if signals is not None and len(signals.get("sentences") or ()) > MAX_SIGNALS:
        raise CompanyDossierValidationError(
            f"{name} lists more than {MAX_SIGNALS} observable signals")
    return {
        "status": status, "reason": None, "market_view_available": available,
        "market_view_reason": (None if wire["market_view_reason"] is None
                               else _text(wire["market_view_reason"],
                                          f"{name}.market_view_reason")),
        "structure": structure, "slots": checked, "sources": sources,
        "gaps": _gaps(wire["gaps"], f"{name}.gaps"),
    }


def _drafted_at(value: Any, name: str = "drafted_at") -> dict[str, str]:
    """When each unit was last written, by unit.

    Carried forward unchanged for the units a version did not touch, which is
    what makes "has anything arrived since *this part* was written" answerable.
    Comparing against the head of the chain instead would mean that once any
    part was redrafted, every part that had never been drafted looked current
    -- and with three units a tick and twelve units, most of the file would
    never be written at all.
    """

    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise CompanyDossierValidationError(f"{name} must be an object")
    out: dict[str, str] = {}
    for unit, when in value.items():
        if unit not in UNITS:
            raise CompanyDossierValidationError(
                f"{name} names {unit!r}, which is not a dossier unit")
        out[unit] = _text(when, f"{name}[{unit}]", maximum=64)
    return dict(sorted(out.items()))


def _bindings(value: Any, name: str = "bindings") -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != _BINDING_FIELDS:
        raise CompanyDossierValidationError(f"{name} has an invalid closed shape")
    wire = dict(value)
    out: dict[str, Any] = {}
    for field in ("constitution_version", "playbook_version"):
        item = wire[field]
        if not isinstance(item, Mapping) or set(item) != {"ref", "hash"}:
            raise CompanyDossierValidationError(f"{name}.{field} must be ref and hash")
        out[field] = {
            "ref": _text(item["ref"], f"{name}.{field}.ref", maximum=512),
            "hash": _sha256(item["hash"], f"{name}.{field}.hash"),
        }
    out["mission_version_ref"] = (
        None if wire["mission_version_ref"] is None
        else _text(wire["mission_version_ref"], f"{name}.mission_version_ref", maximum=512))
    out["policy_ref"] = _text(wire["policy_ref"], f"{name}.policy_ref")
    out["policy_hash"] = _sha256(wire["policy_hash"], f"{name}.policy_hash")
    out["causal_chain_hash"] = (
        None if wire["causal_chain_hash"] is None
        else _sha256(wire["causal_chain_hash"], f"{name}.causal_chain_hash"))
    out["rubric_ref"] = _text(wire["rubric_ref"], f"{name}.rubric_ref")
    out["rubric_hash"] = _sha256(wire["rubric_hash"], f"{name}.rubric_hash")
    return out


def validate_dossier_version(value: Mapping[str, Any]) -> dict[str, Any]:
    """One CompanyDossierVersion, closed and self-consistent."""

    if not isinstance(value, Mapping):
        raise CompanyDossierValidationError("dossier version must be an object")
    wire = dict(value)
    if set(wire) != _RECORD_FIELDS or wire.get("schema_version") != SCHEMA_VERSION:
        raise CompanyDossierValidationError(
            "company dossier version has an invalid closed shape; "
            f"missing={sorted(_RECORD_FIELDS - set(wire))}, "
            f"unknown={sorted(set(wire) - _RECORD_FIELDS)}")
    for field in ("id", "created_at", "dossier_ref", "company_ref", "generator_ref"):
        wire[field] = _text(wire[field], field, maximum=512)
    wire["actor_ref"] = _principal(wire["actor_ref"])
    wire["change_reason"] = _one_of(wire["change_reason"], CHANGE_REASONS, "change_reason")
    if type(wire["version"]) is not int or wire["version"] < 1:
        raise CompanyDossierValidationError("version must be a positive integer")
    if wire["prior_version_ref"] is not None:
        wire["prior_version_ref"] = _text(wire["prior_version_ref"], "prior_version_ref",
                                          maximum=512)
    wire["decision"] = (None if wire["decision"] is None
                        else _text(wire["decision"], "decision"))
    evidence = wire["evidence_refs"]
    if not isinstance(evidence, list) or not evidence:
        raise CompanyDossierValidationError(
            "a version must name the evidence that occasioned it")
    wire["evidence_refs"] = [
        normalise_ref(row, f"evidence_refs[{index}]")
        for index, row in enumerate(evidence)
    ]
    sections = wire["sections"]
    if not isinstance(sections, list) or len(sections) != len(SECTIONS):
        raise CompanyDossierValidationError(
            f"a dossier has exactly {len(SECTIONS)} sections")
    checked = []
    for index, section in enumerate(sections):
        row = validate_section(section, f"sections[{index}]")
        if row["aspect"] != SECTIONS[index]:
            raise CompanyDossierValidationError(
                f"sections[{index}] must be {SECTIONS[index]!r}; the ten sections "
                "are in the aspect vocabulary's order")
        checked.append(row)
    wire["sections"] = checked
    wire["industry_classification"] = validate_classification(wire["industry_classification"])
    wire["variant_view"] = validate_variant_view(wire["variant_view"])
    wire["drafted_at"] = _drafted_at(wire["drafted_at"])
    wire["bindings"] = _bindings(wire["bindings"])
    wire["body_hash"] = _sha256(wire["body_hash"], "body_hash")
    wire["content_hash"] = _sha256(wire["content_hash"], "content_hash")
    if body_hash(wire) != wire["body_hash"]:
        raise CompanyDossierConflict("company dossier body_hash is not its body")
    base = {key: item for key, item in wire.items() if key != "content_hash"}
    if content_hash(base) != wire["content_hash"]:
        raise CompanyDossierConflict("company dossier content_hash drifted")
    return wire


def body_hash(record: Mapping[str, Any]) -> str:
    """What this version says about the company, and nothing else."""

    return content_hash({
        key: value for key, value in record.items()
        if key in _RECORD_FIELDS and key not in _BODY_EXCLUDED
    })


def evidence_scope(record: Mapping[str, Any]) -> list[str]:
    """Every ref this version rests on, sorted.  ADR-0008 reads this."""

    refs: set[str] = set()
    for section in record.get("sections") or []:
        refs.update(row["ref"] for row in section.get("sources") or [])
    for block in (record.get("industry_classification"), record.get("variant_view")):
        for row in (block or {}).get("sources") or []:
            refs.add(row["ref"])
    return sorted(refs)


def new_refs(record: Mapping[str, Any], prior: Mapping[str, Any] | None) -> list[str]:
    """The refs this version cites that the current one did not."""

    if prior is None:
        return evidence_scope(record)
    return sorted(set(evidence_scope(record)) - set(evidence_scope(prior)))


# ---------------------------------------------------------------------------
# readable projections
# ---------------------------------------------------------------------------


def section_body(section: Mapping[str, Any]) -> str:
    """The prose a reader sees, assembled from the sentence rows.

    Assembled here rather than written by the model: the tags never enter the
    text, so nothing is left behind when they are stripped, because they were
    never in it.
    """

    body = ""
    for slot in section.get("slots") or []:
        for row in slot.get("sentences") or ():
            text = row["text"]
            if body and body[-1] not in _CJK_TERMINATORS:
                # Chinese sentences carry their own full stop and need no
                # space; anything else runs together into one long word.
                body += " "
            body += text
    return body


def _artefact_sections(record: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The ten sections and the two blocks, as one list of scoreable sections.

    The blocks are included because the rubric's "does this version cite
    anything new" check reads this list, and a version whose only new evidence
    is in the classification or the variant view has learned something. Leaving
    them out would make the quality gate stricter than ADR-0008 in exactly the
    corner where the file gets sharper.
    """

    def one(title: str, block: Mapping[str, Any]) -> dict[str, Any]:
        sources = block.get("sources") or []
        return {
            "title": title,
            "body": section_body(block),
            "claim_refs": [row["ref"] for row in sources if row["kind"] == "claim"],
            "numbers": [
                {
                    "text": row["text"],
                    "claim_version_ref": row["ref"] if row["kind"] == "claim" else "",
                    "period": row.get("period"),
                }
                for row in sources
            ],
            "gaps": block.get("gaps") or [],
        }

    out = [one(section["aspect"], section) for section in record.get("sections") or []]
    for title, key in ((CLASSIFICATION_UNIT, "industry_classification"),
                       (VARIANT_UNIT, "variant_view")):
        block = record.get(key)
        if block:
            out.append(one(title, block))
    return out


def dossier_artefact(
    record: Mapping[str, Any], *, prior: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    """The dossier as Q1's scoreable artefact shape.

    Built here rather than through ``research_quality_score.artefact_from_dossier``
    for one reason: that adapter names Q1's own ten Chinese section titles, and
    the aspect vocabulary is what this authority stores. Aligning the two is an
    integration item (see the report); until then the titles this passes are the
    ones the document actually has.

    ``claim_version_ref`` carries only claim-kind refs, because Q1's
    ``claim_refs_resolve`` resolves refs against ``claim_versions``: a figure
    ref there would be reported as an unresolvable Claim. The figure and
    forecast refs are still checked -- by this module's own resolver, against
    the authorities that own them.
    """

    from .research_quality_score import artefact

    sections = _artefact_sections(record)
    return artefact(
        artefact_kind="company_dossier",
        ref=str(record["id"]),
        hash=str(record["content_hash"]),
        title=str(record.get("dossier_ref") or ""),
        subject_ref=record.get("company_ref"),
        sections=sections,
        gaps=[gap for section in sections for gap in section["gaps"]],
        expected_sections=SECTIONS,
        prior=None if prior is None else {"sections": _artefact_sections(prior)},
    )


# ---------------------------------------------------------------------------
# the Constitution's output_rubric, as a pre-publish structural check
# ---------------------------------------------------------------------------

# Words that turn a file into a call.  Deliberately short and deliberately
# unambiguous: a dossier says what is true about a company, and the decision
# about what to do with it is a Thesis, admitted by a person (ADR-0001).
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
    finding.  The Constitution is a published standard; a consumer that reads
    the criteria it happens to understand and ignores the rest is not a
    consumer of the standard, it is a consumer of its own list.
    """

    from .mission_deliverable import unsourced_numbers

    bindings = {
        item["criterion_hash"]: item
        for item in validate_policy(policy)["output_rubric_bindings"]
    }
    criteria = list(((constitution.get("method") or {}).get("output_rubric") or {})
                    .get("criteria") or [])
    findings: list[dict[str, Any]] = []
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
        check = binding["check"]
        if check is None:
            continue
        # The whole document, which is the ten sections *and* the two blocks:
        # "低估" and a target price live in ``our_view`` if they live anywhere,
        # and a standard applied to nine tenths of a document is not applied.
        parts = _artefact_sections(record)
        if check == "numbers_trace_to_refs":
            for part in parts:
                for token in unsourced_numbers(part["body"], part["numbers"]):
                    findings.append({
                        "code": "number_without_source", "criterion_index": index,
                        "section": part["title"], "figure": token,
                    })
        elif check == "not_a_restatement":
            if prior is not None and not new_refs(record, prior):
                findings.append({
                    "code": "no_new_evidence", "criterion_index": index,
                })
        elif check == "no_investment_conclusion":
            for part in parts:
                for pattern in _CONCLUSION_PATTERNS:
                    if pattern in part["body"]:
                        findings.append({
                            "code": "investment_conclusion", "criterion_index": index,
                            "section": part["title"], "phrase": pattern,
                        })
    return findings


# ---------------------------------------------------------------------------
# the authority
# ---------------------------------------------------------------------------

SOURCE_VERSION_KEY = "computed_from_version_ref"
_UNSET = object()


class CompanyDossierAuthority:
    """Append-only CompanyDossierVersions, one chain per company.

    It publishes what it is handed and refuses what does not learn anything.
    It never decides that a dossier should be re-written: ADR-0008's entry
    points take a ``change_reason`` and evidence refs *from their caller*, and
    the judgement about whether the file should move lives one layer up.
    """

    _authorized = authorized_flag()

    def __init__(self, store: DaltonStore):
        self.store = store
        self.connection = store.connection
        self._authorization_flag = authorization_flag(
            self.connection, "dalton_company_dossier_authorized")
        self.connection.executescript(_SCHEMA_PATH.read_text(encoding="utf-8"))

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Cursor]:
        if self._authorized:
            raise RuntimeError("CompanyDossierAuthority operation cannot be nested")
        self._authorized = True
        try:
            with self.store._transaction() as cur:
                yield cur
        finally:
            self._authorized = False

    def publish(self, body: Mapping[str, Any]) -> dict[str, Any]:
        """Store one dossier version, or say why it is not one.

        Two ways to be a ``duplicate``, and they are different findings:
        the body is byte-for-byte what the chain already holds, or the body
        moved but every ref it rests on was already cited by the current
        version.  The second is the one ADR-0008 was written for -- a rewrite
        of an unchanged world is noise wearing a version number.
        """

        body = dict(body)
        source = body.pop(SOURCE_VERSION_KEY, _UNSET)
        for field in _BODY_EXCLUDED - {"change_reason", "evidence_refs",
                                       "decision", "drafted_at"}:
            body.pop(field, None)
        company_ref = _text(body.get("company_ref"), "company_ref", maximum=512)
        dossier_ref = dossier_ref_for(company_ref)
        body["dossier_ref"] = dossier_ref
        body.setdefault("schema_version", SCHEMA_VERSION)
        body.setdefault("generator_ref", GENERATOR_REF)
        change_reason = _one_of(body.get("change_reason"), CHANGE_REASONS, "change_reason")
        if not body.get("evidence_refs"):
            raise CompanyDossierValidationError(
                "a version must name the evidence that occasioned it")
        digest = body_hash(body)
        latest_row = self.connection.execute(
            "SELECT * FROM company_dossier_versions WHERE dossier_ref=? "
            "ORDER BY version_number DESC LIMIT 1", (dossier_ref,),
        ).fetchone()
        latest = None if latest_row is None else self.dossier(latest_row["version_id"])
        if latest is not None and latest["body_hash"] == digest:
            return {**latest, "status": "duplicate", "duplicate_reason": "identical_body"}
        if latest is not None and not new_refs(body, latest):
            return {
                **latest, "status": "duplicate",
                "duplicate_reason": "no_new_evidence",
                "detail": ("this draft cites nothing the current version does not; "
                           "ADR-0008 refuses it rather than storing a rewrite"),
            }
        head = None if latest is None else str(latest["id"])
        if source is not _UNSET and source != head:
            raise CompanyDossierConflict(
                f"this dossier is now at {head or 'no version'}, and this body was "
                f"computed from {source or 'no version'}")
        version = 1 if latest is None else int(latest["version"]) + 1
        version_id = f"company-dossier-version:{company_slug(company_ref)}:{version}"
        record = {
            **body,
            "id": version_id,
            "created_at": _now(),
            "version": version,
            "prior_version_ref": head,
            "change_reason": change_reason,
            "body_hash": digest,
        }
        record.setdefault("decision", None)
        record["drafted_at"] = _drafted_at(record.get("drafted_at"))
        record["content_hash"] = content_hash(record)
        wire = validate_dossier_version(record)
        scope_hash = content_hash(evidence_scope(wire))
        with self._transaction() as cur:
            if cur.execute(
                "SELECT 1 FROM company_dossier_versions WHERE version_id=?", (version_id,)
            ).fetchone():
                raise CompanyDossierConflict("company dossier version id already exists")
            cur.execute(
                "INSERT INTO company_dossier_versions"
                "(version_id,dossier_ref,version_number,prior_version_id,company_ref,"
                "change_reason,body_hash,evidence_scope_hash,record_json,content_hash,"
                "actor_ref,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    version_id, dossier_ref, version, head, company_ref, change_reason,
                    digest, scope_hash, canonical_json(wire), wire["content_hash"],
                    wire["actor_ref"], wire["created_at"],
                ),
            )
        stored = self.dossier(version_id)
        if stored["content_hash"] != wire["content_hash"]:
            raise CompanyDossierConflict("company dossier did not read back as written")
        return {**stored, "status": "fresh"}

    def revise(self, body: Mapping[str, Any], **kwargs: Any) -> dict[str, Any]:
        """ADR-0008's revision entry point.  Same rules; a named door."""

        return self.publish({**dict(body), **kwargs})

    def dossier(self, version_ref: str) -> dict[str, Any]:
        version_ref = _text(version_ref, "version_ref", maximum=512)
        row = self.connection.execute(
            "SELECT * FROM company_dossier_versions WHERE version_id=?", (version_ref,)
        ).fetchone()
        if row is None:
            raise CompanyDossierNotFound("company dossier version was not found")
        wire = validate_dossier_version(json.loads(row["record_json"]))
        if (
            wire["id"] != row["version_id"]
            or wire["dossier_ref"] != row["dossier_ref"]
            or wire["version"] != row["version_number"]
            or wire["prior_version_ref"] != row["prior_version_id"]
            or wire["company_ref"] != row["company_ref"]
            or wire["change_reason"] != row["change_reason"]
            or wire["body_hash"] != row["body_hash"]
            or wire["content_hash"] != row["content_hash"]
            or content_hash(evidence_scope(wire)) != row["evidence_scope_hash"]
        ):
            raise CompanyDossierConflict("company dossier authority drifted")
        return wire

    def latest(self, company_ref: str) -> dict[str, Any] | None:
        company_ref = _text(company_ref, "company_ref", maximum=512)
        row = self.connection.execute(
            "SELECT version_id FROM company_dossier_versions WHERE company_ref=? "
            "ORDER BY version_number DESC LIMIT 1", (company_ref,),
        ).fetchone()
        return None if row is None else self.dossier(row["version_id"])

    def versions(self, company_ref: str) -> list[dict[str, Any]]:
        company_ref = _text(company_ref, "company_ref", maximum=512)
        rows = self.connection.execute(
            "SELECT version_id FROM company_dossier_versions WHERE company_ref=? "
            "ORDER BY version_number", (company_ref,),
        ).fetchall()
        return [self.dossier(row["version_id"]) for row in rows]

    def companies(self) -> list[str]:
        return [str(row["company_ref"]) for row in self.connection.execute(
            "SELECT DISTINCT company_ref FROM company_dossier_versions "
            "ORDER BY company_ref").fetchall()]

    def replay_section(self, company_ref: str, aspect: str) -> list[dict[str, Any]]:
        """What every version of this file said about one section, and why.

        ADR-0008's replay requirement: "what did we know and what did we
        conclude as of version N" has to be answerable from the chain alone.
        """

        _one_of(aspect, SECTIONS, "aspect")
        out = []
        for record in self.versions(company_ref):
            section = next(
                (item for item in record["sections"] if item["aspect"] == aspect), None)
            if section is None:
                continue
            out.append({
                "version_ref": record["id"], "version": record["version"],
                "created_at": record["created_at"],
                "change_reason": record["change_reason"],
                "status": section["status"], "reason": section["reason"],
                "body": section_body(section),
                "refs": [row["ref"] for row in section.get("sources") or []],
            })
        return out


__all__ = [
    "CLASSIFICATION_SLOTS",
    "CHANGE_REASONS",
    "CLASSIFICATION_UNIT",
    "GENERATOR_REF",
    "INDUSTRY_CLASSIFICATIONS",
    "MAX_SIGNALS",
    "OUTPUT_RUBRIC_CHECKS",
    "REF_KINDS",
    "SCHEMA_VERSION",
    "SECTIONS",
    "SECTION_SENTENCE_CAP",
    "SLOT_SENTENCE_CAP",
    "SOURCE_VERSION_KEY",
    "UNAVAILABLE_REASONS",
    "UNITS",
    "VARIANT_SLOTS",
    "VARIANT_UNIT",
    "WRITE_SCOPE",
    "CompanyDossierAuthority",
    "CompanyDossierConflict",
    "CompanyDossierError",
    "CompanyDossierNotFound",
    "CompanyDossierValidationError",
    "DossierStructureUnmapped",
    "body_hash",
    "causal_chain_hash",
    "chain_assignment",
    "company_slug",
    "dossier_artefact",
    "dossier_ref_for",
    "evidence_scope",
    "load_policy",
    "new_refs",
    "normalise_ref",
    "output_rubric_findings",
    "policy_hash",
    "section_body",
    "section_slots",
    "unit_slots",
    "validate_classification",
    "validate_dossier_version",
    "validate_policy",
    "validate_section",
    "validate_variant_view",
]
