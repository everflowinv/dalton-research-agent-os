"""P12d: the Deep Insight Gate -- twelve answers drafted, one person deciding.

The Playbook has asked twelve questions of every company since Phase 9 and
nothing has ever answered them: the gap analysis records the stage as "有合同无
lane".  This module is the contract for the answer and for the decision, and it
is deliberately two things rather than one.

**The draft is an output, so it obeys ADR-0008.**  It is a chain per company,
append-only, every version carrying a ``change_reason`` and the evidence refs
that occasioned it; a draft that cites nothing the current one does not is a
``duplicate`` rather than a version.  Nothing here re-drafts on its own -- the
authority exposes ``publish`` and ``revise`` and never calls them.

**The decision is a person's, so it is not an output at all.**  It is a
separate append-only record, one per draft version, bound to that version's
content hash, and it refuses any actor that is not ``human:``.  A verdict that
does not name the exact draft it read is a verdict about nothing, and a gate
that automation could pass would make the Playbook's ``human_checkpoint`` flag
decoration.  Approving is what lets the company enter the next stage; the
ladder itself stays where it already is, in ``CoverageMission.record_stage``.

Three rules give the twelve answers their shape.

*The questions are the bound Playbook's, read back by hash.*  They are not
copied into this file.  A draft binds the playbook version it answered and the
hash of the twelve question strings, so a gate answered under one methodology
can never be read as though it had been answered under another.

*An answer is sentences with refs, or it is ``unknown`` and says what evidence
would settle it.*  Padding is the failure mode of a twelve-question document:
eleven honest unknowns and one real answer is a better file than twelve
paragraphs of plausible prose, and only the first kind tells the owner what to
buy next.

*Question one is checked, not trusted.*  The dossier already carries the
company's ``industry_classification``, chosen from a closed list of five kinds
plus "the evidence does not say".  A gate whose first answer disagrees with the
file it was drafted from is not a disagreement worth reading -- it is two
models guessing -- so the whole draft is refused rather than published with a
contradiction inside it.
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

from .company_dossier import (
    INDUSTRY_CLASSIFICATIONS,
    OUTPUT_RUBRIC_CHECKS,
    validate_policy,
)
# One vocabulary for why a version exists, shared with Wave 1C's forecast
# models and P12a's dossier rather than copied: ADR-0008 is a contract over
# every output-class authority, and two closed lists claiming to be the same
# list is how a contract stops being one.
from .model_forecast_driver import CHANGE_REASONS
from .research_quality_rubrics import Criterion, Rubric
from .store import DaltonStore, canonical_json, content_hash

SCHEMA_VERSION = "0.1"
_SCHEMA_PATH = Path(__file__).with_name("deep_insight_gate_schema.sql")
GENERATOR_REF = "generator:deep-insight-gate:0.1"
# The gate draft is a deliverable-class output and takes the deliverable grant.
# It has no word of its own in ``AUTOMATION_WRITE_SCOPES`` on purpose: the gate
# is the Playbook's own exit document for a stage, which is what ``deliverable``
# already means, and a mission that granted one and not the other would be
# saying something nobody meant.
WRITE_SCOPE = "deliverable"
# The kind this draft takes in ``mission_deliverable.DELIVERABLE_KINDS`` when it
# is rendered for a reader.  Named here so the two spellings cannot drift.
DELIVERABLE_KIND = "deep_insight_gate"
STAGE_REF = "deep_insight_gate"

# The Playbook's Deep Insight Gate asks exactly twelve questions.  The text is
# the Playbook's and is read back from the bound version; the *count* is the
# contract, because "the 12 questions" is what the blueprint, the mission and
# the owner all call this stage.
QUESTION_COUNT = 12
QUESTION_REFS: tuple[str, ...] = tuple(f"q{index}" for index in range(1, QUESTION_COUNT + 1))

# Which questions are drafted in one bounded call.  Grouped by the material
# they need rather than by number: the four industry questions read the same
# rows, and a call per question would pay four times for one table.
GROUPS: tuple[str, ...] = ("industry", "company", "market", "thesis")
GROUP_QUESTIONS: Mapping[str, tuple[str, ...]] = MappingProxyType({
    "industry": ("q1", "q2", "q3", "q4"),
    "company": ("q5", "q6"),
    "market": ("q7", "q8"),
    "thesis": ("q9", "q10", "q11", "q12"),
})
GROUP_OF: Mapping[str, str] = MappingProxyType({
    question: group
    for group, questions in GROUP_QUESTIONS.items()
    for question in questions
})

# Where each question's material comes from, as dossier sections plus the named
# non-dossier sources.  This is the one mapping between the Playbook's twelve
# questions and the ten-word aspect vocabulary, and it is frozen and hashed so
# that a draft records which mapping it was drafted under.
DOSSIER_SOURCES: Mapping[str, tuple[str, ...]] = MappingProxyType({
    "q1": ("business_model", "segments_and_mix", "demand_drivers", "supply_and_cost"),
    "q2": ("demand_drivers", "kpi_dictionary"),
    "q3": ("supply_and_cost",),
    "q4": ("demand_drivers", "competitive_position"),
    "q5": ("competitive_position", "business_model"),
    "q6": ("management_and_capital_allocation", "guidance_style"),
    "q7": ("history_of_price_drivers",),
    "q8": ("history_of_price_drivers", "competitive_position"),
    "q9": ("kpi_dictionary", "demand_drivers"),
    "q10": ("catalyst_calendar", "segments_and_mix"),
    "q11": ("segments_and_mix", "competitive_position"),
    "q12": ("kpi_dictionary", "catalyst_calendar"),
})
# The non-dossier material a question is shown.  ``classification`` is the
# dossier's own block, ``variant_view`` likewise; the rest come from the
# DebateMap, the forecast model and the valuation snapshot.
EXTRA_SOURCES: Mapping[str, tuple[str, ...]] = MappingProxyType({
    "q1": ("classification",),
    "q4": ("debates",),
    "q7": ("valuation", "debates"),
    "q8": ("debates", "variant_view"),
    "q9": ("variant_view", "debates"),
    "q10": ("forecast", "valuation"),
    "q11": ("variant_view", "debates"),
    "q12": ("gaps", "rejected_debates"),
})
EXTRA_KINDS: tuple[str, ...] = (
    "classification", "variant_view", "debates", "rejected_debates",
    "forecast", "valuation", "gaps",
)
QUESTION_SOURCE_MAP_REF = "deep-insight-gate-source-map:p12d:v1"
QUESTION_SOURCE_MAP_HASH = content_hash({
    "ref": QUESTION_SOURCE_MAP_REF,
    "dossier": {key: list(value) for key, value in DOSSIER_SOURCES.items()},
    "extra": {key: list(value) for key, value in EXTRA_SOURCES.items()},
})

# What a sentence may cite.  Six kinds because the gate rests on six different
# authorities and a reader has to know which door to open.
REF_KINDS: tuple[str, ...] = (
    "claim", "figure", "forecast_cell", "valuation_metric",
    "dossier_section", "debate",
)

ANSWER_STATUSES: tuple[str, ...] = ("answered", "unknown")
# How sure the draft is, on the scale a person can argue with.  Required on an
# answered question and forbidden on an unknown one: "low confidence in nothing"
# is not a grade, it is a category error.
CONFIDENCE_LEVELS: tuple[str, ...] = ("high", "medium", "low")
# Why a question has no answer.  Closed, because "nobody has told us" and "the
# authority that would answer this does not exist yet" are different facts, and
# the second is a roadmap item rather than a gap in the research.
UNKNOWN_REASONS: tuple[str, ...] = (
    # no material at all was shown for this question
    "no_material_shown",
    # the authority that would answer it is not on this Core
    "source_unavailable",
    # this question's group came back outside its contract and was refused whole
    "group_refused",
    # the question was not drafted on this run and has no prior answer to carry
    "not_drafted_this_run",
)

# The three decisions a person may make, and only a person.
DECISIONS: tuple[str, ...] = ("approve", "return_for_more_work", "reject")
DECISION_LABELS: Mapping[str, str] = MappingProxyType({
    "approve": "通过：这家公司可以进入下一阶段",
    "return_for_more_work": "退回补充：证据变厚后再出一版",
    "reject": "否决：这家公司现在不值得深度覆盖",
})
# What each decision does to the mission stage ladder.  ``None`` writes no
# stage record at all: a returned draft is not a failed gate, it is a gate
# nobody has decided yet.
DECISION_STAGE_STATUS: Mapping[str, str | None] = MappingProxyType({
    "approve": "gate_passed",
    "return_for_more_work": None,
    "reject": "gate_failed",
})

# Bounds.  A gate answer is a paragraph a PM reads in the meeting, not a
# section: three sentences is the whole budget, and the twelfth question's
# answer is worth more than the eleventh sentence of the second.
SENTENCES_PER_ANSWER = 3
MAX_SENTENCE_CHARS = 400
MAX_SOURCES_PER_ANSWER = 30
MAX_GAPS = 4
MAX_GAP_CHARS = 300
MAX_UNKNOWN_CHARS = 400

_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
# The citation scaffolding, as it appears when it leaks into prose.  Same shape
# the dossier refuses, and for the same reason P13ap found in the live Initial
# Screens: a body that uses tags as words cannot survive their removal.
_PROSE_TAG_RE = re.compile(r"(?<![A-Za-z0-9])[CND]\d{1,3}(?![A-Za-z0-9])")
_CJK_TERMINATORS = "。！？；」』）"
_PRINCIPALS = ("human:", "automation:")
_HUMAN_RE = re.compile(r"^human:[A-Za-z0-9][A-Za-z0-9._/@:-]*$")


class DeepInsightGateError(RuntimeError):
    """Base error for the Deep Insight Gate authority."""


class DeepInsightGateValidationError(DeepInsightGateError, ValueError):
    """A closed field or argument is invalid."""


class DeepInsightGateConflict(DeepInsightGateError):
    """The stored chain and the record disagree, or the caller raced it."""


class DeepInsightGateNotFound(DeepInsightGateError, LookupError):
    """No such gate draft or decision."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _text(value: Any, name: str, *, maximum: int = 2000) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DeepInsightGateValidationError(f"{name} must be non-empty text")
    value = value.strip()
    if len(value) > maximum:
        raise DeepInsightGateValidationError(
            f"{name} must be at most {maximum} characters")
    return value


def _one_of(value: Any, allowed: Sequence[str], name: str) -> str:
    if value not in allowed:
        raise DeepInsightGateValidationError(
            f"{name} must be one of {', '.join(allowed)}; got {value!r}")
    return str(value)


def _sha256(value: Any, name: str) -> str:
    value = _text(value, name, maximum=64)
    if _HASH_RE.fullmatch(value) is None:
        raise DeepInsightGateValidationError(f"{name} must be lowercase SHA-256")
    return value


def _principal(value: Any, name: str = "actor_ref") -> str:
    value = _text(value, name, maximum=200)
    if not value.startswith(_PRINCIPALS):
        raise DeepInsightGateValidationError(f"{name} must use a principal namespace")
    return value


def _human(value: Any, name: str = "actor_ref") -> str:
    value = _text(value, name, maximum=200)
    if _HUMAN_RE.fullmatch(value) is None:
        raise DeepInsightGateValidationError(
            f"{name} must be a human: principal; the Deep Insight Gate is a "
            "human checkpoint and automation never passes it")
    return value


def company_slug(company_ref: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "-", company_ref).strip("-").lower()


def gate_ref_for(company_ref: str) -> str:
    return f"deep-insight-gate:{company_slug(company_ref)}"


# ---------------------------------------------------------------------------
# the questions, read back from the Playbook rather than copied
# ---------------------------------------------------------------------------


def gate_questions(playbook: Mapping[str, Any]) -> list[str]:
    """The twelve questions of the bound Playbook's Deep Insight Gate.

    Read rather than copied.  A gate answered under one methodology must never
    be readable as though it had been answered under another, and a Playbook
    that changed its exit gate to eleven questions is a methodology change a
    person made -- so it stops this lane rather than being quietly absorbed.
    """

    stages = (playbook or {}).get("stages") or []
    stage = next(
        (item for item in stages if item.get("stage_ref") == STAGE_REF), None)
    if stage is None:
        raise DeepInsightGateValidationError(
            "the bound playbook has no deep_insight_gate stage")
    questions = list((stage.get("exit_gate") or {}).get("questions") or [])
    if len(questions) != QUESTION_COUNT:
        raise DeepInsightGateValidationError(
            f"the bound playbook's Deep Insight Gate asks {len(questions)} "
            f"questions; this lane answers exactly {QUESTION_COUNT}")
    return [_text(item, "question", maximum=1000) for item in questions]


def questions_hash(questions: Sequence[str]) -> str:
    """The identity of one set of twelve questions, in order."""

    return content_hash([str(item) for item in questions])


def gate_pass_rule(playbook: Mapping[str, Any]) -> str:
    stages = (playbook or {}).get("stages") or []
    stage = next(
        (item for item in stages if item.get("stage_ref") == STAGE_REF), None)
    if stage is None:
        raise DeepInsightGateValidationError(
            "the bound playbook has no deep_insight_gate stage")
    return str((stage.get("exit_gate") or {}).get("pass_rule") or "")


# ---------------------------------------------------------------------------
# the record
# ---------------------------------------------------------------------------

_RECORD_FIELDS = frozenset({
    "schema_version", "id", "created_at", "gate_ref", "company_ref", "version",
    "prior_version_ref", "change_reason", "evidence_refs", "classification",
    "answers", "drafted_at", "bindings", "generator_ref", "actor_ref",
    "body_hash", "content_hash",
})
# What the chain is *about*.  Not the reason it exists, not who asked, not when.
_BODY_EXCLUDED = frozenset({
    "id", "created_at", "version", "prior_version_ref", "change_reason",
    "evidence_refs", "body_hash", "content_hash",
    # When each group was last written is not part of what the gate says about
    # the company; leaving it in the body would make a redraft that produced
    # identical prose a new version purely because the clock moved.
    "drafted_at",
})
_BINDING_FIELDS = frozenset({
    "constitution_version", "playbook_version", "mission_version_ref",
    "dossier_version_ref", "dossier_version_hash", "debate_map_version_ref",
    "policy_ref", "policy_hash", "questions_hash", "source_map_ref",
    "source_map_hash", "rubric_ref", "rubric_hash",
})
_ANSWER_FIELDS = frozenset({
    "question_ref", "question", "group", "status", "confidence", "unknown",
    "sentences", "sources", "gaps",
})
_UNKNOWN_FIELDS = frozenset({"reason", "missing", "evidence_that_would_answer"})


def normalise_ref(value: Any, name: str) -> dict[str, Any]:
    """One citable thing: its kind, its ref, the text shown, its period."""

    if not isinstance(value, Mapping):
        raise DeepInsightGateValidationError(f"{name} must be an object")
    wire = dict(value)
    if set(wire) - {"kind", "ref", "text", "period"} or not {"kind", "ref"} <= set(wire):
        raise DeepInsightGateValidationError(f"{name} has an invalid closed shape")
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
        raise DeepInsightGateValidationError(f"{name} must be exactly text and refs")
    text = _text(value["text"], f"{name}.text", maximum=MAX_SENTENCE_CHARS)
    tag = _PROSE_TAG_RE.search(text)
    if tag is not None:
        raise DeepInsightGateValidationError(
            f"{name}.text writes the citation tag {tag.group(0)!r} into the prose; "
            "tags travel in refs, and a sentence whose subject is a tag becomes "
            "a sentence with no subject once the tag is gone")
    refs = value["refs"]
    if not isinstance(refs, list) or not refs:
        raise DeepInsightGateValidationError(
            f"{name} cites nothing; a sentence you cannot cite is a sentence "
            "you may not write")
    seen: list[str] = []
    for position, ref in enumerate(refs):
        ref = _text(ref, f"{name}.refs[{position}]", maximum=512)
        if ref not in allowed_refs:
            raise DeepInsightGateValidationError(
                f"{name} cites {ref}, which was not among the material shown")
        if ref not in seen:
            seen.append(ref)
    return {"text": text, "refs": seen}


def _gaps(value: Any, name: str) -> list[str]:
    rows = value or []
    if not isinstance(rows, list) or len(rows) > MAX_GAPS:
        raise DeepInsightGateValidationError(
            f"{name} must be at most {MAX_GAPS} short strings")
    return [_text(row, f"{name}[]", maximum=MAX_GAP_CHARS) for row in rows]


def _unknown(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != _UNKNOWN_FIELDS:
        raise DeepInsightGateValidationError(
            f"{name} must be exactly reason, missing and evidence_that_would_answer")
    return {
        "reason": _one_of(value["reason"], UNKNOWN_REASONS, f"{name}.reason"),
        "missing": _text(value["missing"], f"{name}.missing", maximum=MAX_UNKNOWN_CHARS),
        # The point of the whole unknown branch.  "We do not know" is worth
        # nothing on its own; "we do not know, and this is the cheapest thing
        # that would tell us" is the twelfth question's answer for every other
        # question too.
        "evidence_that_would_answer": _text(
            value["evidence_that_would_answer"],
            f"{name}.evidence_that_would_answer", maximum=MAX_UNKNOWN_CHARS),
    }


def validate_answer(value: Any, name: str, *, question_ref: str) -> dict[str, Any]:
    """One question's answer: sentences with refs, or an honest unknown."""

    if not isinstance(value, Mapping):
        raise DeepInsightGateValidationError(f"{name} must be an object")
    wire = dict(value)
    if set(wire) != _ANSWER_FIELDS:
        raise DeepInsightGateValidationError(
            f"{name} has an invalid closed shape; "
            f"missing={sorted(_ANSWER_FIELDS - set(wire))}, "
            f"unknown={sorted(set(wire) - _ANSWER_FIELDS)}")
    if wire["question_ref"] != question_ref:
        raise DeepInsightGateValidationError(
            f"{name}.question_ref must be {question_ref!r}; the twelve questions "
            "are in the Playbook's own order")
    question = _text(wire["question"], f"{name}.question", maximum=1000)
    group = _one_of(wire["group"], GROUPS, f"{name}.group")
    if group != GROUP_OF[question_ref]:
        raise DeepInsightGateValidationError(
            f"{name}.group must be {GROUP_OF[question_ref]!r}")
    status = _one_of(wire["status"], ANSWER_STATUSES, f"{name}.status")
    gaps = _gaps(wire["gaps"], f"{name}.gaps")
    if status == "unknown":
        if wire["sentences"] or wire["sources"] or wire["confidence"] is not None:
            raise DeepInsightGateValidationError(
                f"{name} is unknown and may not also carry an answer or a confidence")
        return {
            "question_ref": question_ref, "question": question, "group": group,
            "status": status, "confidence": None,
            "unknown": _unknown(wire["unknown"], f"{name}.unknown"),
            "sentences": [], "sources": [], "gaps": gaps,
        }
    if wire["unknown"] is not None:
        raise DeepInsightGateValidationError(
            f"{name} is answered and carries an unknown block")
    confidence = _one_of(wire["confidence"], CONFIDENCE_LEVELS, f"{name}.confidence")
    sources = wire["sources"]
    if not isinstance(sources, list) or not sources:
        raise DeepInsightGateValidationError(f"{name} is answered and cites nothing")
    if len(sources) > MAX_SOURCES_PER_ANSWER:
        raise DeepInsightGateValidationError(
            f"{name} shows {len(sources)} sources; the cap is {MAX_SOURCES_PER_ANSWER}")
    checked_sources = [
        normalise_ref(row, f"{name}.sources[{index}]")
        for index, row in enumerate(sources)
    ]
    refs = {row["ref"] for row in checked_sources}
    if len(refs) != len(checked_sources):
        raise DeepInsightGateValidationError(f"{name}.sources repeats a ref")
    rows = wire["sentences"]
    if not isinstance(rows, list) or not rows:
        raise DeepInsightGateValidationError(
            f"{name} is answered and writes nothing; say unknown instead")
    if len(rows) > SENTENCES_PER_ANSWER:
        raise DeepInsightGateValidationError(
            f"{name} writes {len(rows)} sentences; the cap is {SENTENCES_PER_ANSWER}")
    sentences = [
        _sentence(row, f"{name}.sentences[{index}]", refs)
        for index, row in enumerate(rows)
    ]
    cited = {ref for row in sentences for ref in row["refs"]}
    unused = refs - cited
    if unused:
        raise DeepInsightGateValidationError(
            f"{name} lists sources no sentence cites: {sorted(unused)[:3]}")
    return {
        "question_ref": question_ref, "question": question, "group": group,
        "status": status, "confidence": confidence, "unknown": None,
        "sentences": sentences, "sources": checked_sources, "gaps": gaps,
    }


def _bindings(value: Any, name: str = "bindings") -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != _BINDING_FIELDS:
        raise DeepInsightGateValidationError(
            f"{name} has an invalid closed shape; "
            f"missing={sorted(_BINDING_FIELDS - set(value or {}))}, "
            f"unknown={sorted(set(value or {}) - _BINDING_FIELDS)}")
    wire = dict(value)
    out: dict[str, Any] = {}
    for field in ("constitution_version", "playbook_version"):
        item = wire[field]
        if not isinstance(item, Mapping) or set(item) != {"ref", "hash"}:
            raise DeepInsightGateValidationError(f"{name}.{field} must be ref and hash")
        out[field] = {
            "ref": _text(item["ref"], f"{name}.{field}.ref", maximum=512),
            "hash": _sha256(item["hash"], f"{name}.{field}.hash"),
        }
    out["mission_version_ref"] = (
        None if wire["mission_version_ref"] is None
        else _text(wire["mission_version_ref"], f"{name}.mission_version_ref",
                   maximum=512))
    # The file this gate was answered from, by hash.  A gate that cannot name
    # the dossier version behind it cannot be re-read: "what did we know when
    # we passed this company" is exactly what the chain is for.
    out["dossier_version_ref"] = _text(
        wire["dossier_version_ref"], f"{name}.dossier_version_ref", maximum=512)
    out["dossier_version_hash"] = _sha256(
        wire["dossier_version_hash"], f"{name}.dossier_version_hash")
    out["debate_map_version_ref"] = (
        None if wire["debate_map_version_ref"] is None
        else _text(wire["debate_map_version_ref"], f"{name}.debate_map_version_ref",
                   maximum=512))
    out["policy_ref"] = _text(wire["policy_ref"], f"{name}.policy_ref")
    out["policy_hash"] = _sha256(wire["policy_hash"], f"{name}.policy_hash")
    out["questions_hash"] = _sha256(wire["questions_hash"], f"{name}.questions_hash")
    out["source_map_ref"] = _text(wire["source_map_ref"], f"{name}.source_map_ref")
    out["source_map_hash"] = _sha256(wire["source_map_hash"], f"{name}.source_map_hash")
    out["rubric_ref"] = _text(wire["rubric_ref"], f"{name}.rubric_ref")
    out["rubric_hash"] = _sha256(wire["rubric_hash"], f"{name}.rubric_hash")
    return out


def _drafted_at(value: Any, name: str = "drafted_at") -> dict[str, str]:
    """When each group was last written, by group."""

    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise DeepInsightGateValidationError(f"{name} must be an object")
    out: dict[str, str] = {}
    for group, when in value.items():
        if group not in GROUPS:
            raise DeepInsightGateValidationError(
                f"{name} names {group!r}, which is not a question group")
        out[group] = _text(when, f"{name}[{group}]", maximum=64)
    return dict(sorted(out.items()))


def validate_gate_version(value: Mapping[str, Any]) -> dict[str, Any]:
    """One DeepInsightGateVersion, closed and self-consistent."""

    if not isinstance(value, Mapping):
        raise DeepInsightGateValidationError("gate version must be an object")
    wire = dict(value)
    if set(wire) != _RECORD_FIELDS or wire.get("schema_version") != SCHEMA_VERSION:
        raise DeepInsightGateValidationError(
            "deep insight gate version has an invalid closed shape; "
            f"missing={sorted(_RECORD_FIELDS - set(wire))}, "
            f"unknown={sorted(set(wire) - _RECORD_FIELDS)}")
    for field in ("id", "created_at", "gate_ref", "company_ref", "generator_ref"):
        wire[field] = _text(wire[field], field, maximum=512)
    wire["actor_ref"] = _principal(wire["actor_ref"])
    wire["change_reason"] = _one_of(wire["change_reason"], CHANGE_REASONS, "change_reason")
    if type(wire["version"]) is not int or wire["version"] < 1:
        raise DeepInsightGateValidationError("version must be a positive integer")
    if wire["prior_version_ref"] is not None:
        wire["prior_version_ref"] = _text(
            wire["prior_version_ref"], "prior_version_ref", maximum=512)
    wire["classification"] = _one_of(
        wire["classification"], INDUSTRY_CLASSIFICATIONS, "classification")
    evidence = wire["evidence_refs"]
    if not isinstance(evidence, list) or not evidence:
        raise DeepInsightGateValidationError(
            "a version must name the evidence that occasioned it")
    wire["evidence_refs"] = [
        normalise_ref(row, f"evidence_refs[{index}]")
        for index, row in enumerate(evidence)
    ]
    answers = wire["answers"]
    if not isinstance(answers, list) or len(answers) != QUESTION_COUNT:
        raise DeepInsightGateValidationError(
            f"a gate draft answers exactly {QUESTION_COUNT} questions")
    wire["answers"] = [
        validate_answer(answer, f"answers[{index}]", question_ref=QUESTION_REFS[index])
        for index, answer in enumerate(answers)
    ]
    wire["drafted_at"] = _drafted_at(wire["drafted_at"])
    wire["bindings"] = _bindings(wire["bindings"])
    if questions_hash([answer["question"] for answer in wire["answers"]]) != \
            wire["bindings"]["questions_hash"]:
        raise DeepInsightGateConflict(
            "the answers do not carry the twelve questions this draft binds")
    wire["body_hash"] = _sha256(wire["body_hash"], "body_hash")
    wire["content_hash"] = _sha256(wire["content_hash"], "content_hash")
    if body_hash(wire) != wire["body_hash"]:
        raise DeepInsightGateConflict("deep insight gate body_hash is not its body")
    base = {key: item for key, item in wire.items() if key != "content_hash"}
    if content_hash(base) != wire["content_hash"]:
        raise DeepInsightGateConflict("deep insight gate content_hash drifted")
    return wire


def body_hash(record: Mapping[str, Any]) -> str:
    """What this draft says about the company, and nothing else."""

    return content_hash({
        key: value for key, value in record.items()
        if key in _RECORD_FIELDS and key not in _BODY_EXCLUDED
    })


def evidence_scope(record: Mapping[str, Any]) -> list[str]:
    """Every ref this draft rests on, sorted.  ADR-0008 reads this."""

    refs: set[str] = set()
    for answer in record.get("answers") or []:
        refs.update(row["ref"] for row in answer.get("sources") or [])
    return sorted(refs)


def new_refs(record: Mapping[str, Any], prior: Mapping[str, Any] | None) -> list[str]:
    """The refs this draft cites that the current one did not."""

    if prior is None:
        return evidence_scope(record)
    return sorted(set(evidence_scope(record)) - set(evidence_scope(prior)))


def evidence_fingerprint(
    dossier: Mapping[str, Any] | None, map_version: Mapping[str, Any] | None
) -> str:
    """The exact state of the two files a gate draft is made from.

    The lane's idempotency key.  A gate answered from dossier version 3 and
    debate-map version 2 is the same gate however many ticks pass, so a run that
    finds this fingerprint already on the chain's head has nothing to do -- and
    it can say so before spending a model call, rather than drafting four groups
    and having ADR-0008 refuse the result.
    """

    return content_hash([
        str((dossier or {}).get("id") or "-"),
        str((dossier or {}).get("content_hash") or "-"),
        str((map_version or {}).get("id") or "-"),
    ])


def fingerprint_of(record: Mapping[str, Any]) -> str:
    """The fingerprint a stored draft was made under, read back from its bindings."""

    bindings = record.get("bindings") or {}
    return content_hash([
        str(bindings.get("dossier_version_ref") or "-"),
        str(bindings.get("dossier_version_hash") or "-"),
        str(bindings.get("debate_map_version_ref") or "-"),
    ])


def answered_count(record: Mapping[str, Any]) -> int:
    return sum(1 for answer in record.get("answers") or []
               if answer.get("status") == "answered")


def answer_body(answer: Mapping[str, Any]) -> str:
    """The prose a reader sees, assembled from the sentence rows.

    Assembled here rather than written by the model: the tags never enter the
    text, so nothing is left behind when they are stripped.
    """

    body = ""
    for row in answer.get("sentences") or ():
        if body and body[-1] not in _CJK_TERMINATORS:
            body += " "
        body += row["text"]
    return body


def classification_agrees(
    record: Mapping[str, Any], dossier: Mapping[str, Any]
) -> tuple[bool, str]:
    """Question one against the file it was drafted from.

    Not a warning.  The dossier's classification was itself drafted from a
    closed list against the same evidence and verified independently; a gate
    that reaches a different one is two models guessing, and publishing the
    disagreement would leave the reader to arbitrate between two documents that
    are supposed to be one understanding.
    """

    filed = str((dossier.get("industry_classification") or {}).get("classification") or "")
    drafted = str(record.get("classification") or "")
    if filed and drafted and filed == drafted:
        return True, ""
    return False, (
        f"question one classifies this company as {drafted or 'nothing'}, and the "
        f"dossier it was drafted from says {filed or 'nothing'}")


# ---------------------------------------------------------------------------
# the pre-publish structural standard
# ---------------------------------------------------------------------------

def _anchors(zero: str, two: str, four: str) -> Mapping[str, str]:
    """The 0 / 2 / 4 anchors of Q1's shared five-point scale.

    A three-line copy of the quality line's own helper rather than an import of
    its private one: the scale itself comes from ``Rubric.body()``, which is
    where it belongs, and a criterion that reached across a module boundary for
    a private name would break the next time that file was tidied.
    """

    return MappingProxyType({"0": zero, "2": two, "4": four})


# Q1's five-point scale and criterion shape, reused rather than re-invented, but
# deliberately *not* registered in ``research_quality_rubrics.RUBRICS``.  That
# mapping is pinned by hash and paired with a golden set of five to ten cases
# per rubric; promoting this one is a Q-line task with a golden set attached,
# and registering it without one would break the pin and the golden shape test
# for no gain here.  Everything this module needs -- the deterministic checks,
# the frozen hash a draft binds -- works on an unregistered Rubric.
DEEP_INSIGHT_GATE_RUBRIC = Rubric(
    rubric_ref="rubric:deep-insight-gate",
    version=1,
    title="Deep Insight Gate 十二问质量评分表",
    applies_to="deep_insight_gate_version",
    intent=(
        "深度认知门是 Initial Screen 与完整覆盖之间的那道人闸：十二个问题问的是「这家公司到底"
        "是怎么回事」，而不是「资料齐不齐」。它最容易犯的错误是把十二个问题都写满——一个诚实的"
        "「不知道，而且下一步花多少钱能知道」比一段读起来完整、指不回任何证据的散文更有价值。"
    ),
    criteria=(
        Criterion(
            criterion_id="twelve_questions_present",
            question="十二问是否逐条在案，缺的那条是否写明缺什么？",
            evidence_required="answers 的 question_ref 与 Playbook 出口门的十二问逐条比对",
            anchors=_anchors(
                "有问题根本没出现，读者不知道它被漏掉了还是被回避了",
                "十二问都在，但未答的那些只写了「资料不足」",
                "十二问都在；未答的写明缺什么、什么证据能答、下一步做什么",
            ),
            layer="both",
            checks=("required_sections_present",),
        ),
        Criterion(
            criterion_id="answers_traced",
            question="每一句已答的话是否都指回展示过的材料？",
            evidence_required="每条 sentence 的 refs 与该问 sources 的比对",
            anchors=_anchors(
                "有断言完全没有引用",
                "有引用，但整问只靠一条引用支撑",
                "每个断言都能指回一条 Claim、一份 filing、一条 debate 或一格模型",
            ),
            layer="both",
            checks=("claim_refs_resolve",),
        ),
        Criterion(
            criterion_id="numbers_traced",
            question="正文里的每个数字是否逐字来自它引用的那一行？",
            evidence_required="正文数字 token 与本问引用行文本的逐字比对",
            anchors=_anchors(
                "有数字没有来源",
                "数字有来源但做了换算",
                "逐字可核；没有数字的地方写明缺口",
            ),
            layer="both",
            checks=("numbers_without_refs",),
        ),
        Criterion(
            criterion_id="no_citation_scaffolding",
            question="正文里是否残留引用标记？",
            evidence_required="正文中形如 C3 / N1 / D2 的标记",
            anchors=_anchors(
                "标记被当成词写进句子，去掉标记后句子没有主语",
                "标记只出现在括号里",
                "正文完全没有标记；引用全部走 refs",
            ),
            layer="both",
            checks=("residual_citation_artefacts",),
        ),
        Criterion(
            criterion_id="new_version_new_evidence",
            question="新一版是否至少引用了上一版没有引用过的证据？",
            evidence_required="本版与上一版 refs 的差集",
            anchors=_anchors(
                "没有任何新证据，这只是重写了一遍",
                "有新引用，但没有改变任何一问的答案",
                "新证据被指名，并说明它把哪一问从「不知道」推到了「知道」",
            ),
            layer="both",
            checks=("new_version_cites_new_refs",),
        ),
        Criterion(
            criterion_id="unknowns_are_actionable",
            question="未答的问题是否给出了成本最低、信息增益最高的下一步？",
            evidence_required="unknown.evidence_that_would_answer 的具体程度",
            anchors=_anchors(
                "只写了「资料不足」",
                "写了要什么资料，但没说去哪取",
                "写明缺哪个指标、去哪个来源取、取到之后能定哪一问",
            ),
            layer="judge",
        ),
        Criterion(
            criterion_id="confidence_is_argued",
            question="每一问的信心等级是否与它引用的证据强度一致？",
            evidence_required="confidence 与该问 sources 的层级、数量、时效",
            anchors=_anchors(
                "single 一条新闻支撑的判断标为 high",
                "信心等级存在但与证据无关",
                "信心等级与证据层级一致；low 的地方说明还缺什么才能升上去",
            ),
            layer="judge",
        ),
    ),
    grading_notes=(
        "第一问的分类必须与档案一致；不一致的草稿根本到不了这里，它在发布前就被整份拒绝了。",
        "十二问里十一问是 unknown 不是失败，只要每个 unknown 都说得出下一步。",
        "深度认知门不是投资建议：它说清楚这家公司是怎么回事，买不买是别人的决定。",
    ),
)
# The two deterministic checks a draft may not fail.  Everything else the
# rubric reports is recorded and read; these two are this layer's stop-loss,
# so they are a gate rather than a score.
HARD_CHECKS: tuple[str, ...] = ("numbers_without_refs", "residual_citation_artefacts")


def gate_sections(record: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The twelve answers as one list of scoreable sections.

    Separate from :func:`gate_artefact` because the ``output_rubric`` check runs
    *before* a record has an identity: a draft is graded, refused or published,
    in that order, and a shape that needed the version id could only ever grade
    what had already been stored.
    """

    out: list[dict[str, Any]] = []
    for answer in record.get("answers") or []:
        sources = answer.get("sources") or []
        # An unanswered question is a gap in the file, and it is written where
        # every other gap is written.  Without this the structural check reads
        # each honest unknown as an empty shell -- which would make "say unknown
        # rather than guess" the one behaviour the standard punishes.
        gaps = list(answer.get("gaps") or [])
        if answer.get("status") == "unknown":
            unknown = answer.get("unknown") or {}
            gaps = [
                f"{answer['question_ref']} 未答：{unknown.get('missing', '')}",
                f"{answer['question_ref']} 下一步："
                f"{unknown.get('evidence_that_would_answer', '')}",
            ] + gaps
        out.append({
            "title": answer["question_ref"],
            "body": answer_body(answer),
            # Only claim-kind refs: Q1's ``claim_refs_resolve`` resolves refs
            # against ``claim_versions``, so a figure ref there would be
            # reported as an unresolvable Claim.  The other five kinds are
            # checked by this lane's own resolver, against the authorities that
            # own them.
            "claim_refs": [row["ref"] for row in sources if row["kind"] == "claim"],
            "numbers": [
                {
                    "text": row["text"],
                    "claim_version_ref": row["ref"] if row["kind"] == "claim" else "",
                    "period": row.get("period"),
                }
                for row in sources
            ],
            "gaps": gaps,
        })
    return out


def gate_artefact(
    record: Mapping[str, Any], *, prior: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    """The draft in Q1's scoreable artefact shape, one section per question.

    Built here rather than through ``research_quality_score.artefact`` because
    that constructor validates ``artefact_kind`` against a closed tuple this
    lane is not in, and widening that tuple belongs to the quality line.  The
    shape is the same one every check reads, and ``artefact_kind`` is carried
    honestly rather than borrowed from a neighbouring document.
    """

    sections = gate_sections(record)
    return {
        "artefact_kind": "deep_insight_gate",
        "ref": str(record["id"]),
        "hash": str(record["content_hash"]),
        "title": str(record.get("gate_ref") or ""),
        "subject_ref": record.get("company_ref"),
        "sections": sections,
        "gaps": [gap for section in sections for gap in section["gaps"]],
        "expected_sections": list(QUESTION_REFS),
        "shown_claims": [],
        "cited_tags": [],
        "confidence": None,
        "question": None,
        "prior": None if prior is None else {"sections": gate_sections(prior)},
    }


# Words that turn a file into a call.  Same list the dossier refuses: the gate
# says what is true about a company; the decision about what to do with it is a
# Thesis, admitted by a person (ADR-0001).
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

    The bindings are P12a's -- criterion hash to check name -- because the
    criteria belong to the Constitution rather than to either document, and a
    second policy file naming the same criteria under the same three checks
    would be a second answer that could drift.  A criterion the policy neither
    binds nor declares inapplicable is itself a finding: reading the criteria
    you happen to understand is not reading the standard.
    """

    from .mission_deliverable import unsourced_numbers

    bindings = {
        item["criterion_hash"]: item
        for item in validate_policy(policy)["output_rubric_bindings"]
    }
    criteria = list(((constitution.get("method") or {}).get("output_rubric") or {})
                    .get("criteria") or [])
    parts = gate_sections(record)
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
        if check == "numbers_trace_to_refs":
            for part in parts:
                for token in unsourced_numbers(part["body"], part["numbers"]):
                    findings.append({
                        "code": "number_without_source", "criterion_index": index,
                        "section": part["title"], "figure": token,
                    })
        elif check == "not_a_restatement":
            if prior is not None and not new_refs(record, prior):
                findings.append({"code": "no_new_evidence", "criterion_index": index})
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
_DECISION_FIELDS = frozenset({
    "schema_version", "id", "created_at", "gate_version_ref", "gate_version_hash",
    "company_ref", "decision", "reason", "stage_record_ref", "actor_ref",
    "content_hash",
})


def validate_decision(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise DeepInsightGateValidationError("a decision must be an object")
    wire = dict(value)
    if set(wire) != _DECISION_FIELDS or wire.get("schema_version") != SCHEMA_VERSION:
        raise DeepInsightGateValidationError(
            "deep insight gate decision has an invalid closed shape")
    for field in ("id", "created_at", "gate_version_ref", "company_ref"):
        wire[field] = _text(wire[field], field, maximum=512)
    wire["gate_version_hash"] = _sha256(wire["gate_version_hash"], "gate_version_hash")
    wire["decision"] = _one_of(wire["decision"], DECISIONS, "decision")
    wire["reason"] = _text(wire["reason"], "reason", maximum=4000)
    wire["stage_record_ref"] = (
        None if wire["stage_record_ref"] is None
        else _text(wire["stage_record_ref"], "stage_record_ref", maximum=512))
    wire["actor_ref"] = _human(wire["actor_ref"])
    wire["content_hash"] = _sha256(wire["content_hash"], "content_hash")
    base = {key: item for key, item in wire.items() if key != "content_hash"}
    if content_hash(base) != wire["content_hash"]:
        raise DeepInsightGateConflict("deep insight gate decision content_hash drifted")
    return wire


class DeepInsightGateAuthority:
    """Append-only gate drafts and the one human decision on each of them.

    It publishes what it is handed and refuses what has learned nothing; it
    never decides that a company's gate should be re-drafted.  And it never
    decides the gate: ``decide`` takes a ``human:`` actor or refuses, which is
    the Playbook's ``human_checkpoint`` flag made enforceable rather than
    documented.
    """

    def __init__(self, store: DaltonStore):
        self.store = store
        self.connection = store.connection
        self._authorized = False
        self.connection.create_function(
            "dalton_deep_insight_gate_authorized", 0, lambda: int(self._authorized)
        )
        self.connection.executescript(_SCHEMA_PATH.read_text(encoding="utf-8"))

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Cursor]:
        if self._authorized:
            raise RuntimeError("DeepInsightGateAuthority operation cannot be nested")
        self._authorized = True
        try:
            with self.store._transaction() as cur:
                yield cur
        finally:
            self._authorized = False

    # -- drafts ------------------------------------------------------------

    def publish(self, body: Mapping[str, Any]) -> dict[str, Any]:
        """Store one gate draft, or say why it is not one.

        Two ways to be a ``duplicate``, and they are different findings: the
        body is byte-for-byte what the chain already holds, or the body moved
        but every ref it rests on was already cited.  The second is the one
        ADR-0008 was written for.
        """

        body = dict(body)
        source = body.pop(SOURCE_VERSION_KEY, _UNSET)
        for field in _BODY_EXCLUDED - {"change_reason", "evidence_refs", "drafted_at"}:
            body.pop(field, None)
        company_ref = _text(body.get("company_ref"), "company_ref", maximum=512)
        gate_ref = gate_ref_for(company_ref)
        body["gate_ref"] = gate_ref
        body.setdefault("schema_version", SCHEMA_VERSION)
        body.setdefault("generator_ref", GENERATOR_REF)
        change_reason = _one_of(body.get("change_reason"), CHANGE_REASONS, "change_reason")
        if not body.get("evidence_refs"):
            raise DeepInsightGateValidationError(
                "a version must name the evidence that occasioned it")
        digest = body_hash(body)
        latest_row = self.connection.execute(
            "SELECT * FROM deep_insight_gate_versions WHERE gate_ref=? "
            "ORDER BY version_number DESC LIMIT 1", (gate_ref,),
        ).fetchone()
        latest = None if latest_row is None else self.gate(latest_row["version_id"])
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
            raise DeepInsightGateConflict(
                f"this gate is now at {head or 'no version'}, and this body was "
                f"computed from {source or 'no version'}")
        version = 1 if latest is None else int(latest["version"]) + 1
        version_id = f"deep-insight-gate-version:{company_slug(company_ref)}:{version}"
        record = {
            **body,
            "id": version_id,
            "created_at": _now(),
            "version": version,
            "prior_version_ref": head,
            "change_reason": change_reason,
            "body_hash": digest,
        }
        record["drafted_at"] = _drafted_at(record.get("drafted_at"))
        record["content_hash"] = content_hash(record)
        wire = validate_gate_version(record)
        scope_hash = content_hash(evidence_scope(wire))
        with self._transaction() as cur:
            if cur.execute(
                "SELECT 1 FROM deep_insight_gate_versions WHERE version_id=?",
                (version_id,),
            ).fetchone():
                raise DeepInsightGateConflict("gate version id already exists")
            cur.execute(
                "INSERT INTO deep_insight_gate_versions"
                "(version_id,gate_ref,version_number,prior_version_id,company_ref,"
                "change_reason,body_hash,evidence_scope_hash,record_json,content_hash,"
                "actor_ref,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    version_id, gate_ref, version, head, company_ref, change_reason,
                    digest, scope_hash, canonical_json(wire), wire["content_hash"],
                    wire["actor_ref"], wire["created_at"],
                ),
            )
        stored = self.gate(version_id)
        if stored["content_hash"] != wire["content_hash"]:
            raise DeepInsightGateConflict("gate draft did not read back as written")
        return {**stored, "status": "fresh"}

    def revise(self, body: Mapping[str, Any], **kwargs: Any) -> dict[str, Any]:
        """ADR-0008's revision entry point.  Same rules; a named door."""

        return self.publish({**dict(body), **kwargs})

    def gate(self, version_ref: str) -> dict[str, Any]:
        version_ref = _text(version_ref, "version_ref", maximum=512)
        row = self.connection.execute(
            "SELECT * FROM deep_insight_gate_versions WHERE version_id=?", (version_ref,)
        ).fetchone()
        if row is None:
            raise DeepInsightGateNotFound("deep insight gate version was not found")
        wire = validate_gate_version(json.loads(row["record_json"]))
        if (
            wire["id"] != row["version_id"]
            or wire["gate_ref"] != row["gate_ref"]
            or wire["version"] != row["version_number"]
            or wire["prior_version_ref"] != row["prior_version_id"]
            or wire["company_ref"] != row["company_ref"]
            or wire["change_reason"] != row["change_reason"]
            or wire["body_hash"] != row["body_hash"]
            or wire["content_hash"] != row["content_hash"]
            or content_hash(evidence_scope(wire)) != row["evidence_scope_hash"]
        ):
            raise DeepInsightGateConflict("deep insight gate authority drifted")
        return wire

    def latest(self, company_ref: str) -> dict[str, Any] | None:
        company_ref = _text(company_ref, "company_ref", maximum=512)
        row = self.connection.execute(
            "SELECT version_id FROM deep_insight_gate_versions WHERE company_ref=? "
            "ORDER BY version_number DESC LIMIT 1", (company_ref,),
        ).fetchone()
        return None if row is None else self.gate(row["version_id"])

    def versions(self, company_ref: str) -> list[dict[str, Any]]:
        company_ref = _text(company_ref, "company_ref", maximum=512)
        rows = self.connection.execute(
            "SELECT version_id FROM deep_insight_gate_versions WHERE company_ref=? "
            "ORDER BY version_number", (company_ref,),
        ).fetchall()
        return [self.gate(row["version_id"]) for row in rows]

    def companies(self) -> list[str]:
        return [str(row["company_ref"]) for row in self.connection.execute(
            "SELECT DISTINCT company_ref FROM deep_insight_gate_versions "
            "ORDER BY company_ref").fetchall()]

    def replay_question(self, company_ref: str, question_ref: str) -> list[dict[str, Any]]:
        """What every draft said about one question, and why it changed.

        ADR-0008's replay requirement, asked of the gate: "what did we know and
        what did we conclude when we passed this company" has to be answerable
        from the chain alone.
        """

        _one_of(question_ref, QUESTION_REFS, "question_ref")
        index = QUESTION_REFS.index(question_ref)
        out = []
        for record in self.versions(company_ref):
            answer = record["answers"][index]
            out.append({
                "version_ref": record["id"], "version": record["version"],
                "created_at": record["created_at"],
                "change_reason": record["change_reason"],
                "status": answer["status"], "confidence": answer["confidence"],
                "body": answer_body(answer),
                "unknown": answer["unknown"],
                "refs": [row["ref"] for row in answer.get("sources") or []],
            })
        return out

    # -- the decision ------------------------------------------------------

    def decide(
        self,
        *,
        gate_version_ref: str,
        gate_version_hash: str,
        decision: str,
        reason: str,
        actor_ref: str,
        stage_record_ref: str | None = None,
    ) -> dict[str, Any]:
        """One person's verdict on one exact draft.

        Bound to the draft's content hash, so a decision can never be read as
        being about a draft the owner did not see.  Idempotent by draft: a
        repeated identical decision returns ``duplicate`` rather than a second
        record, which is what makes the writer operation safe to retry after
        the stage record has already been written.
        """

        gate_version_ref = _text(gate_version_ref, "gate_version_ref", maximum=512)
        gate_version_hash = _sha256(gate_version_hash, "gate_version_hash")
        decision = _one_of(decision, DECISIONS, "decision")
        reason = _text(reason, "reason", maximum=4000)
        actor_ref = _human(actor_ref)
        if stage_record_ref is not None:
            stage_record_ref = _text(stage_record_ref, "stage_record_ref", maximum=512)
        draft = self.gate(gate_version_ref)
        if draft["content_hash"] != gate_version_hash:
            raise DeepInsightGateConflict(
                "this decision names a different draft than the one on the chain")
        existing = self.decision_for(gate_version_ref)
        if existing is not None:
            if (existing["decision"] != decision
                    or existing["actor_ref"] != actor_ref
                    or existing["reason"] != reason):
                raise DeepInsightGateConflict(
                    "this draft has already been decided; a change of mind is a "
                    "new draft, not a second verdict on the same one")
            return {**existing, "status": "duplicate"}
        created_at = _now()
        record = {
            "schema_version": SCHEMA_VERSION,
            "id": ("deep-insight-gate-decision:"
                   + content_hash({"version": gate_version_ref,
                                   "decision": decision})[:32]),
            "created_at": created_at,
            "gate_version_ref": gate_version_ref,
            "gate_version_hash": gate_version_hash,
            "company_ref": draft["company_ref"],
            "decision": decision,
            "reason": reason,
            "stage_record_ref": stage_record_ref,
            "actor_ref": actor_ref,
        }
        record["content_hash"] = content_hash(record)
        wire = validate_decision(record)
        with self._transaction() as cur:
            cur.execute(
                "INSERT INTO deep_insight_gate_decisions"
                "(decision_id,gate_version_ref,gate_version_hash,company_ref,decision,"
                "reason,stage_record_ref,record_json,content_hash,actor_ref,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    wire["id"], gate_version_ref, gate_version_hash,
                    wire["company_ref"], decision, reason, stage_record_ref,
                    canonical_json(wire), wire["content_hash"], actor_ref, created_at,
                ),
            )
        return {**wire, "status": "fresh"}

    def decision_for(self, gate_version_ref: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT record_json, content_hash FROM deep_insight_gate_decisions "
            "WHERE gate_version_ref=?",
            (_text(gate_version_ref, "gate_version_ref", maximum=512),),
        ).fetchone()
        if row is None:
            return None
        wire = validate_decision(json.loads(row["record_json"]))
        if wire["content_hash"] != row["content_hash"]:
            raise DeepInsightGateConflict("deep insight gate decision drifted")
        return wire

    def undecided(self) -> list[dict[str, Any]]:
        """Every published draft nobody has decided yet, oldest first."""

        rows = self.connection.execute(
            "SELECT v.version_id AS version_id FROM deep_insight_gate_versions v "
            "LEFT JOIN deep_insight_gate_decisions d "
            "ON d.gate_version_ref=v.version_id "
            "WHERE d.decision_id IS NULL ORDER BY v.created_at, v.version_id"
        ).fetchall()
        return [self.gate(row["version_id"]) for row in rows]

    def status_for(self, company_ref: str) -> dict[str, Any]:
        """Where one company's gate stands: the head draft and its decision."""

        head = self.latest(company_ref)
        if head is None:
            return {"company_ref": company_ref, "version_ref": None,
                    "version": 0, "decision": None, "decided": False}
        decision = self.decision_for(head["id"])
        return {
            "company_ref": company_ref,
            "version_ref": head["id"],
            "version": head["version"],
            "content_hash": head["content_hash"],
            "decision": None if decision is None else decision["decision"],
            "decided": decision is not None,
            "answered": answered_count(head),
        }

    def counts(self) -> dict[str, int]:
        drafts = self.connection.execute(
            "SELECT COUNT(*) FROM deep_insight_gate_versions").fetchone()[0]
        decisions = self.connection.execute(
            "SELECT COUNT(*) FROM deep_insight_gate_decisions").fetchone()[0]
        return {"drafts": int(drafts), "decisions": int(decisions)}


def table_exists(connection: Any) -> bool:
    return connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' "
        "AND name='deep_insight_gate_versions'"
    ).fetchone() is not None


__all__ = [
    "ANSWER_STATUSES",
    "CHANGE_REASONS",
    "CONFIDENCE_LEVELS",
    "DECISIONS",
    "DECISION_LABELS",
    "DECISION_STAGE_STATUS",
    "DEEP_INSIGHT_GATE_RUBRIC",
    "DELIVERABLE_KIND",
    "DOSSIER_SOURCES",
    "EXTRA_KINDS",
    "EXTRA_SOURCES",
    "GENERATOR_REF",
    "GROUPS",
    "GROUP_OF",
    "GROUP_QUESTIONS",
    "HARD_CHECKS",
    "MAX_GAPS",
    "MAX_SOURCES_PER_ANSWER",
    "OUTPUT_RUBRIC_CHECKS",
    "QUESTION_COUNT",
    "QUESTION_REFS",
    "QUESTION_SOURCE_MAP_HASH",
    "QUESTION_SOURCE_MAP_REF",
    "REF_KINDS",
    "SCHEMA_VERSION",
    "SENTENCES_PER_ANSWER",
    "SOURCE_VERSION_KEY",
    "STAGE_REF",
    "UNKNOWN_REASONS",
    "WRITE_SCOPE",
    "DeepInsightGateAuthority",
    "DeepInsightGateConflict",
    "DeepInsightGateError",
    "DeepInsightGateNotFound",
    "DeepInsightGateValidationError",
    "answer_body",
    "answered_count",
    "body_hash",
    "classification_agrees",
    "company_slug",
    "evidence_fingerprint",
    "evidence_scope",
    "fingerprint_of",
    "gate_artefact",
    "gate_sections",
    "gate_pass_rule",
    "gate_questions",
    "gate_ref_for",
    "new_refs",
    "normalise_ref",
    "output_rubric_findings",
    "questions_hash",
    "table_exists",
    "validate_answer",
    "validate_decision",
    "validate_gate_version",
]
