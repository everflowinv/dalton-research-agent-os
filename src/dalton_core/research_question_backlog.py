"""ResearchQuestionBacklog append-only authority.

A research question is a long-lived unit of work that must survive Agenda
cycles.  The Agenda shadow only proposes per-cycle candidates; the backlog is
the authority that keeps one stable question identity across cycles, tracks
its frozen state machine, and eventually binds formal answers.

Frozen state machine (Slice ResearchQuestionBacklog):

    open -> selected -> planned -> in_progress -> answered | blocked | retired

The machine is intentionally linear: ``blocked`` and ``retired`` are terminal
and every transition is validated in the same Core transaction that appends
the immutable event row, so illegal or out-of-order transitions fail closed
with no residue.  No recovery transitions exist in this slice; a question
that reaches a terminal state must be re-proposed as a new question (e.g.
with revised text), which the identity binding treats as a distinct question.

Authority rules implemented here:

- Question identity is deterministic: ``question_ref`` is derived from the
  canonical ``{mandate_ref, company_ref, question}`` binding.  Callers can
  never supply a question ref, version id, identity hash or content hash;
  every id/hash is recomputed from exact Core authority on write and on read.
- ``record_question`` is idempotent across cycles: an identical binding plus
  identical content returns the existing head (duplicate), divergent content
  for the same binding fails closed (conflict).  Agenda cannot re-invent the
  same question each day.
- ``select_question`` binds the question to the exact AgendaDecision and
  AgendaCycle that selected it.  The decision is re-derived from its
  canonical row, the cycle from its frozen start hash, the mandate scope
  cross-checked, and the selected candidate must canonically match the
  question content; a caller cannot link a question to an arbitrary decision.
- ``answer_question`` binds one or more exact formal ClaimVersion refs.  Each
  claim is re-read from the Core ``claim_versions`` Ledger, its hash and
  canonical record re-computed, and its closed shape re-validated (0.1 via
  the ClaimVersion contract, 0.2 via the additive Ledger validator).
  Candidate/staging claims, missing claims and tampered rows fail closed.
  An AgendaDecision is never an answer.
- This slice creates no plan, no WorkOrder DAG and grants no execution
  permission.  ``plan_question`` only advances the state machine; the
  Planner slice will bind a ResearchPlanVersion to the planned state.

The Mandate progress projection is a pure, deterministic re-derivation from
the exact backlog and mandate authority rows; it never mutates MandateVersion
authority and never becomes an alternate authority.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from .agenda import (
    read_exact_agenda_cycle,
    read_exact_mandate_version,
)
from .contracts import ClaimVersion
from .research_review import validate_claim_version_v0_2
from .store import canonical_json, content_hash


SCHEMA_VERSION = "0.1"
_SCHEMA_PATH = Path(__file__).with_name("research_question_backlog_schema.sql")
_IDENTITY_SCHEMA = "research-question-identity-v1"

QUESTION_STATES = (
    "open",
    "selected",
    "planned",
    "in_progress",
    "answered",
    "blocked",
    "retired",
)

# Exactly the frozen machine; terminal states have no outgoing transitions.
_QUESTION_TRANSITIONS = {
    None: {"open"},
    "open": {"selected"},
    "selected": {"planned"},
    "planned": {"in_progress"},
    "in_progress": {"answered", "blocked", "retired"},
    "answered": set(),
    "blocked": set(),
    "retired": set(),
}


class ResearchQuestionError(Exception):
    pass


class ResearchQuestionValidationError(ResearchQuestionError, ValueError):
    pass


class ResearchQuestionConflict(ResearchQuestionError):
    pass


class ResearchQuestionNotFound(ResearchQuestionError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ResearchQuestionValidationError(f"{name} must be a non-empty string")
    return value.strip()


def _hash_text(value: Any, name: str) -> str:
    value = _text(value, name)
    if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
        raise ResearchQuestionValidationError(f"{name} must be lowercase SHA-256")
    return value


def _timestamp(value: Any, name: str, *, nullable: bool = False) -> str | None:
    if value is None and nullable:
        return None
    value = _text(value, name)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ResearchQuestionValidationError(f"{name} must be RFC3339") from exc
    if parsed.tzinfo is None:
        raise ResearchQuestionValidationError(f"{name} must include timezone")
    return parsed.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _refs(value: Any, name: str, *, nonempty: bool = False) -> list[str]:
    if not isinstance(value, (list, tuple)):
        raise ResearchQuestionValidationError(f"{name} must be an array")
    refs = [_text(item, f"{name}[]") for item in value]
    if nonempty and not refs:
        raise ResearchQuestionValidationError(f"{name} must not be empty")
    if len(set(refs)) != len(refs):
        raise ResearchQuestionValidationError(f"{name} must contain unique values")
    return refs


def _object(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ResearchQuestionValidationError(f"{name} must be an object")
    return dict(value)


def _json_column(value: Any, name: str) -> Any:
    """Decode one canonical JSON authority column or fail closed."""

    if type(value) is not str:
        raise ResearchQuestionConflict(f"{name} is missing")
    try:
        decoded = json.loads(value)
    except (TypeError, ValueError) as exc:
        raise ResearchQuestionConflict(f"{name} is not valid JSON") from exc
    if canonical_json(decoded) != value:
        raise ResearchQuestionConflict(f"{name} is not canonical JSON")
    return decoded


def question_identity(mandate_ref: str, company_ref: str, question: str) -> dict[str, str]:
    """Canonical identity binding for one logical research question.

    The binding is the only input to the question ref, so the same question
    proposed under the same mandate/scope in a later Agenda cycle resolves to
    the same authority identity.  Identity is content/scope/mandate, never a
    caller-supplied id.
    """

    return {
        "identity_schema": _IDENTITY_SCHEMA,
        "mandate_ref": _text(mandate_ref, "mandate_ref"),
        "company_ref": _text(company_ref, "company_ref"),
        "question": _text(question, "question"),
    }


def question_ref_for(mandate_ref: str, company_ref: str, question: str) -> str:
    return "research-question:" + content_hash(
        question_identity(mandate_ref, company_ref, question)
    )[:32]


def _reverify_record(
    record_json: Any, columns: Mapping[str, Any], *, name: str
) -> dict[str, Any]:
    """Re-derive one append-only record from its own canonical bytes.

    The SQL columns are a queryable projection, never the authority.  A
    reader must detect a row whose indexed columns were edited away from the
    canonical ``record_json``, and a ``record_json`` whose bytes no longer
    hash to the stored ``content_hash``.
    """

    if type(record_json) is not str:
        raise ResearchQuestionConflict(f"{name} record_json is missing")
    try:
        wire = json.loads(record_json)
    except (TypeError, ValueError) as exc:
        raise ResearchQuestionConflict(f"{name} record_json is not valid JSON") from exc
    if type(wire) is not dict:
        raise ResearchQuestionConflict(f"{name} record_json must be an object")
    if canonical_json(wire) != record_json:
        raise ResearchQuestionConflict(f"{name} record_json is not canonical")
    body = dict(wire)
    asserted = body.pop("content_hash", None)
    if not isinstance(asserted, str) or asserted != content_hash(body):
        raise ResearchQuestionConflict(f"{name} content_hash mismatch")
    for field, value in columns.items():
        if wire.get(field) != value:
            raise ResearchQuestionConflict(f"{name} SQL column for {field} drifted")
    return wire


def _authority_row(cursor: Any, sql: str, parameters: tuple[Any, ...], name: str) -> Any:
    try:
        return cursor.execute(sql, parameters).fetchone()
    except sqlite3.OperationalError as exc:
        raise ResearchQuestionNotFound(f"{name} authority table is unavailable") from exc


def read_exact_backlog_question(cursor: Any, question_ref: str) -> dict[str, Any]:
    """Read one backlog question identity row from its canonical record."""

    question_ref = _text(question_ref, "question_ref")
    row = _authority_row(
        cursor,
        "SELECT * FROM backlog_questions WHERE question_ref=?",
        (question_ref,),
        "ResearchQuestion",
    )
    if row is None:
        raise ResearchQuestionNotFound(f"backlog question {question_ref}")
    wire = _reverify_record(
        row["record_json"],
        {
            "identity_hash": row["identity_hash"],
            "actor_ref": row["actor_ref"],
            "created_at": row["created_at"],
        },
        name="ResearchQuestion",
    )
    if wire["content_hash"] != row["content_hash"]:
        raise ResearchQuestionConflict("ResearchQuestion content_hash column drifted")
    expected_fields = {
        "schema_version", "id", "created_at", "identity", "identity_hash",
        "actor_ref", "content_hash",
    }
    if set(wire) != expected_fields or wire.get("schema_version") != SCHEMA_VERSION:
        raise ResearchQuestionConflict("ResearchQuestion has an invalid closed shape")
    try:
        identity = _object(wire["identity"], "ResearchQuestion.identity")
        _text(wire["id"], "ResearchQuestion.id")
        _hash_text(wire["identity_hash"], "ResearchQuestion.identity_hash")
        _timestamp(wire["created_at"], "ResearchQuestion.created_at")
        _text(wire["actor_ref"], "ResearchQuestion.actor_ref")
    except ResearchQuestionValidationError as exc:
        raise ResearchQuestionConflict("ResearchQuestion canonical record is invalid") from exc
    if (
        identity.get("identity_schema") != _IDENTITY_SCHEMA
        or content_hash(identity) != wire["identity_hash"]
    ):
        raise ResearchQuestionConflict("ResearchQuestion identity binding is invalid")
    expected_ref = "research-question:" + content_hash(identity)[:32]
    if wire["id"] != expected_ref or expected_ref != row["question_ref"]:
        raise ResearchQuestionConflict("ResearchQuestion ref drifted from its identity binding")
    if row["identity_json"] != canonical_json(identity):
        raise ResearchQuestionConflict("ResearchQuestion identity_json column drifted")
    return wire


def read_exact_backlog_question_version(cursor: Any, version_ref: str) -> dict[str, Any]:
    """Read one immutable question content version from its canonical record."""

    version_ref = _text(version_ref, "question_version_ref")
    row = _authority_row(
        cursor,
        "SELECT * FROM backlog_question_versions WHERE version_id=?",
        (version_ref,),
        "ResearchQuestionVersion",
    )
    if row is None:
        raise ResearchQuestionNotFound(f"backlog question version {version_ref}")
    wire = _reverify_record(
        row["record_json"],
        {
            "id": row["version_id"],
            "question_ref": row["question_ref"],
            "version": row["version_number"],
            "prior_version_ref": row["prior_version_id"],
            "mandate_ref": row["mandate_ref"],
            "company_ref": row["company_ref"],
            "question": row["question"],
            "answer_criteria": row["answer_criteria"],
            "actor_ref": row["actor_ref"],
            "created_at": row["created_at"],
        },
        name="ResearchQuestionVersion",
    )
    if wire["content_hash"] != row["content_hash"]:
        raise ResearchQuestionConflict("ResearchQuestionVersion content_hash column drifted")
    expected_fields = {
        "schema_version", "id", "created_at", "question_ref", "version",
        "prior_version_ref", "mandate_ref", "company_ref", "question",
        "answer_criteria", "source_refs", "actor_ref", "content_hash",
    }
    if set(wire) != expected_fields or wire.get("schema_version") != SCHEMA_VERSION:
        raise ResearchQuestionConflict("ResearchQuestionVersion has an invalid closed shape")
    try:
        _text(wire["id"], "ResearchQuestionVersion.id")
        _text(wire["question_ref"], "ResearchQuestionVersion.question_ref")
        _timestamp(wire["created_at"], "ResearchQuestionVersion.created_at")
        _text(wire["mandate_ref"], "ResearchQuestionVersion.mandate_ref")
        _text(wire["company_ref"], "ResearchQuestionVersion.company_ref")
        _text(wire["question"], "ResearchQuestionVersion.question")
        _text(wire["answer_criteria"], "ResearchQuestionVersion.answer_criteria")
        _refs(wire["source_refs"], "ResearchQuestionVersion.source_refs")
        _text(wire["actor_ref"], "ResearchQuestionVersion.actor_ref")
        if wire["prior_version_ref"] is not None:
            _text(wire["prior_version_ref"], "ResearchQuestionVersion.prior_version_ref")
    except ResearchQuestionValidationError as exc:
        raise ResearchQuestionConflict(
            "ResearchQuestionVersion canonical record is invalid"
        ) from exc
    if (
        isinstance(wire["version"], bool)
        or not isinstance(wire["version"], int)
        or wire["version"] < 1
    ):
        raise ResearchQuestionConflict("ResearchQuestionVersion version is invalid")
    if row["source_refs_json"] != canonical_json(wire["source_refs"]):
        raise ResearchQuestionConflict("ResearchQuestionVersion source_refs column drifted")
    identity = read_exact_backlog_question(cursor, wire["question_ref"])["identity"]
    if (
        identity.get("mandate_ref") != wire["mandate_ref"]
        or identity.get("company_ref") != wire["company_ref"]
        or identity.get("question") != wire["question"]
    ):
        raise ResearchQuestionConflict(
            "ResearchQuestionVersion drifted from its question identity binding"
        )
    expected_ref = "research-question-version:" + content_hash({
        "question_ref": wire["question_ref"], "version": wire["version"],
    })[:32]
    if wire["id"] != expected_ref:
        raise ResearchQuestionConflict(
            "ResearchQuestionVersion ref drifted from its version binding"
        )
    if wire["version"] == 1:
        if wire["prior_version_ref"] is not None:
            raise ResearchQuestionConflict(
                "ResearchQuestionVersion v1 cannot have a prior version"
            )
    else:
        if wire["prior_version_ref"] is None:
            raise ResearchQuestionConflict(
                "ResearchQuestionVersion revision is missing its prior version"
            )
        prior = read_exact_backlog_question_version(cursor, wire["prior_version_ref"])
        if (
            prior["question_ref"] != wire["question_ref"]
            or prior["version"] != wire["version"] - 1
        ):
            raise ResearchQuestionConflict(
                "ResearchQuestionVersion prior-version chain is invalid"
            )
    return wire


def read_exact_backlog_event(cursor: Any, event_id: str) -> dict[str, Any]:
    """Read one immutable state event from its canonical row.

    Following the ``agenda_cycle_events`` convention, the event row carries
    no ``record_json``; the canonical wire is rebuilt from the columns and
    its content hash re-checked against the stored hash.
    """

    event_id = _text(event_id, "event_id")
    row = _authority_row(
        cursor,
        "SELECT * FROM backlog_question_events WHERE event_id=?",
        (event_id,),
        "ResearchQuestionEvent",
    )
    if row is None:
        raise ResearchQuestionNotFound(f"backlog question event {event_id}")
    try:
        metadata = json.loads(row["metadata_json"])
    except (TypeError, ValueError) as exc:
        raise ResearchQuestionConflict(
            "ResearchQuestionEvent metadata_json is not valid JSON"
        ) from exc
    wire = {
        "schema_version": SCHEMA_VERSION,
        "id": row["event_id"],
        "question_ref": row["question_ref"],
        "state": row["state"],
        "reason": row["reason"],
        "metadata": metadata,
        "actor_ref": row["actor_ref"],
        "created_at": row["created_at"],
    }
    if content_hash(wire) != row["content_hash"]:
        raise ResearchQuestionConflict("ResearchQuestionEvent columns drifted from their content hash")
    wire["content_hash"] = row["content_hash"]
    if row["metadata_json"] != canonical_json(metadata):
        raise ResearchQuestionConflict("ResearchQuestionEvent metadata_json is not canonical")
    expected_fields = {
        "schema_version", "id", "created_at", "question_ref", "state",
        "reason", "metadata", "actor_ref", "content_hash",
    }
    if set(wire) != expected_fields or wire.get("schema_version") != SCHEMA_VERSION:
        raise ResearchQuestionConflict("ResearchQuestionEvent has an invalid closed shape")
    try:
        _text(wire["id"], "ResearchQuestionEvent.id")
        _text(wire["question_ref"], "ResearchQuestionEvent.question_ref")
        _timestamp(wire["created_at"], "ResearchQuestionEvent.created_at")
        _text(wire["reason"], "ResearchQuestionEvent.reason")
        _object(wire["metadata"], "ResearchQuestionEvent.metadata")
        _text(wire["actor_ref"], "ResearchQuestionEvent.actor_ref")
    except ResearchQuestionValidationError as exc:
        raise ResearchQuestionConflict("ResearchQuestionEvent canonical record is invalid") from exc
    if wire["state"] not in QUESTION_STATES:
        raise ResearchQuestionConflict("ResearchQuestionEvent state is invalid")
    return wire


def _read_exact_selection_link(cursor: Any, link_id: str) -> dict[str, Any]:
    row = _authority_row(
        cursor,
        "SELECT * FROM backlog_selection_links WHERE link_id=?",
        (link_id,),
        "ResearchQuestionSelectionLink",
    )
    if row is None:
        raise ResearchQuestionNotFound(f"backlog selection link {link_id}")
    wire = {
        "schema_version": SCHEMA_VERSION,
        "id": row["link_id"],
        "question_ref": row["question_ref"],
        "decision_ref": row["decision_ref"],
        "decision_hash": row["decision_hash"],
        "cycle_ref": row["cycle_ref"],
        "cycle_hash": row["cycle_hash"],
        "candidate_ref": row["candidate_ref"],
        "event_ref": row["event_ref"],
        "actor_ref": row["actor_ref"],
        "created_at": row["created_at"],
    }
    if content_hash(wire) != row["content_hash"]:
        raise ResearchQuestionConflict(
            "ResearchQuestionSelectionLink columns drifted from their content hash"
        )
    wire["content_hash"] = row["content_hash"]
    expected_fields = {
        "schema_version", "id", "created_at", "question_ref", "decision_ref",
        "decision_hash", "cycle_ref", "cycle_hash", "candidate_ref",
        "event_ref", "actor_ref", "content_hash",
    }
    if set(wire) != expected_fields or wire.get("schema_version") != SCHEMA_VERSION:
        raise ResearchQuestionConflict(
            "ResearchQuestionSelectionLink has an invalid closed shape"
        )
    try:
        _text(wire["id"], "ResearchQuestionSelectionLink.id")
        _text(wire["question_ref"], "ResearchQuestionSelectionLink.question_ref")
        _text(wire["decision_ref"], "ResearchQuestionSelectionLink.decision_ref")
        _hash_text(wire["decision_hash"], "ResearchQuestionSelectionLink.decision_hash")
        _text(wire["cycle_ref"], "ResearchQuestionSelectionLink.cycle_ref")
        _hash_text(wire["cycle_hash"], "ResearchQuestionSelectionLink.cycle_hash")
        _text(wire["candidate_ref"], "ResearchQuestionSelectionLink.candidate_ref")
        _text(wire["event_ref"], "ResearchQuestionSelectionLink.event_ref")
        _text(wire["actor_ref"], "ResearchQuestionSelectionLink.actor_ref")
        _timestamp(wire["created_at"], "ResearchQuestionSelectionLink.created_at")
    except ResearchQuestionValidationError as exc:
        raise ResearchQuestionConflict(
            "ResearchQuestionSelectionLink canonical record is invalid"
        ) from exc
    event = read_exact_backlog_event(cursor, wire["event_ref"])
    if (
        event["question_ref"] != wire["question_ref"]
        or event["state"] != "selected"
        or event["metadata"] != {
            "decision_ref": wire["decision_ref"],
            "decision_hash": wire["decision_hash"],
            "cycle_ref": wire["cycle_ref"],
            "cycle_hash": wire["cycle_hash"],
            "candidate_ref": wire["candidate_ref"],
        }
    ):
        raise ResearchQuestionConflict(
            "ResearchQuestionSelectionLink drifted from its selected event"
        )
    decision = _read_exact_agenda_decision(cursor, wire["decision_ref"])
    cycle = read_exact_agenda_cycle(cursor, wire["cycle_ref"])
    candidate = _read_exact_agenda_candidate(cursor, wire["candidate_ref"])
    pointer = cursor.execute(
        "SELECT version_id FROM backlog_question_pointer WHERE question_ref=?",
        (wire["question_ref"],),
    ).fetchone()
    if pointer is None:
        raise ResearchQuestionConflict(
            "ResearchQuestionSelectionLink question has no head version"
        )
    head = read_exact_backlog_question_version(cursor, pointer["version_id"])
    if (
        decision["content_hash"] != wire["decision_hash"]
        or decision["cycle_ref"] != wire["cycle_ref"]
        or wire["candidate_ref"] not in decision["selected_candidate_refs"]
        or decision["policy_version_ref"] != cycle["policy_version_ref"]
        or cycle["content_hash"] != wire["cycle_hash"]
        or candidate["cycle_ref"] != wire["cycle_ref"]
        or head["question_ref"] != wire["question_ref"]
        or candidate["proposed_question"] != head["question"]
        or candidate["answer_criteria"] != head["answer_criteria"]
        or candidate["source_refs"] != head["source_refs"]
        or not candidate["valid"]
    ):
        raise ResearchQuestionConflict(
            "ResearchQuestionSelectionLink referenced authority drifted"
        )
    return wire


def _read_exact_answer_binding(cursor: Any, binding_id: str) -> dict[str, Any]:
    row = _authority_row(
        cursor,
        "SELECT * FROM backlog_answer_bindings WHERE binding_id=?",
        (binding_id,),
        "ResearchQuestionAnswerBinding",
    )
    if row is None:
        raise ResearchQuestionNotFound(f"backlog answer binding {binding_id}")
    wire = {
        "schema_version": SCHEMA_VERSION,
        "id": row["binding_id"],
        "question_ref": row["question_ref"],
        "claim_version_ref": row["claim_version_ref"],
        "claim_version_hash": row["claim_version_hash"],
        "claim_ref": row["claim_ref"],
        "event_ref": row["event_ref"],
        "actor_ref": row["actor_ref"],
        "created_at": row["created_at"],
    }
    if content_hash(wire) != row["content_hash"]:
        raise ResearchQuestionConflict(
            "ResearchQuestionAnswerBinding columns drifted from their content hash"
        )
    wire["content_hash"] = row["content_hash"]
    expected_fields = {
        "schema_version", "id", "created_at", "question_ref",
        "claim_version_ref", "claim_version_hash", "claim_ref", "event_ref",
        "actor_ref", "content_hash",
    }
    if set(wire) != expected_fields or wire.get("schema_version") != SCHEMA_VERSION:
        raise ResearchQuestionConflict(
            "ResearchQuestionAnswerBinding has an invalid closed shape"
        )
    try:
        _text(wire["id"], "ResearchQuestionAnswerBinding.id")
        _text(wire["question_ref"], "ResearchQuestionAnswerBinding.question_ref")
        _text(wire["claim_version_ref"], "ResearchQuestionAnswerBinding.claim_version_ref")
        _hash_text(
            wire["claim_version_hash"], "ResearchQuestionAnswerBinding.claim_version_hash"
        )
        _text(wire["claim_ref"], "ResearchQuestionAnswerBinding.claim_ref")
        _text(wire["event_ref"], "ResearchQuestionAnswerBinding.event_ref")
        _text(wire["actor_ref"], "ResearchQuestionAnswerBinding.actor_ref")
        _timestamp(wire["created_at"], "ResearchQuestionAnswerBinding.created_at")
    except ResearchQuestionValidationError as exc:
        raise ResearchQuestionConflict(
            "ResearchQuestionAnswerBinding canonical record is invalid"
        ) from exc
    event = read_exact_backlog_event(cursor, wire["event_ref"])
    claim = _read_exact_claim_version(cursor, wire["claim_version_ref"])
    refs = event["metadata"].get("claim_version_refs")
    hashes = event["metadata"].get("claim_version_hashes")
    if (
        event["question_ref"] != wire["question_ref"]
        or event["state"] != "answered"
        or not isinstance(refs, list)
        or not isinstance(hashes, list)
        or len(refs) != len(hashes)
        or wire["claim_version_ref"] not in refs
        or hashes[refs.index(wire["claim_version_ref"])] != wire["claim_version_hash"]
        or claim != {
            "claim_version_ref": wire["claim_version_ref"],
            "claim_version_hash": wire["claim_version_hash"],
            "claim_ref": wire["claim_ref"],
        }
    ):
        raise ResearchQuestionConflict(
            "ResearchQuestionAnswerBinding referenced authority drifted"
        )
    return wire


def _read_exact_agenda_decision(cursor: Any, decision_ref: str) -> dict[str, Any]:
    """Re-derive one AgendaDecision from its canonical row.

    ``agenda_decisions`` has no ``record_json``; the row columns are the
    whole record, so the reader rebuilds the canonical wire and re-checks the
    stored content hash.  A caller cannot assert a decision identity that
    Core does not carry.
    """

    decision_ref = _text(decision_ref, "decision_ref")
    row = _authority_row(
        cursor,
        "SELECT * FROM agenda_decisions WHERE decision_id=?",
        (decision_ref,),
        "AgendaDecision",
    )
    if row is None:
        raise ResearchQuestionNotFound(f"agenda decision {decision_ref}")
    wire = {
        "schema_version": "0.1",
        "id": row["decision_id"],
        "cycle_ref": row["cycle_id"],
        "selected_candidate_refs": _json_column(
            row["selected_candidate_refs_json"],
            "AgendaDecision.selected_candidate_refs_json",
        ),
        "deferred_candidate_refs": _json_column(
            row["deferred_candidate_refs_json"],
            "AgendaDecision.deferred_candidate_refs_json",
        ),
        "rejected_candidate_refs": _json_column(
            row["rejected_candidate_refs_json"],
            "AgendaDecision.rejected_candidate_refs_json",
        ),
        "score_breakdown": _json_column(
            row["score_breakdown_json"], "AgendaDecision.score_breakdown_json"
        ),
        "policy_version_ref": row["policy_version_ref"],
        "actor_ref": row["actor_ref"],
        "created_at": row["created_at"],
    }
    if content_hash(wire) != row["content_hash"]:
        raise ResearchQuestionConflict("AgendaDecision columns drifted from their content hash")
    try:
        _refs(wire["selected_candidate_refs"], "AgendaDecision.selected_candidate_refs")
        _refs(wire["deferred_candidate_refs"], "AgendaDecision.deferred_candidate_refs")
        _refs(wire["rejected_candidate_refs"], "AgendaDecision.rejected_candidate_refs")
        _object(wire["score_breakdown"], "AgendaDecision.score_breakdown")
    except ResearchQuestionValidationError as exc:
        raise ResearchQuestionConflict("AgendaDecision canonical record is invalid") from exc
    return {**wire, "content_hash": row["content_hash"]}


def _read_exact_agenda_candidate(cursor: Any, candidate_ref: str) -> dict[str, Any]:
    """Re-derive one agenda candidate from its canonical row."""

    candidate_ref = _text(candidate_ref, "candidate_ref")
    row = _authority_row(
        cursor,
        "SELECT * FROM agenda_candidates WHERE candidate_id=?",
        (candidate_ref,),
        "AgendaCandidate",
    )
    if row is None:
        raise ResearchQuestionNotFound(f"agenda candidate {candidate_ref}")
    wire = {
        "schema_version": "0.1",
        "id": row["candidate_id"],
        "cycle_ref": row["cycle_id"],
        "question_version_ref": row["question_version_ref"],
        "proposed_question": row["proposed_question"],
        "answer_criteria": row["answer_criteria"],
        "features": _json_column(
            row["features_json"], "AgendaCandidate.features_json"
        ),
        "rationale": row["rationale"],
        "source_refs": _json_column(
            row["source_refs_json"], "AgendaCandidate.source_refs_json"
        ),
        "valid": bool(row["valid"]),
        "rejection_reason": row["rejection_reason"],
        "actor_ref": row["actor_ref"],
        "created_at": row["created_at"],
    }
    if content_hash(wire) != row["content_hash"]:
        raise ResearchQuestionConflict("AgendaCandidate columns drifted from their content hash")
    try:
        _object(wire["features"], "AgendaCandidate.features")
        _refs(wire["source_refs"], "AgendaCandidate.source_refs")
    except ResearchQuestionValidationError as exc:
        raise ResearchQuestionConflict("AgendaCandidate canonical record is invalid") from exc
    return wire


def _read_exact_claim_version(cursor: Any, claim_version_ref: str) -> dict[str, Any]:
    """Re-read one formal ClaimVersion from the Core Ledger.

    Only rows in the formal ``claim_versions`` table can bind an answer.
    Candidate/staging claims live in separate candidate authorities and are
    rejected as missing; ``candidate-claim:`` refs are rejected explicitly.
    The record is re-derived from its canonical bytes, its hash recomputed,
    its SQL columns re-checked and its closed shape re-validated (0.1 via the
    ClaimVersion contract, 0.2 via the additive Ledger validator).
    """

    claim_version_ref = _text(claim_version_ref, "claim_version_ref")
    if claim_version_ref.startswith("candidate-claim:"):
        raise ResearchQuestionConflict(
            "candidate claims cannot bind a backlog answer; only formal ClaimVersions"
        )
    row = _authority_row(
        cursor,
        "SELECT * FROM claim_versions WHERE claim_version_id=?",
        (claim_version_ref,),
        "ClaimVersion",
    )
    if row is None:
        raise ResearchQuestionNotFound(
            f"formal ClaimVersion {claim_version_ref} is missing from the Ledger"
        )
    try:
        wire = json.loads(row["claim_json"])
    except (TypeError, ValueError) as exc:
        raise ResearchQuestionConflict("ClaimVersion claim_json is not valid JSON") from exc
    if type(wire) is not dict or wire.get("id") != claim_version_ref:
        raise ResearchQuestionConflict("ClaimVersion identity binding failed")
    if (
        row["claim_ref"] != wire.get("claim_ref")
        or row["version_number"] != wire.get("version")
        or row["prior_version_id"] != wire.get("prior_version_ref")
        or row["created_at"] != wire.get("created_at")
        or row["content_hash"] != wire.get("content_hash")
        or row["claim_json"] != canonical_json(wire)
    ):
        raise ResearchQuestionConflict("ClaimVersion SQL columns drifted")
    body = dict(wire)
    asserted = body.pop("content_hash", None)
    if not isinstance(asserted, str) or asserted != content_hash(body):
        raise ResearchQuestionConflict("ClaimVersion content_hash mismatch")
    if wire["content_hash"] != row["content_hash"]:
        raise ResearchQuestionConflict("ClaimVersion content_hash column drifted")
    if wire.get("schema_version") == "0.2":
        try:
            validate_claim_version_v0_2(wire)
        except Exception as exc:
            raise ResearchQuestionConflict("ClaimVersion 0.2 canonical record is invalid") from exc
    elif wire.get("schema_version") == "0.1":
        try:
            ClaimVersion.from_dict(wire)
        except Exception as exc:
            raise ResearchQuestionConflict("ClaimVersion 0.1 canonical record is invalid") from exc
    else:
        raise ResearchQuestionConflict(
            f"ClaimVersion has unsupported schema_version {wire.get('schema_version')!r}"
        )
    return {
        "claim_version_ref": claim_version_ref,
        "claim_version_hash": wire["content_hash"],
        "claim_ref": wire["claim_ref"],
    }


def _read_exact_event_history(
    cursor: Any,
    question_ref: str,
    *,
    revalidate_plan: bool = True,
) -> list[dict[str, Any]]:
    """Replay and validate the complete state-machine history."""

    rows = cursor.execute(
        "SELECT event_id FROM backlog_question_events WHERE question_ref=? "
        "ORDER BY event_seq",
        (question_ref,),
    ).fetchall()
    events: list[dict[str, Any]] = []
    prior: str | None = None
    for row in rows:
        event = read_exact_backlog_event(cursor, row["event_id"])
        if event["question_ref"] != question_ref:
            raise ResearchQuestionConflict(
                "ResearchQuestionEvent drifted from its question history"
            )
        if event["state"] not in _QUESTION_TRANSITIONS.get(prior, set()):
            raise ResearchQuestionConflict(
                f"backlog question history transition {prior!r} -> "
                f"{event['state']!r} is invalid"
            )
        events.append(event)
        prior = event["state"]
    if revalidate_plan:
        planned = next((event for event in events if event["state"] == "planned"), None)
        if planned is not None:
            metadata = planned["metadata"]
            if set(metadata) != {"plan_version_ref", "plan_version_hash"}:
                raise ResearchQuestionConflict(
                    "planned question event lacks its exact plan binding"
                )
            from .research_plan import (
                _revalidate_plan_start_binding,
                revalidate_plan_binds_question,
            )

            plan = revalidate_plan_binds_question(
                cursor,
                metadata["plan_version_ref"],
                question_ref,
            )
            if plan["content_hash"] != metadata["plan_version_hash"]:
                raise ResearchQuestionConflict(
                    "planned question event plan hash drifted"
                )
            in_progress = next(
                (event for event in events if event["state"] == "in_progress"),
                None,
            )
            if in_progress is not None:
                expected_keys = {
                    "plan_version_ref", "plan_version_hash",
                    "workflow_version_ref", "workflow_version_hash",
                    "root_work_order_ref", "root_work_order_hash",
                }
                if set(in_progress["metadata"]) != expected_keys:
                    raise ResearchQuestionConflict(
                        "in_progress question event lacks its exact start binding"
                    )
                if any(
                    in_progress["metadata"][key] != metadata[key]
                    for key in ("plan_version_ref", "plan_version_hash")
                ):
                    raise ResearchQuestionConflict(
                        "in_progress question event changed its planned plan"
                    )
                _revalidate_plan_start_binding(
                    cursor,
                    **in_progress["metadata"],
                )
    return events


def _append_event_row(
    cursor: Any,
    *,
    question_ref: str,
    state: str,
    reason: str,
    metadata: Mapping[str, Any],
    actor_ref: str,
) -> dict[str, Any]:
    """Append one validated backlog event inside an existing Core transaction.

    Planner is a peer Core authority and must bind plan creation/start to the
    question transition atomically.  This narrow helper exposes only that
    append operation; it does not expose the database path or create a second
    writer boundary.
    """

    events = _read_exact_event_history(cursor, question_ref)
    prior = events[-1]["state"] if events else None
    if state not in _QUESTION_TRANSITIONS.get(prior, set()):
        raise ResearchQuestionConflict(
            f"backlog question transition {prior!r} -> {state!r} is invalid"
        )
    created_at = _now()
    wire = {
        "schema_version": SCHEMA_VERSION,
        "id": f"research-question-event:{uuid.uuid4().hex}",
        "question_ref": _text(question_ref, "question_ref"),
        "state": state,
        "reason": _text(reason, "reason"),
        "metadata": dict(metadata),
        "actor_ref": _text(actor_ref, "actor_ref"),
        "created_at": created_at,
    }
    wire["content_hash"] = content_hash(wire)
    cursor.execute(
        "INSERT INTO backlog_question_events(event_id,question_ref,state,reason,"
        "metadata_json,actor_ref,created_at,content_hash) VALUES(?,?,?,?,?,?,?,?)",
        (
            wire["id"], question_ref, state, wire["reason"],
            canonical_json(wire["metadata"]), wire["actor_ref"], created_at,
            wire["content_hash"],
        ),
    )
    return wire


class ResearchQuestionBacklog:
    """Backlog authority layered on a ``DaltonStore`` transaction boundary."""

    def __init__(self, store: Any):
        if not hasattr(store, "connection") or not hasattr(store, "_transaction"):
            raise TypeError("store must be a DaltonStore-like authority")
        self.store = store
        self.connection: sqlite3.Connection = store.connection
        self.connection.executescript(_SCHEMA_PATH.read_text(encoding="utf-8"))

    @staticmethod
    def _record(base: Mapping[str, Any]) -> dict[str, Any]:
        wire = dict(base)
        wire["content_hash"] = content_hash(wire)
        return wire

    def _idem(
        self, cur: sqlite3.Cursor, key: str | None, operation: str, request_hash: str
    ) -> dict[str, Any] | None:
        if key is None:
            return None
        key = _text(key, "idempotency_key")
        row = cur.execute(
            "SELECT operation,request_hash,result_json FROM backlog_idempotency "
            "WHERE idempotency_key=?",
            (key,),
        ).fetchone()
        if row is None:
            return None
        if row["operation"] == operation and row["request_hash"] == request_hash:
            result = json.loads(row["result_json"])
            result["status"] = "duplicate"
            return result
        return {"status": "conflict", "idempotency_key": key}

    @staticmethod
    def _save_idem(
        cur: sqlite3.Cursor,
        key: str | None,
        operation: str,
        request_hash: str,
        result: Mapping[str, Any],
    ) -> None:
        if key is None:
            return
        key = _text(key, "idempotency_key")
        cur.execute(
            "INSERT INTO backlog_idempotency(idempotency_key,operation,request_hash,"
            "result_json,created_at) VALUES(?,?,?,?,?)",
            (key, operation, request_hash, canonical_json(result), _now()),
        )

    def _latest_state(self, cur: sqlite3.Cursor, question_ref: str) -> str | None:
        events = _read_exact_event_history(cur, question_ref)
        return None if not events else events[-1]["state"]

    def _event(
        self,
        cur: sqlite3.Cursor,
        question_ref: str,
        state: str,
        reason: str,
        metadata: Mapping[str, Any],
        actor_ref: str,
    ) -> dict[str, Any]:
        return _append_event_row(
            cur,
            question_ref=question_ref,
            state=state,
            reason=reason,
            metadata=metadata,
            actor_ref=actor_ref,
        )

    def record_question(
        self,
        *,
        mandate_version_ref: str,
        company_ref: str,
        question: str,
        answer_criteria: str,
        source_refs: Sequence[str],
        actor_ref: str,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Record one question under an exact MandateVersion.

        The question identity is derived from the canonical binding, never
        from a caller id/hash.  An identical question recorded again (the
        same day or a later cycle) resolves to the existing head; divergent
        content for the same identity fails closed.
        """

        mandate_version_ref = _text(mandate_version_ref, "mandate_version_ref")
        company_ref = _text(company_ref, "company_ref")
        question = _text(question, "question")
        answer_criteria = _text(answer_criteria, "answer_criteria")
        source_refs = _refs(source_refs, "source_refs")
        actor_ref = _text(actor_ref, "actor_ref")
        request = {
            "mandate_version_ref": mandate_version_ref,
            "company_ref": company_ref,
            "question": question,
            "answer_criteria": answer_criteria,
            "source_refs": source_refs,
            "actor_ref": actor_ref,
        }
        request_hash = content_hash(request)
        with self.store._transaction() as cur:
            duplicate = self._idem(cur, idempotency_key, "record_backlog_question", request_hash)
            if duplicate is not None:
                return duplicate
            mandate = read_exact_mandate_version(cur, mandate_version_ref)
            if company_ref not in mandate["scope_refs"]:
                raise ResearchQuestionConflict(
                    "company_ref is outside the exact MandateVersion scope"
                )
            question_ref = question_ref_for(
                mandate["mandate_ref"], company_ref, question
            )
            existing = cur.execute(
                "SELECT * FROM backlog_questions WHERE question_ref=?",
                (question_ref,),
            ).fetchone()
            created_at = _now()
            if existing is not None:
                read_exact_backlog_question(cur, question_ref)
                head = cur.execute(
                    "SELECT v.* FROM backlog_question_pointer p "
                    "JOIN backlog_question_versions v ON v.version_id=p.version_id "
                    "WHERE p.question_ref=?",
                    (question_ref,),
                ).fetchone()
                if head is None:
                    raise ResearchQuestionConflict(
                        "backlog question has no head version"
                    )
                head_wire = read_exact_backlog_question_version(cur, head["version_id"])
                incoming_content = {
                    "question": question,
                    "answer_criteria": answer_criteria,
                    "source_refs": source_refs,
                }
                head_content = {
                    "question": head_wire["question"],
                    "answer_criteria": head_wire["answer_criteria"],
                    "source_refs": head_wire["source_refs"],
                }
                if canonical_json(incoming_content) != canonical_json(head_content):
                    raise ResearchQuestionConflict(
                        "question identity is already bound to different content"
                    )
                return {
                    "status": "duplicate",
                    "question_ref": question_ref,
                    "question_version_ref": head_wire["id"],
                    "state": self._latest_state(cur, question_ref),
                }
            version_ref = "research-question-version:" + content_hash({
                "question_ref": question_ref, "version": 1,
            })[:32]
            identity = question_identity(mandate["mandate_ref"], company_ref, question)
            identity_hash = content_hash(identity)
            question_wire = self._record({
                "schema_version": SCHEMA_VERSION,
                "id": question_ref,
                "identity": identity,
                "identity_hash": identity_hash,
                "actor_ref": actor_ref,
                "created_at": created_at,
            })
            cur.execute(
                "INSERT INTO backlog_questions(question_ref,identity_json,identity_hash,"
                "actor_ref,record_json,content_hash,created_at) VALUES(?,?,?,?,?,?,?)",
                (
                    question_ref, canonical_json(identity), identity_hash, actor_ref,
                    canonical_json(question_wire), question_wire["content_hash"],
                    created_at,
                ),
            )
            version_wire = self._record({
                "schema_version": SCHEMA_VERSION,
                "id": version_ref,
                "question_ref": question_ref,
                "version": 1,
                "prior_version_ref": None,
                "mandate_ref": mandate["mandate_ref"],
                "company_ref": company_ref,
                "question": question,
                "answer_criteria": answer_criteria,
                "source_refs": list(source_refs),
                "actor_ref": actor_ref,
                "created_at": created_at,
            })
            cur.execute(
                "INSERT INTO backlog_question_versions(version_id,question_ref,"
                "version_number,prior_version_id,mandate_ref,company_ref,question,"
                "answer_criteria,source_refs_json,actor_ref,record_json,content_hash,"
                "created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    version_ref, question_ref, 1, None, mandate["mandate_ref"],
                    company_ref, question, answer_criteria,
                    canonical_json(list(source_refs)), actor_ref,
                    canonical_json(version_wire), version_wire["content_hash"],
                    created_at,
                ),
            )
            cur.execute(
                "INSERT INTO backlog_question_pointer(question_ref,version_id) "
                "VALUES(?,?)",
                (question_ref, version_ref),
            )
            event = self._event(
                cur, question_ref, "open", "recorded",
                {"mandate_version_ref": mandate_version_ref}, actor_ref,
            )
            result = {
                "status": "fresh",
                "question_ref": question_ref,
                "question_version_ref": version_ref,
                "state": "open",
                "event": event,
            }
            self._save_idem(
                cur, idempotency_key, "record_backlog_question", request_hash, result
            )
            return result

    def _head(self, cur: sqlite3.Cursor, question_ref: str) -> dict[str, Any]:
        read_exact_backlog_question(cur, question_ref)
        row = cur.execute(
            "SELECT v.* FROM backlog_question_pointer p "
            "JOIN backlog_question_versions v ON v.version_id=p.version_id "
            "WHERE p.question_ref=?",
            (question_ref,),
        ).fetchone()
        if row is None:
            raise ResearchQuestionConflict("backlog question has no head version")
        head = read_exact_backlog_question_version(cur, row["version_id"])
        if head["question_ref"] != question_ref:
            raise ResearchQuestionConflict(
                "backlog question pointer resolved a different question"
            )
        return head

    def select_question(
        self,
        *,
        question_ref: str,
        decision_ref: str,
        actor_ref: str,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Bind an open question to the exact AgendaDecision that selected it.

        The decision, its cycle and the cycle's frozen mandate are re-read
        from Core authority; the question must be in the same mandate scope
        and the decision must have selected a candidate that canonically
        matches the question content.  A caller cannot link a question to an
        arbitrary or cross-mandate decision.
        """

        question_ref = _text(question_ref, "question_ref")
        decision_ref = _text(decision_ref, "decision_ref")
        actor_ref = _text(actor_ref, "actor_ref")
        request = {
            "question_ref": question_ref,
            "decision_ref": decision_ref,
            "actor_ref": actor_ref,
        }
        request_hash = content_hash(request)
        with self.store._transaction() as cur:
            duplicate = self._idem(cur, idempotency_key, "select_backlog_question", request_hash)
            if duplicate is not None:
                return duplicate
            question = read_exact_backlog_question(cur, question_ref)
            head = self._head(cur, question_ref)
            if self._latest_state(cur, question_ref) != "open":
                raise ResearchQuestionConflict(
                    "only an open question can be selected"
                )
            decision = _read_exact_agenda_decision(cur, decision_ref)
            cycle = read_exact_agenda_cycle(cur, decision["cycle_ref"])
            mandate = read_exact_mandate_version(cur, cycle["mandate_version_ref"])
            if decision["policy_version_ref"] != cycle["policy_version_ref"]:
                raise ResearchQuestionConflict(
                    "decision policy differs from the cycle's frozen policy"
                )
            if mandate["mandate_ref"] != question["identity"]["mandate_ref"]:
                raise ResearchQuestionConflict(
                    "decision cycle belongs to a different mandate than the question"
                )
            if cycle["company_ref"] != question["identity"]["company_ref"]:
                raise ResearchQuestionConflict(
                    "decision cycle covers a different company than the question"
                )
            matched_candidate: str | None = None
            for candidate_ref in decision["selected_candidate_refs"]:
                candidate = _read_exact_agenda_candidate(cur, candidate_ref)
                if (
                    candidate["cycle_ref"] == cycle["cycle_id"]
                    and candidate["valid"]
                    and candidate["proposed_question"] == head["question"]
                    and candidate["answer_criteria"] == head["answer_criteria"]
                    and candidate["source_refs"] == head["source_refs"]
                ):
                    matched_candidate = candidate_ref
                    break
            if matched_candidate is None:
                raise ResearchQuestionConflict(
                    "the decision does not select this exact question content"
                )
            event = self._event(
                cur, question_ref, "selected", "agenda_decision_selected",
                {
                    "decision_ref": decision["id"],
                    "decision_hash": decision["content_hash"],
                    "cycle_ref": cycle["cycle_id"],
                    "cycle_hash": cycle["content_hash"],
                    "candidate_ref": matched_candidate,
                },
                actor_ref,
            )
            link_id = "research-question-selection:" + content_hash({
                "question_ref": question_ref,
                "decision_ref": decision["id"],
                "cycle_ref": cycle["cycle_id"],
            })[:32]
            link_wire = self._record({
                "schema_version": SCHEMA_VERSION,
                "id": link_id,
                "question_ref": question_ref,
                "decision_ref": decision["id"],
                "decision_hash": decision["content_hash"],
                "cycle_ref": cycle["cycle_id"],
                "cycle_hash": cycle["content_hash"],
                "candidate_ref": matched_candidate,
                "event_ref": event["id"],
                "actor_ref": actor_ref,
                "created_at": event["created_at"],
            })
            cur.execute(
                "INSERT INTO backlog_selection_links(link_id,question_ref,decision_ref,"
                "decision_hash,cycle_ref,cycle_hash,candidate_ref,event_ref,actor_ref,"
                "created_at,content_hash) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    link_id, question_ref, decision["id"],
                    decision["content_hash"], cycle["cycle_id"],
                    cycle["content_hash"], matched_candidate, event["id"],
                    actor_ref, event["created_at"], link_wire["content_hash"],
                ),
            )
            result = {
                "status": "fresh",
                "question_ref": question_ref,
                "state": "selected",
                "event": event,
                "selection_link": link_wire,
            }
            self._save_idem(
                cur, idempotency_key, "select_backlog_question", request_hash, result
            )
            return result

    def plan_question(
        self,
        *,
        question_ref: str,
        plan_version_ref: str,
        plan_version_hash: str,
        actor_ref: str,
        reason: str = "plan_bound",
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Advance a selected question to planned with an exact plan binding.

        The planned transition is only legal with an exact immutable
        ResearchPlanVersion that binds this question's head version; the plan
        is re-read from the plan authority and its hash re-computed inside
        the same transaction.  The record/select flows call the plan
        authority instead (``create_plan`` appends the same event in one
        transaction); this method keeps the identical gate for direct
        callers and for replay, so an unbound planned transition is
        impossible from any path.
        """

        from .research_plan import revalidate_plan_binds_question

        question_ref = _text(question_ref, "question_ref")
        plan_version_ref = _text(plan_version_ref, "plan_version_ref")
        plan_version_hash = _hash_text(plan_version_hash, "plan_version_hash")
        actor_ref = _text(actor_ref, "actor_ref")
        reason = _text(reason, "reason")
        request = {
            "question_ref": question_ref,
            "plan_version_ref": plan_version_ref,
            "plan_version_hash": plan_version_hash,
            "reason": reason,
            "actor_ref": actor_ref,
        }
        request_hash = content_hash(request)
        with self.store._transaction() as cur:
            duplicate = self._idem(cur, idempotency_key, "plan_backlog_question", request_hash)
            if duplicate is not None:
                return duplicate
            head = self._head(cur, question_ref)
            if self._latest_state(cur, question_ref) != "selected":
                raise ResearchQuestionConflict(
                    "only a selected question can be planned"
                )
            plan = revalidate_plan_binds_question(
                cur, plan_version_ref, question_ref, head_version_id=head["id"],
            )
            if plan["content_hash"] != plan_version_hash:
                raise ResearchQuestionConflict(
                    "planned event plan hash drifted from the exact plan"
                )
            event = _append_event_row(
                cur, question_ref=question_ref, state="planned", reason=reason,
                metadata={
                    "plan_version_ref": plan_version_ref,
                    "plan_version_hash": plan_version_hash,
                },
                actor_ref=actor_ref,
            )
            result = {
                "status": "fresh", "question_ref": question_ref,
                "state": "planned", "event": event,
            }
            self._save_idem(
                cur, idempotency_key, "plan_backlog_question", request_hash, result
            )
            return result

    def start_question(
        self,
        *,
        question_ref: str,
        plan_version_ref: str,
        plan_version_hash: str,
        workflow_version_ref: str,
        workflow_version_hash: str,
        root_work_order_ref: str,
        root_work_order_hash: str,
        actor_ref: str,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Advance a planned question to in_progress after an approved start.

        The in_progress transition is only legal after an approved plan start
        that created the exact WorkflowRunVersion + root WorkOrder; the plan
        start binding and every ref/hash are re-read and re-computed from
        authority in the same transaction.  The control plane appends the
        same event inside its own single transaction; this method keeps the
        identical gate for direct callers and replay.
        """

        from .research_plan import (
            _revalidate_plan_start_binding,
            revalidate_plan_binds_question,
        )

        question_ref = _text(question_ref, "question_ref")
        plan_version_ref = _text(plan_version_ref, "plan_version_ref")
        plan_version_hash = _hash_text(plan_version_hash, "plan_version_hash")
        workflow_version_ref = _text(workflow_version_ref, "workflow_version_ref")
        workflow_version_hash = _hash_text(workflow_version_hash, "workflow_version_hash")
        root_work_order_ref = _text(root_work_order_ref, "root_work_order_ref")
        root_work_order_hash = _hash_text(root_work_order_hash, "root_work_order_hash")
        actor_ref = _text(actor_ref, "actor_ref")
        request = {
            "question_ref": question_ref,
            "plan_version_ref": plan_version_ref,
            "plan_version_hash": plan_version_hash,
            "workflow_version_ref": workflow_version_ref,
            "workflow_version_hash": workflow_version_hash,
            "root_work_order_ref": root_work_order_ref,
            "root_work_order_hash": root_work_order_hash,
            "actor_ref": actor_ref,
        }
        request_hash = content_hash(request)
        with self.store._transaction() as cur:
            duplicate = self._idem(cur, idempotency_key, "start_backlog_question", request_hash)
            if duplicate is not None:
                return duplicate
            if self._latest_state(cur, question_ref) != "planned":
                raise ResearchQuestionConflict(
                    "only a planned question can start"
                )
            head = self._head(cur, question_ref)
            events = _read_exact_event_history(cur, question_ref)
            planned = next(
                (event for event in reversed(events) if event["state"] == "planned"),
                None,
            )
            if planned is None or planned["metadata"] != {
                "plan_version_ref": plan_version_ref,
                "plan_version_hash": plan_version_hash,
            }:
                raise ResearchQuestionConflict(
                    "start binding drifted from the exact planned plan binding"
                )
            revalidate_plan_binds_question(
                cur, plan_version_ref, question_ref, head_version_id=head["id"],
            )
            _revalidate_plan_start_binding(
                cur,
                plan_version_ref=plan_version_ref,
                plan_version_hash=plan_version_hash,
                workflow_version_ref=workflow_version_ref,
                workflow_version_hash=workflow_version_hash,
                root_work_order_ref=root_work_order_ref,
                root_work_order_hash=root_work_order_hash,
            )
            event = _append_event_row(
                cur, question_ref=question_ref, state="in_progress",
                reason="approved_plan_started",
                metadata={
                    "plan_version_ref": plan_version_ref,
                    "plan_version_hash": plan_version_hash,
                    "workflow_version_ref": workflow_version_ref,
                    "workflow_version_hash": workflow_version_hash,
                    "root_work_order_ref": root_work_order_ref,
                    "root_work_order_hash": root_work_order_hash,
                },
                actor_ref=actor_ref,
            )
            result = {
                "status": "fresh", "question_ref": question_ref,
                "state": "in_progress", "event": event,
            }
            self._save_idem(
                cur, idempotency_key, "start_backlog_question", request_hash, result
            )
            return result

    def answer_question(
        self,
        *,
        question_ref: str,
        claim_version_refs: Sequence[str],
        actor_ref: str,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Bind an in_progress question to one or more formal ClaimVersions.

        Every claim ref is re-read from the Core Ledger and its exact hash
        recomputed inside the same transaction that appends the answered
        event and the binding rows, so a failure anywhere rolls everything
        back.  Candidate/staging/unverified or tampered claims fail closed;
        an AgendaDecision is never an answer.
        """

        question_ref = _text(question_ref, "question_ref")
        claim_version_refs = _refs(
            claim_version_refs, "claim_version_refs", nonempty=True
        )
        actor_ref = _text(actor_ref, "actor_ref")
        request = {
            "question_ref": question_ref,
            "claim_version_refs": claim_version_refs,
            "actor_ref": actor_ref,
        }
        request_hash = content_hash(request)
        with self.store._transaction() as cur:
            duplicate = self._idem(cur, idempotency_key, "answer_backlog_question", request_hash)
            if duplicate is not None:
                return duplicate
            read_exact_backlog_question(cur, question_ref)
            if self._latest_state(cur, question_ref) != "in_progress":
                raise ResearchQuestionConflict(
                    "only an in_progress question can be answered"
                )
            bindings = [
                _read_exact_claim_version(cur, ref) for ref in claim_version_refs
            ]
            created_at = _now()
            event = self._event(
                cur, question_ref, "answered", "formal_claim_bound",
                {
                    "claim_version_refs": claim_version_refs,
                    "claim_version_hashes": [
                        binding["claim_version_hash"] for binding in bindings
                    ],
                },
                actor_ref,
            )
            binding_wires: list[dict[str, Any]] = []
            for binding in bindings:
                binding_id = "research-question-answer:" + content_hash({
                    "question_ref": question_ref,
                    "claim_version_ref": binding["claim_version_ref"],
                })[:32]
                binding_wire = self._record({
                    "schema_version": SCHEMA_VERSION,
                    "id": binding_id,
                    "question_ref": question_ref,
                    "claim_version_ref": binding["claim_version_ref"],
                    "claim_version_hash": binding["claim_version_hash"],
                    "claim_ref": binding["claim_ref"],
                    "event_ref": event["id"],
                    "actor_ref": actor_ref,
                    "created_at": created_at,
                })
                cur.execute(
                    "INSERT INTO backlog_answer_bindings(binding_id,question_ref,"
                    "claim_version_ref,claim_version_hash,claim_ref,event_ref,actor_ref,"
                    "created_at,content_hash) VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        binding_id, question_ref, binding["claim_version_ref"],
                        binding["claim_version_hash"], binding["claim_ref"],
                        event["id"], actor_ref, created_at,
                        binding_wire["content_hash"],
                    ),
                )
                binding_wires.append(binding_wire)
            result = {
                "status": "fresh",
                "question_ref": question_ref,
                "state": "answered",
                "event": event,
                "answer_bindings": binding_wires,
            }
            self._save_idem(
                cur, idempotency_key, "answer_backlog_question", request_hash, result
            )
            return result

    def block_question(
        self,
        *,
        question_ref: str,
        reason: str,
        actor_ref: str,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Move an in_progress question to the terminal blocked state."""

        question_ref = _text(question_ref, "question_ref")
        reason = _text(reason, "reason")
        actor_ref = _text(actor_ref, "actor_ref")
        request = {"question_ref": question_ref, "reason": reason, "actor_ref": actor_ref}
        request_hash = content_hash(request)
        with self.store._transaction() as cur:
            duplicate = self._idem(cur, idempotency_key, "block_backlog_question", request_hash)
            if duplicate is not None:
                return duplicate
            read_exact_backlog_question(cur, question_ref)
            event = self._event(
                cur, question_ref, "blocked", reason, {}, actor_ref
            )
            result = {
                "status": "fresh", "question_ref": question_ref,
                "state": "blocked", "event": event,
            }
            self._save_idem(
                cur, idempotency_key, "block_backlog_question", request_hash, result
            )
            return result

    def retire_question(
        self,
        *,
        question_ref: str,
        reason: str,
        actor_ref: str,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Move an in_progress question to the terminal retired state."""

        question_ref = _text(question_ref, "question_ref")
        reason = _text(reason, "reason")
        actor_ref = _text(actor_ref, "actor_ref")
        request = {"question_ref": question_ref, "reason": reason, "actor_ref": actor_ref}
        request_hash = content_hash(request)
        with self.store._transaction() as cur:
            duplicate = self._idem(cur, idempotency_key, "retire_backlog_question", request_hash)
            if duplicate is not None:
                return duplicate
            read_exact_backlog_question(cur, question_ref)
            event = self._event(
                cur, question_ref, "retired", reason, {}, actor_ref
            )
            result = {
                "status": "fresh", "question_ref": question_ref,
                "state": "retired", "event": event,
            }
            self._save_idem(
                cur, idempotency_key, "retire_backlog_question", request_hash, result
            )
            return result

    def question(self, question_ref: str) -> dict[str, Any]:
        """Exact reader: head version, derived state, links and bindings."""

        question_ref = _text(question_ref, "question_ref")
        cursor = self.connection.cursor()
        identity = read_exact_backlog_question(cursor, question_ref)
        head = self._head(cursor, question_ref)
        events = _read_exact_event_history(cursor, question_ref)
        links = [
            _read_exact_selection_link(cursor, row["link_id"])
            for row in cursor.execute(
                "SELECT link_id FROM backlog_selection_links WHERE question_ref=? "
                "ORDER BY created_at,link_id",
                (question_ref,),
            ).fetchall()
        ]
        bindings = [
            _read_exact_answer_binding(cursor, row["binding_id"])
            for row in cursor.execute(
                "SELECT binding_id FROM backlog_answer_bindings WHERE question_ref=? "
                "ORDER BY created_at,binding_id",
                (question_ref,),
            ).fetchall()
        ]
        return {
            "question_ref": question_ref,
            "identity": identity,
            "head": head,
            "state": events[-1]["state"] if events else None,
            "events": events,
            "selection_links": links,
            "answer_bindings": bindings,
        }

    def question_version(self, version_ref: str) -> dict[str, Any]:
        """Exact reader for one immutable content version."""

        return read_exact_backlog_question_version(self.connection.cursor(), version_ref)

    def history(self, question_ref: str) -> dict[str, Any]:
        """Exact replay of every version and event for one question."""

        question_ref = _text(question_ref, "question_ref")
        cursor = self.connection.cursor()
        identity = read_exact_backlog_question(cursor, question_ref)
        versions = [
            read_exact_backlog_question_version(cursor, row["version_id"])
            for row in cursor.execute(
                "SELECT version_id FROM backlog_question_versions WHERE question_ref=? "
                "ORDER BY version_number",
                (question_ref,),
            ).fetchall()
        ]
        events = _read_exact_event_history(cursor, question_ref)
        return {
            "question_ref": question_ref,
            "identity": identity,
            "versions": versions,
            "events": events,
            "state": events[-1]["state"] if events else None,
        }

    def questions(self, *, mandate_ref: str | None = None) -> list[dict[str, Any]]:
        """List backlog heads, optionally filtered by logical mandate."""

        cursor = self.connection.cursor()
        rows = cursor.execute(
            "SELECT p.question_ref FROM backlog_question_pointer p "
            "JOIN backlog_question_versions v ON v.version_id=p.version_id "
            "WHERE (? IS NULL OR v.mandate_ref=?) ORDER BY p.question_ref",
            (mandate_ref, mandate_ref),
        ).fetchall()
        return [self.question(row["question_ref"]) for row in rows]

    def mandate_progress(self, mandate_ref: str) -> dict[str, Any]:
        """Deterministic, rebuildable progress projection for one mandate.

        The projection is a pure re-derivation from exact backlog and mandate
        authority rows: it never writes to any table (so it cannot mutate the
        objective/constraint Mandate authority and cannot become an alternate
        authority) and rebuilding it from the same authority state always
        yields the same canonical record and hash.  ``created_at`` is the
        latest authority timestamp among the projection inputs, so the record
        is a deterministic function of the authority state.
        """

        mandate_ref = _text(mandate_ref, "mandate_ref")
        cursor = self.connection.cursor()
        pointer = cursor.execute(
            "SELECT version_id FROM mandate_pointer WHERE mandate_ref=? AND active=1",
            (mandate_ref,),
        ).fetchone()
        if pointer is None:
            raise ResearchQuestionNotFound(f"active mandate {mandate_ref}")
        mandate = read_exact_mandate_version(cursor, pointer["version_id"])
        if mandate["mandate_ref"] != mandate_ref:
            raise ResearchQuestionConflict("mandate pointer resolved a different mandate")
        rows = cursor.execute(
            "SELECT question_ref FROM backlog_questions ORDER BY question_ref"
        ).fetchall()
        questions: list[dict[str, Any]] = []
        totals: dict[str, int] = {state: 0 for state in QUESTION_STATES}
        answered_claim_refs: list[dict[str, str]] = []
        latest_created_at = mandate["created_at"]
        for row in rows:
            question = self.question(row["question_ref"])
            identity = question["identity"]["identity"]
            if identity.get("mandate_ref") != mandate_ref:
                continue
            state = question["state"]
            totals[state] += 1
            entry = {
                "question_ref": question["question_ref"],
                "question_version_ref": question["head"]["id"],
                "content_hash": question["head"]["content_hash"],
                "state": state,
                "created_at": question["head"]["created_at"],
            }
            if state == "answered":
                entry["answer_bindings"] = [
                    {
                        "claim_version_ref": binding["claim_version_ref"],
                        "claim_version_hash": binding["claim_version_hash"],
                        "claim_ref": binding["claim_ref"],
                    }
                    for binding in question["answer_bindings"]
                ]
                answered_claim_refs.extend(entry["answer_bindings"])
            questions.append(entry)
            for event in question["events"]:
                if event["created_at"] > latest_created_at:
                    latest_created_at = event["created_at"]
        body = {
            "schema_version": SCHEMA_VERSION,
            "mandate_ref": mandate_ref,
            "mandate_version_ref": mandate["id"],
            "mandate_version_hash": mandate["content_hash"],
            "created_at": latest_created_at,
            "totals": totals,
            "questions": questions,
            "answered_claim_refs": answered_claim_refs,
        }
        record = {
            **body,
            "id": "mandate-question-progress:" + content_hash(body)[:32],
        }
        record["content_hash"] = content_hash(record)
        return record


__all__ = [
    "ResearchQuestionBacklog",
    "ResearchQuestionError",
    "ResearchQuestionValidationError",
    "ResearchQuestionConflict",
    "ResearchQuestionNotFound",
    "QUESTION_STATES",
    "question_identity",
    "question_ref_for",
    "read_exact_backlog_question",
    "read_exact_backlog_question_version",
    "read_exact_backlog_event",
]
