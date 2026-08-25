"""Core-side admission for confirmed natural-language intent candidates.

The Cockpit stages and confirms candidates in a separate append-only database.
This module lives inside the owner-only writer process and resolves every
subject from Core authority before delegating to the existing ResearchQuestion
and Bounded Planner writers.  It never trusts a caller-supplied mandate body,
company, coverage catalog, or actor.
"""

from __future__ import annotations

import json
import re
from typing import Any, Mapping

from .agenda import AgendaStore, read_exact_agenda_cycle, read_exact_mandate_version
from .bounded_planner_loop import BoundedPlannerAuthority
from .research_question_backlog import ResearchQuestionBacklog
from .store import canonical_json, content_hash


_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_HUMAN_RE = re.compile(r"^human:[A-Za-z0-9][A-Za-z0-9._:-]*$")
_BINDING_FIELDS = {
    "kind", "ref", "hash", "label", "state", "authority", "parent_ref",
    "allowed_intents",
}
_QUESTION_SUBJECT_KINDS = {
    "agenda_decision", "bounded_planner_loop", "coverage_item", "mandate",
}


class IntentDispatchError(Exception):
    pass


class IntentDispatchValidationError(IntentDispatchError, ValueError):
    pass


class IntentDispatchConflict(IntentDispatchError):
    pass


class IntentDispatchNotFound(IntentDispatchError):
    pass


def _text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise IntentDispatchValidationError(f"{name} must be a non-empty string")
    return value.strip()


def _hash(value: Any, name: str) -> str:
    value = _text(value, name)
    if _SHA256_RE.fullmatch(value) is None:
        raise IntentDispatchValidationError(f"{name} must be lowercase SHA-256")
    return value


def _human(value: Any) -> str:
    value = _text(value, "actor_ref")
    if _HUMAN_RE.fullmatch(value) is None:
        raise IntentDispatchValidationError("actor_ref must use the human: namespace")
    return value


def _binding(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != _BINDING_FIELDS:
        raise IntentDispatchValidationError(f"{name} has an invalid closed shape")
    wire = dict(value)
    wire["kind"] = _text(wire["kind"], f"{name}.kind")
    wire["ref"] = _text(wire["ref"], f"{name}.ref")
    wire["hash"] = _hash(wire["hash"], f"{name}.hash")
    wire["label"] = _text(wire["label"], f"{name}.label")
    wire["state"] = _text(wire["state"], f"{name}.state")
    if not isinstance(wire["authority"], bool):
        raise IntentDispatchValidationError(f"{name}.authority must be boolean")
    if wire["parent_ref"] is not None:
        wire["parent_ref"] = _text(wire["parent_ref"], f"{name}.parent_ref")
    allowed = wire["allowed_intents"]
    if (
        not isinstance(allowed, list)
        or not allowed
        or any(not isinstance(item, str) or not item for item in allowed)
        or allowed != sorted(set(allowed))
    ):
        raise IntentDispatchValidationError(
            f"{name}.allowed_intents must be a sorted unique string array"
        )
    return wire


def _wire_binding(
    *,
    kind: str,
    ref: str,
    hash_value: str,
    label: str,
    state: str,
    authority: bool,
    allowed_intents: tuple[str, ...],
    parent_ref: str | None = None,
) -> dict[str, Any]:
    return _binding(
        {
            "kind": kind,
            "ref": ref,
            "hash": hash_value,
            "label": label,
            "state": state,
            "authority": authority,
            "parent_ref": parent_ref,
            "allowed_intents": sorted(allowed_intents),
        },
        "authority binding",
    )


def _coverage_hash(loop: Mapping[str, Any], coverage_item_ref: str, state: str) -> str:
    return content_hash(
        {
            "kind": "coverage_item",
            "ref": coverage_item_ref,
            "parent_ref": loop["id"],
            "parent_hash": loop["content_hash"],
            "state": state,
        }
    )


class IntentWriterAuthority:
    """Exact resolver and adapter to the existing Core writer authorities."""

    def __init__(
        self,
        agenda: AgendaStore,
        backlog: ResearchQuestionBacklog,
        bounded: BoundedPlannerAuthority,
    ) -> None:
        if not (
            agenda.connection is backlog.connection is bounded.connection
        ):
            raise TypeError("intent writer authorities must share one Core connection")
        self.agenda = agenda
        self.backlog = backlog
        self.bounded = bounded
        self.connection = agenda.connection

    def context_bindings(self) -> list[dict[str, Any]]:
        bindings: list[dict[str, Any]] = []
        for mandate in self.agenda.active_mandates():
            bindings.append(
                _wire_binding(
                    kind="mandate",
                    ref=mandate["id"],
                    hash_value=mandate["content_hash"],
                    label=f"Mandate · {mandate['objective']}",
                    state="active",
                    authority=True,
                    allowed_intents=("meta", "priority", "question"),
                )
            )
        rows = self.connection.execute(
            "SELECT v.version_id FROM bounded_planner_loop_versions v "
            "WHERE NOT EXISTS (SELECT 1 FROM bounded_planner_loop_versions newer "
            " WHERE newer.loop_ref=v.loop_ref AND newer.version_number>v.version_number) "
            "AND NOT EXISTS (SELECT 1 FROM bounded_planner_terminal_events t "
            " WHERE t.loop_version_ref=v.version_id) ORDER BY v.version_id"
        ).fetchall()
        for row in rows:
            loop = self.bounded.loop(row["version_id"])
            question = self.backlog.question_version(loop["question_version_ref"])
            bindings.append(
                _wire_binding(
                    kind="bounded_planner_loop",
                    ref=loop["id"],
                    hash_value=loop["content_hash"],
                    label=f"Research loop · {question['question']}",
                    state="active",
                    authority=True,
                    allowed_intents=("directive", "meta", "question"),
                )
            )
            outcomes = {
                item["coverage_item_ref"]: item
                for item in self.bounded.outcomes(loop["id"])
            }
            for coverage_item_ref in loop["required_coverage_items"]:
                state = "covered" if coverage_item_ref in outcomes else "uncovered"
                bindings.append(
                    _wire_binding(
                        kind="coverage_item",
                        ref=coverage_item_ref,
                        hash_value=_coverage_hash(loop, coverage_item_ref, state),
                        label=coverage_item_ref,
                        state=state,
                        authority=True,
                        parent_ref=loop["id"],
                        allowed_intents=("directive", "meta", "question"),
                    )
                )
        return sorted(bindings, key=lambda item: (item["kind"], item["ref"]))

    def _current_binding(self, value: Any, *, required_intent: str) -> dict[str, Any]:
        candidate = _binding(value, "subject binding")
        if candidate["kind"] == "agenda_decision":
            payload = self._agenda_decision_payload(candidate)
            feedback = self.connection.execute(
                "SELECT COALESCE(subject_ref,actor_ref) AS subject_ref "
                "FROM agenda_feedback WHERE decision_id=?",
                (candidate["ref"],),
            ).fetchall()
            subjects = {row["subject_ref"] for row in feedback}
            if any(
                isinstance(subject, str) and subject.startswith("human:")
                for subject in subjects
            ):
                state = "explicit_human"
            elif "automation:timeout" in subjects:
                state = "auto_accept_timeout"
            else:
                state = "pending"
            current = _wire_binding(
                kind="agenda_decision",
                ref=candidate["ref"],
                hash_value=candidate["hash"],
                label=f"Agenda · {payload.get('company_ref') or candidate['ref']}",
                state=state,
                authority=True,
                allowed_intents=("approval", "meta", "priority", "question"),
            )
            if canonical_json(current) != canonical_json(candidate):
                raise IntentDispatchConflict(
                    "intent subject binding is stale or unavailable"
                )
            if required_intent not in candidate["allowed_intents"]:
                raise IntentDispatchValidationError(
                    "intent is not allowed for this subject"
                )
            return candidate
        matches = [
            item
            for item in self.context_bindings()
            if item["kind"] == candidate["kind"] and item["ref"] == candidate["ref"]
        ]
        if len(matches) != 1 or canonical_json(matches[0]) != canonical_json(candidate):
            raise IntentDispatchConflict("intent subject binding is stale or unavailable")
        if required_intent not in candidate["allowed_intents"]:
            raise IntentDispatchValidationError("intent is not allowed for this subject")
        return candidate

    def _active_mandate_for_question(
        self, question: Mapping[str, Any]
    ) -> dict[str, Any]:
        row = self.connection.execute(
            "SELECT version_id FROM mandate_pointer WHERE mandate_ref=? AND active=1",
            (question["mandate_ref"],),
        ).fetchone()
        if row is None:
            raise IntentDispatchNotFound("question mandate is not active")
        mandate = read_exact_mandate_version(self.connection.cursor(), row["version_id"])
        return self._require_active_mandate(mandate, question["company_ref"])

    def _require_active_mandate(
        self, mandate: Mapping[str, Any], company_ref: str
    ) -> dict[str, Any]:
        matches = [
            item
            for item in self.agenda.active_mandates()
            if item["id"] == mandate["id"]
            and item["content_hash"] == mandate["content_hash"]
        ]
        if len(matches) != 1:
            raise IntentDispatchNotFound("question mandate is not currently active")
        if company_ref not in mandate["scope_refs"]:
            raise IntentDispatchConflict("question company left the active mandate scope")
        return dict(mandate)

    def _agenda_decision_payload(
        self, subject: Mapping[str, Any]
    ) -> dict[str, Any]:
        rows = self.connection.execute(
            "SELECT payload_json,payload_hash FROM agenda_outbox_messages "
            "WHERE json_extract(payload_json,'$.decision_ref')=?",
            (subject["ref"],),
        ).fetchall()
        if len(rows) != 1:
            raise IntentDispatchNotFound("Agenda decision payload is unavailable")
        row = rows[0]
        try:
            payload = json.loads(row["payload_json"])
        except (TypeError, json.JSONDecodeError) as exc:
            raise IntentDispatchConflict("Agenda decision payload is invalid") from exc
        if (
            canonical_json(payload) != row["payload_json"]
            or content_hash(payload) != row["payload_hash"]
            or row["payload_hash"] != subject["hash"]
            or payload.get("decision_ref") != subject["ref"]
        ):
            raise IntentDispatchConflict("Agenda decision payload hash drifted")
        return payload

    def _question_subject(
        self, subject: Mapping[str, Any]
    ) -> tuple[dict[str, Any], str]:
        kind = subject["kind"]
        if kind not in _QUESTION_SUBJECT_KINDS:
            raise IntentDispatchValidationError(
                "question confirmation requires a mandate-resolvable subject"
            )
        if kind == "mandate":
            mandate = read_exact_mandate_version(
                self.connection.cursor(), subject["ref"]
            )
            if mandate["content_hash"] != subject["hash"]:
                raise IntentDispatchConflict("mandate binding hash drifted")
            if len(mandate["scope_refs"]) != 1:
                raise IntentDispatchValidationError(
                    "multi-company mandate requires a company-specific subject"
                )
            company_ref = mandate["scope_refs"][0]
            return self._require_active_mandate(mandate, company_ref), company_ref
        if kind == "agenda_decision":
            payload = self._agenda_decision_payload(subject)
            cycle = read_exact_agenda_cycle(
                self.connection.cursor(), _text(payload.get("cycle_ref"), "cycle_ref")
            )
            if cycle["company_ref"] != payload.get("company_ref"):
                raise IntentDispatchConflict("Agenda decision company binding drifted")
            mandate = read_exact_mandate_version(
                self.connection.cursor(), cycle["mandate_version_ref"]
            )
            if mandate["content_hash"] != cycle["mandate_version_hash"]:
                raise IntentDispatchConflict("Agenda cycle mandate binding drifted")
            return (
                self._require_active_mandate(mandate, cycle["company_ref"]),
                cycle["company_ref"],
            )
        loop_ref = subject["ref"] if kind == "bounded_planner_loop" else subject["parent_ref"]
        loop = self.bounded.loop(_text(loop_ref, "loop_version_ref"))
        if kind == "bounded_planner_loop" and loop["content_hash"] != subject["hash"]:
            raise IntentDispatchConflict("bounded loop binding hash drifted")
        question = self.backlog.question_version(loop["question_version_ref"])
        return self._active_mandate_for_question(question), question["company_ref"]

    def admit_question(
        self,
        *,
        subject_binding: Mapping[str, Any],
        question: str,
        answer_criteria: str,
        candidate_version_ref: str,
        candidate_version_hash: str,
        confirmation_ref: str,
        confirmation_hash: str,
        actor_ref: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        actor_ref = _human(actor_ref)
        subject = self._current_binding(subject_binding, required_intent="question")
        mandate, company_ref = self._question_subject(subject)
        candidate_version_ref = _text(candidate_version_ref, "candidate_version_ref")
        candidate_version_hash = _hash(candidate_version_hash, "candidate_version_hash")
        confirmation_ref = _text(confirmation_ref, "confirmation_ref")
        confirmation_hash = _hash(confirmation_hash, "confirmation_hash")
        result = self.backlog.record_question(
            mandate_version_ref=mandate["id"],
            company_ref=company_ref,
            question=_text(question, "question"),
            answer_criteria=_text(answer_criteria, "answer_criteria"),
            source_refs=[candidate_version_ref, confirmation_ref, subject["ref"]],
            actor_ref=actor_ref,
            idempotency_key=_text(idempotency_key, "idempotency_key"),
        )
        return {
            "operation": "record_backlog_question",
            "candidate_version_ref": candidate_version_ref,
            "candidate_version_hash": candidate_version_hash,
            "confirmation_ref": confirmation_ref,
            "confirmation_hash": confirmation_hash,
            "mandate_version_ref": mandate["id"],
            "mandate_version_hash": mandate["content_hash"],
            "company_ref": company_ref,
            "authority_result": result,
        }

    def issue_directive(
        self,
        *,
        loop_binding: Mapping[str, Any],
        target_coverage_item_binding: Mapping[str, Any] | None,
        verbatim_text: str,
        control_effect: str,
        candidate_version_ref: str,
        candidate_version_hash: str,
        confirmation_ref: str,
        confirmation_hash: str,
        actor_ref: str,
    ) -> dict[str, Any]:
        actor_ref = _human(actor_ref)
        loop = self._current_binding(loop_binding, required_intent="directive")
        if loop["kind"] != "bounded_planner_loop":
            raise IntentDispatchValidationError("directive loop binding is invalid")
        target = None
        if target_coverage_item_binding is not None:
            target = self._current_binding(
                target_coverage_item_binding, required_intent="directive"
            )
            if target["kind"] != "coverage_item" or target["parent_ref"] != loop["ref"]:
                raise IntentDispatchValidationError(
                    "directive target is outside the exact loop"
                )
        result = self.bounded.issue_directive(
            loop["ref"],
            verbatim_text=_text(verbatim_text, "verbatim_text"),
            control_effect=_text(control_effect, "control_effect"),
            target_coverage_item_ref=None if target is None else target["ref"],
            actor_ref=actor_ref,
        )
        return {
            "operation": "issue_research_directive",
            "candidate_version_ref": _text(candidate_version_ref, "candidate_version_ref"),
            "candidate_version_hash": _hash(candidate_version_hash, "candidate_version_hash"),
            "confirmation_ref": _text(confirmation_ref, "confirmation_ref"),
            "confirmation_hash": _hash(confirmation_hash, "confirmation_hash"),
            "authority_result": result,
        }


__all__ = [
    "IntentDispatchConflict", "IntentDispatchError", "IntentDispatchNotFound",
    "IntentDispatchValidationError", "IntentWriterAuthority",
]
