"""Versioned research playbook authority.

The playbook codifies the team's analyst training manual — the three-stage
research process (Initial Screen → Investment Memo → Active Coverage), the
Deep Insight Gate, the memo key questions, the analyst-level acceptance bar,
tracker classes, model discipline and evidence discipline — as an immutable,
human-only, append-only authority.  It is research *method*, never Evidence or
a Claim, and it is industry-agnostic: industry causal chains stay in the
Driver Pack and the Research Constitution.

A CoverageMission binds one exact playbook version so that stage order,
required outputs and human checkpoints are machine-checkable instead of
living only in prose.  Nothing in this module lets a model or automation
principal publish or activate a playbook.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Mapping
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .store import DaltonStore, canonical_json, content_hash


SCHEMA_VERSION = "0.1"
_SCHEMA_PATH = Path(__file__).with_name("research_playbook_schema.sql")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_HUMAN_RE = re.compile(r"^human:[A-Za-z0-9][A-Za-z0-9._/@:-]*$")

# Frozen research stage order.  A playbook must describe exactly these six
# stages in this order; a mission advances a company through them one gate
# at a time.
STAGE_ORDER: tuple[str, ...] = (
    "initial_screen",
    "deep_insight_gate",
    "industry_model",
    "company_model",
    "investment_memo",
    "active_coverage",
)
# Stages whose exit gate can only be passed by a human principal.  A playbook
# may add human checkpoints but can never remove these two.
HUMAN_CHECKPOINT_STAGES: frozenset[str] = frozenset({"deep_insight_gate", "investment_memo"})
# The five-word decision vocabulary of Active Coverage.  Frozen: every event
# ends in exactly one of these decisions.
DECISION_VOCABULARY: tuple[str, ...] = (
    "NO_CHANGE",
    "THESIS_STRENGTHENED",
    "THESIS_WEAKENED",
    "THESIS_BROKEN",
    "NEW_THESIS",
)
NUMBER_PROVENANCE_RULE = "every_timely_number_traces_to_a_tool_result_or_primary_filing"

_STAGE_FIELDS = frozenset({
    "stage_ref", "label", "objective", "required_readings", "required_outputs",
    "exit_gate", "human_checkpoint",
})
_BODY_FIELDS = frozenset({
    "title", "provenance", "stages", "key_questions", "deliverable_templates",
    "decision_vocabulary", "analyst_levels", "tracker_classes",
    "risk_reward_standards", "model_discipline", "evidence_discipline",
})
_VERSION_FIELDS = _BODY_FIELDS | frozenset({
    "schema_version", "id", "created_at", "playbook_ref", "version",
    "prior_version_ref", "actor_ref", "content_hash",
})


class ResearchPlaybookError(RuntimeError):
    """Base error for the research playbook authority."""


class ResearchPlaybookValidationError(ResearchPlaybookError):
    """A request does not satisfy the closed contract."""


class ResearchPlaybookConflict(ResearchPlaybookError):
    """A request conflicts with immutable authority."""


class ResearchPlaybookNotFound(ResearchPlaybookError):
    """A playbook version or pointer is absent."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ResearchPlaybookValidationError(f"{name} must be non-empty text")
    return value.strip()


def _human(value: Any, name: str = "actor_ref") -> str:
    value = _text(value, name)
    if _HUMAN_RE.fullmatch(value) is None:
        raise ResearchPlaybookValidationError(f"{name} must use the human: namespace")
    return value


def _sha256(value: Any, name: str) -> str:
    value = _text(value, name)
    if _SHA256_RE.fullmatch(value) is None:
        raise ResearchPlaybookValidationError(f"{name} must be a lowercase SHA-256")
    return value


def _texts(value: Any, name: str, *, nonempty: bool = False) -> list[str]:
    if not isinstance(value, list):
        raise ResearchPlaybookValidationError(f"{name} must be an array")
    result = [_text(item, f"{name}[]") for item in value]
    if nonempty and not result:
        raise ResearchPlaybookValidationError(f"{name} must not be empty")
    if len(set(result)) != len(result):
        raise ResearchPlaybookValidationError(f"{name} must contain unique values")
    return result


def _positive_int(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ResearchPlaybookValidationError(f"{name} must be a positive integer")
    return value


def _closed(value: Any, fields: frozenset[str], name: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ResearchPlaybookValidationError(f"{name} must be an object")
    result = dict(value)
    if set(result) != fields:
        raise ResearchPlaybookValidationError(
            f"{name} has an invalid closed shape; missing={sorted(fields - set(result))}, "
            f"unknown={sorted(set(result) - fields)}"
        )
    return result


def validate_playbook_stages(value: Any) -> list[dict[str, Any]]:
    """Validate the six frozen stages, in order, with their exit gates."""

    if not isinstance(value, list) or len(value) != len(STAGE_ORDER):
        raise ResearchPlaybookValidationError(
            "stages must list exactly the six frozen stages in order"
        )
    result: list[dict[str, Any]] = []
    for expected, raw in zip(STAGE_ORDER, value):
        stage = _closed(raw, _STAGE_FIELDS, "stage")
        stage_ref = _text(stage["stage_ref"], "stage.stage_ref")
        if stage_ref != expected:
            raise ResearchPlaybookValidationError(
                f"stage order is frozen; expected {expected!r}, got {stage_ref!r}"
            )
        gate = _closed(stage["exit_gate"], frozenset({"questions", "pass_rule"}), "stage.exit_gate")
        checkpoint = stage["human_checkpoint"]
        if type(checkpoint) is not bool:
            raise ResearchPlaybookValidationError("stage.human_checkpoint must be boolean")
        if stage_ref in HUMAN_CHECKPOINT_STAGES and not checkpoint:
            raise ResearchPlaybookValidationError(
                f"playbook cannot remove the human checkpoint on {stage_ref}"
            )
        result.append({
            "stage_ref": stage_ref,
            "label": _text(stage["label"], "stage.label"),
            "objective": _text(stage["objective"], "stage.objective"),
            "required_readings": _texts(stage["required_readings"], "stage.required_readings"),
            "required_outputs": _texts(stage["required_outputs"], "stage.required_outputs", nonempty=True),
            "exit_gate": {
                "questions": _texts(gate["questions"], "stage.exit_gate.questions", nonempty=True),
                "pass_rule": _text(gate["pass_rule"], "stage.exit_gate.pass_rule"),
            },
            "human_checkpoint": checkpoint,
        })
    return result


def validate_playbook_body(value: Mapping[str, Any]) -> dict[str, Any]:
    """Validate the human-authored playbook body (everything but identity)."""

    body = _closed(value, _BODY_FIELDS, "playbook")
    body["title"] = _text(body["title"], "title")
    body["provenance"] = _text(body["provenance"], "provenance")
    body["stages"] = validate_playbook_stages(body["stages"])
    body["key_questions"] = _texts(body["key_questions"], "key_questions", nonempty=True)

    templates = _closed(
        body["deliverable_templates"], frozenset({"initial_screen", "investment_memo"}),
        "deliverable_templates",
    )
    body["deliverable_templates"] = {
        "initial_screen": _texts(templates["initial_screen"], "deliverable_templates.initial_screen", nonempty=True),
        "investment_memo": _texts(templates["investment_memo"], "deliverable_templates.investment_memo", nonempty=True),
    }

    vocabulary = _texts(body["decision_vocabulary"], "decision_vocabulary", nonempty=True)
    if tuple(vocabulary) != DECISION_VOCABULARY:
        raise ResearchPlaybookValidationError(
            "decision_vocabulary is frozen to the five Active Coverage decisions in order"
        )
    body["decision_vocabulary"] = vocabulary

    if not isinstance(body["analyst_levels"], list) or not body["analyst_levels"]:
        raise ResearchPlaybookValidationError("analyst_levels must be a non-empty array")
    levels: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in body["analyst_levels"]:
        level = _closed(raw, frozenset({"level_ref", "label", "criteria"}), "analyst_level")
        ref = _text(level["level_ref"], "analyst_level.level_ref")
        if ref in seen:
            raise ResearchPlaybookValidationError("analyst_levels must have unique level_ref")
        seen.add(ref)
        levels.append({
            "level_ref": ref,
            "label": _text(level["label"], "analyst_level.label"),
            "criteria": _texts(level["criteria"], "analyst_level.criteria", nonempty=True),
        })
    body["analyst_levels"] = levels

    if not isinstance(body["tracker_classes"], list) or not body["tracker_classes"]:
        raise ResearchPlaybookValidationError("tracker_classes must be a non-empty array")
    trackers: list[dict[str, Any]] = []
    seen = set()
    for raw in body["tracker_classes"]:
        tracker = _closed(
            raw, frozenset({"tracker_ref", "label", "cadence", "agent_binding"}), "tracker_class"
        )
        ref = _text(tracker["tracker_ref"], "tracker_class.tracker_ref")
        if ref in seen:
            raise ResearchPlaybookValidationError("tracker_classes must have unique tracker_ref")
        seen.add(ref)
        trackers.append({
            "tracker_ref": ref,
            "label": _text(tracker["label"], "tracker_class.label"),
            "cadence": _text(tracker["cadence"], "tracker_class.cadence"),
            "agent_binding": _text(tracker["agent_binding"], "tracker_class.agent_binding"),
        })
    body["tracker_classes"] = trackers

    body["risk_reward_standards"] = _texts(body["risk_reward_standards"], "risk_reward_standards", nonempty=True)
    body["model_discipline"] = _texts(body["model_discipline"], "model_discipline", nonempty=True)

    discipline = _closed(
        body["evidence_discipline"],
        frozenset({
            "source_hierarchy", "banned_sources",
            "minimum_independent_sources_for_key_numbers", "number_provenance_rule",
        }),
        "evidence_discipline",
    )
    if discipline["number_provenance_rule"] != NUMBER_PROVENANCE_RULE:
        raise ResearchPlaybookValidationError(
            "evidence_discipline.number_provenance_rule cannot be weakened"
        )
    body["evidence_discipline"] = {
        "source_hierarchy": _texts(discipline["source_hierarchy"], "evidence_discipline.source_hierarchy", nonempty=True),
        "banned_sources": _texts(discipline["banned_sources"], "evidence_discipline.banned_sources"),
        "minimum_independent_sources_for_key_numbers": _positive_int(
            discipline["minimum_independent_sources_for_key_numbers"],
            "evidence_discipline.minimum_independent_sources_for_key_numbers",
        ),
        "number_provenance_rule": NUMBER_PROVENANCE_RULE,
    }
    return body


def validate_research_playbook_version(value: Mapping[str, Any]) -> dict[str, Any]:
    wire = dict(value)
    if set(wire) != _VERSION_FIELDS or wire.get("schema_version") != SCHEMA_VERSION:
        raise ResearchPlaybookValidationError("research playbook has an invalid closed shape")
    for field in ("id", "created_at", "playbook_ref"):
        wire[field] = _text(wire[field], field)
    wire["actor_ref"] = _human(wire["actor_ref"])
    wire["content_hash"] = _sha256(wire["content_hash"], "content_hash")
    if type(wire["version"]) is not int or wire["version"] < 1:
        raise ResearchPlaybookValidationError("research playbook version must be positive")
    if wire["prior_version_ref"] is not None:
        wire["prior_version_ref"] = _text(wire["prior_version_ref"], "prior_version_ref")
    body = validate_playbook_body({field: wire[field] for field in _BODY_FIELDS})
    wire.update(body)
    base = dict(wire)
    expected_hash = base.pop("content_hash")
    if content_hash(base) != expected_hash:
        raise ResearchPlaybookValidationError("research playbook content_hash is invalid")
    return wire


def _canonical_record(raw: Any, name: str) -> dict[str, Any]:
    if not isinstance(raw, str):
        raise ResearchPlaybookConflict(f"{name} record is missing")
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ResearchPlaybookConflict(f"{name} record is invalid") from exc
    if not isinstance(value, dict) or canonical_json(value) != raw:
        raise ResearchPlaybookConflict(f"{name} record is not canonical")
    return value


def read_exact_playbook_version(connection: sqlite3.Connection, version_id: str) -> dict[str, Any]:
    """Re-read one playbook version and prove the stored row is intact."""

    version_id = _text(version_id, "playbook_version_ref")
    table = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='research_playbook_versions'"
    ).fetchone()
    if table is None:
        raise ResearchPlaybookNotFound("research playbook authority is not open on this Core")
    row = connection.execute(
        "SELECT * FROM research_playbook_versions WHERE playbook_version_id=?", (version_id,)
    ).fetchone()
    if row is None:
        raise ResearchPlaybookNotFound("research playbook version was not found")
    wire = validate_research_playbook_version(_canonical_record(row["record_json"], "research playbook"))
    if (
        wire["id"] != row["playbook_version_id"]
        or wire["playbook_ref"] != row["playbook_ref"]
        or wire["version"] != row["version_number"]
        or wire["prior_version_ref"] != row["prior_version_id"]
        or wire["actor_ref"] != row["actor_ref"]
        or wire["created_at"] != row["created_at"]
        or wire["content_hash"] != row["content_hash"]
    ):
        raise ResearchPlaybookConflict("research playbook authority drifted")
    return wire


def read_active_playbook_version(connection: sqlite3.Connection, playbook_ref: str) -> dict[str, Any]:
    playbook_ref = _text(playbook_ref, "playbook_ref")
    table = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='research_playbook_pointer'"
    ).fetchone()
    if table is None:
        raise ResearchPlaybookNotFound("research playbook authority is not open on this Core")
    pointer = connection.execute(
        "SELECT * FROM research_playbook_pointer WHERE playbook_ref=?", (playbook_ref,)
    ).fetchone()
    if pointer is None:
        raise ResearchPlaybookNotFound("research playbook pointer was not found")
    wire = read_exact_playbook_version(connection, pointer["playbook_version_id"])
    if wire["version"] != pointer["version_number"] or wire["content_hash"] != pointer["content_hash"]:
        raise ResearchPlaybookConflict("research playbook pointer drifted")
    return wire


class ResearchPlaybookAuthority:
    """Publish and read immutable, human-only research playbook versions."""

    def __init__(self, store: DaltonStore):
        self.store = store
        self.connection = store.connection
        self._authorized = False
        self.connection.create_function(
            "dalton_research_playbook_authorized", 0, lambda: int(self._authorized)
        )
        self.connection.executescript(_SCHEMA_PATH.read_text(encoding="utf-8"))

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Cursor]:
        if self._authorized:
            raise RuntimeError("ResearchPlaybookAuthority operation cannot be nested")
        self._authorized = True
        try:
            with self.store._transaction() as cur:
                yield cur
        finally:
            self._authorized = False

    @staticmethod
    def _request_hash(operation: str, request: Mapping[str, Any]) -> str:
        return content_hash({"operation": operation, "request": dict(request)})

    def _idem(self, cur: sqlite3.Cursor, key: str, operation: str, request_hash: str) -> dict[str, Any] | None:
        row = cur.execute(
            "SELECT * FROM research_playbook_idempotency WHERE idempotency_key=?", (key,)
        ).fetchone()
        if row is None:
            return None
        if row["operation"] != operation or row["request_hash"] != request_hash:
            raise ResearchPlaybookConflict("idempotency key conflicts with prior request")
        return {**json.loads(row["result_json"]), "status": "duplicate"}

    def _save_idem(
        self, cur: sqlite3.Cursor, key: str, operation: str, request_hash: str,
        result: Mapping[str, Any], created_at: str,
    ) -> None:
        cur.execute(
            "INSERT INTO research_playbook_idempotency"
            "(idempotency_key,operation,request_hash,result_json,created_at) VALUES(?,?,?,?,?)",
            (key, operation, request_hash, canonical_json(result), created_at),
        )

    def publish_playbook(
        self,
        playbook_ref: str,
        *,
        title: str,
        provenance: str,
        stages: list[Mapping[str, Any]],
        key_questions: list[str],
        deliverable_templates: Mapping[str, Any],
        decision_vocabulary: list[str],
        analyst_levels: list[Mapping[str, Any]],
        tracker_classes: list[Mapping[str, Any]],
        risk_reward_standards: list[str],
        model_discipline: list[str],
        evidence_discipline: Mapping[str, Any],
        actor_ref: str,
        version_id: str,
        prior_version_ref: str | None,
        idempotency_key: str,
    ) -> dict[str, Any]:
        playbook_ref = _text(playbook_ref, "playbook_ref")
        actor_ref = _human(actor_ref)
        version_id = _text(version_id, "version_id")
        idempotency_key = _text(idempotency_key, "idempotency_key")
        if prior_version_ref is not None:
            prior_version_ref = _text(prior_version_ref, "prior_version_ref")
        body = validate_playbook_body({
            "title": title,
            "provenance": provenance,
            "stages": stages,
            "key_questions": key_questions,
            "deliverable_templates": deliverable_templates,
            "decision_vocabulary": decision_vocabulary,
            "analyst_levels": analyst_levels,
            "tracker_classes": tracker_classes,
            "risk_reward_standards": risk_reward_standards,
            "model_discipline": model_discipline,
            "evidence_discipline": evidence_discipline,
        })
        request = {
            "playbook_ref": playbook_ref,
            **body,
            "actor_ref": actor_ref,
            "version_id": version_id,
            "prior_version_ref": prior_version_ref,
        }
        request_hash = self._request_hash("publish_playbook", request)
        with self._transaction() as cur:
            duplicate = self._idem(cur, idempotency_key, "publish_playbook", request_hash)
            if duplicate is not None:
                return duplicate
            latest = cur.execute(
                "SELECT playbook_version_id,version_number FROM research_playbook_versions "
                "WHERE playbook_ref=? ORDER BY version_number DESC LIMIT 1",
                (playbook_ref,),
            ).fetchone()
            if latest is None:
                if prior_version_ref is not None:
                    raise ResearchPlaybookConflict("first playbook cannot have a prior version")
                version = 1
            else:
                if prior_version_ref != latest["playbook_version_id"]:
                    raise ResearchPlaybookConflict("playbook must continue the latest version")
                version = int(latest["version_number"]) + 1
            if cur.execute(
                "SELECT 1 FROM research_playbook_versions WHERE playbook_version_id=?", (version_id,)
            ).fetchone():
                raise ResearchPlaybookConflict("playbook version id already exists")
            created_at = _now()
            record = {
                "schema_version": SCHEMA_VERSION,
                "id": version_id,
                "created_at": created_at,
                "playbook_ref": playbook_ref,
                "version": version,
                "prior_version_ref": prior_version_ref,
                **body,
                "actor_ref": actor_ref,
            }
            wire = dict(record)
            wire["content_hash"] = content_hash(record)
            validate_research_playbook_version(wire)
            cur.execute(
                "INSERT INTO research_playbook_versions"
                "(playbook_version_id,playbook_ref,version_number,prior_version_id,"
                "record_json,content_hash,actor_ref,created_at) VALUES(?,?,?,?,?,?,?,?)",
                (
                    version_id, playbook_ref, version, prior_version_ref,
                    canonical_json(wire), wire["content_hash"], actor_ref, created_at,
                ),
            )
            cur.execute(
                "INSERT INTO research_playbook_pointer"
                "(playbook_ref,playbook_version_id,version_number,content_hash,updated_at) "
                "VALUES(?,?,?,?,?) ON CONFLICT(playbook_ref) DO UPDATE SET "
                "playbook_version_id=excluded.playbook_version_id,"
                "version_number=excluded.version_number,"
                "content_hash=excluded.content_hash,updated_at=excluded.updated_at",
                (playbook_ref, version_id, version, wire["content_hash"], created_at),
            )
            result = {"status": "fresh", **wire}
            self._save_idem(cur, idempotency_key, "publish_playbook", request_hash, result, created_at)
            return result

    def playbook(self, version_id: str) -> dict[str, Any]:
        return read_exact_playbook_version(self.connection, version_id)

    def active_playbook(self, playbook_ref: str) -> dict[str, Any]:
        return read_active_playbook_version(self.connection, playbook_ref)

    def playbook_report(self) -> dict[str, Any]:
        rows = self.connection.execute(
            "SELECT playbook_ref FROM research_playbook_pointer ORDER BY playbook_ref"
        ).fetchall()
        playbooks = []
        for row in rows:
            wire = self.active_playbook(row["playbook_ref"])
            playbooks.append({
                "playbook_ref": wire["playbook_ref"],
                "version_id": wire["id"],
                "version_number": wire["version"],
                "content_hash": wire["content_hash"],
                "title": wire["title"],
                "stage_refs": [stage["stage_ref"] for stage in wire["stages"]],
                "human_checkpoint_stages": [
                    stage["stage_ref"] for stage in wire["stages"] if stage["human_checkpoint"]
                ],
                "actor_ref": wire["actor_ref"],
                "created_at": wire["created_at"],
            })
        version_count = self.connection.execute(
            "SELECT COUNT(*) FROM research_playbook_versions"
        ).fetchone()[0]
        return {
            "projection_kind": "research_playbook_report",
            "playbook_count": len(playbooks),
            "version_count": version_count,
            "stage_order": list(STAGE_ORDER),
            "decision_vocabulary": list(DECISION_VOCABULARY),
            "playbooks": playbooks,
        }


__all__ = [
    "DECISION_VOCABULARY",
    "HUMAN_CHECKPOINT_STAGES",
    "NUMBER_PROVENANCE_RULE",
    "STAGE_ORDER",
    "ResearchPlaybookAuthority",
    "ResearchPlaybookConflict",
    "ResearchPlaybookError",
    "ResearchPlaybookNotFound",
    "ResearchPlaybookValidationError",
    "read_active_playbook_version",
    "read_exact_playbook_version",
    "validate_playbook_body",
    "validate_playbook_stages",
    "validate_research_playbook_version",
]
