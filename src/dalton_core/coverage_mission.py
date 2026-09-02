"""Coverage mission authority: the task-layer object above the planner.

A CoverageMission is what a human hands the research OS ("establish first
coverage of US IT services").  It freezes the industry, the company universe
with tiers, the research questions, the expected deliverables, an honest
source plan (which connectors are wired, which are not), exact bindings to
one ResearchPlaybook, one ResearchConstitution and one active Mandate, the
autonomy grant for the automation principal that will execute it, and a
budget.  Publishing a mission is human-only and append-only.

Stage records are the append-only ledger of how each company in the
universe moves through the playbook's frozen stage order.  The rules that the
manual states in prose become checks here: a company enters stage k only
after passing the gate of stage k-1; passing a human-checkpoint gate (Deep
Insight Gate, Investment Memo) requires a ``human:`` actor; an automation
actor must be the mission's declared principal and hold the ``stage_record``
write scope; ``gate_passed`` always needs evidence refs.  Nothing here writes
Evidence, Claims, Theses or models.
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

from .research_playbook import (
    STAGE_ORDER,
    ResearchPlaybookError,
    ResearchPlaybookNotFound,
    read_exact_playbook_version,
)
from .store import DaltonStore, canonical_json, content_hash


SCHEMA_VERSION = "0.1"
_SCHEMA_PATH = Path(__file__).with_name("coverage_mission_schema.sql")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_HUMAN_RE = re.compile(r"^human:[A-Za-z0-9][A-Za-z0-9._/@:-]*$")
_AUTOMATION_RE = re.compile(r"^automation:[A-Za-z0-9][A-Za-z0-9._/@:-]*$")

COVERAGE_TIERS: tuple[str, ...] = ("A", "B", "C")
BOOTSTRAP_PRIORITIES: tuple[str, ...] = ("P0", "P1", "P2")
DELIVERABLE_KINDS: tuple[str, ...] = (
    "industry_framework",
    "initial_screen",
    "industry_model",
    "company_model",
    "forecast_lines",
    "investment_memo",
    "weekly_brief",
)
SOURCE_STATUSES: tuple[str, ...] = ("connected", "probe_only", "not_connected")
# Objects an automation principal may be granted to write inside a mission.
# Theses, constitutions, playbooks, missions, mandates and governance policy
# are never in this list: they stay human-only by construction.
AUTOMATION_WRITE_SCOPES: tuple[str, ...] = (
    "evidence",
    "claim",
    "forecast_line",
    "model_run",
    "research_question",
    "observation",
    "stage_record",
)
CHECKPOINT_KINDS: tuple[str, ...] = (
    "deep_insight_gate",
    "investment_memo",
    "thesis_admission",
    "thesis_revision",
    "forecast_overturn",
    "scope_expansion",
    "budget_expansion",
)
# Checkpoints a mission can never drop, in addition to the playbook's
# human-checkpoint stages.
REQUIRED_CHECKPOINTS: frozenset[str] = frozenset({
    "thesis_admission", "thesis_revision", "scope_expansion", "budget_expansion",
})
STAGE_STATUSES: tuple[str, ...] = ("entered", "gate_passed", "gate_failed")

_BINDING_FIELDS = frozenset({"playbook_version", "constitution_version", "mandate_version"})
_BODY_FIELDS = frozenset({
    "title", "objective", "industry_ref", "universe", "research_questions",
    "deliverables", "source_plan", "bindings", "autonomy", "budget",
})
_VERSION_FIELDS = _BODY_FIELDS | frozenset({
    "schema_version", "id", "created_at", "mission_ref", "version",
    "prior_version_ref", "actor_ref", "content_hash",
})
_STAGE_RECORD_FIELDS = frozenset({
    "schema_version", "id", "created_at", "mission_version_ref", "mission_version_hash",
    "company_ref", "stage_ref", "status", "evidence_refs", "rationale", "actor_ref",
    "content_hash",
})


class CoverageMissionError(RuntimeError):
    """Base error for the coverage mission authority."""


class CoverageMissionValidationError(CoverageMissionError):
    """A request does not satisfy the closed contract."""


class CoverageMissionConflict(CoverageMissionError):
    """A request conflicts with immutable authority or stage order."""


class CoverageMissionNotFound(CoverageMissionError):
    """A bound authority, mission version or pointer is absent."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CoverageMissionValidationError(f"{name} must be non-empty text")
    return value.strip()


def _human(value: Any, name: str = "actor_ref") -> str:
    value = _text(value, name)
    if _HUMAN_RE.fullmatch(value) is None:
        raise CoverageMissionValidationError(f"{name} must use the human: namespace")
    return value


def _actor(value: Any, name: str = "actor_ref") -> str:
    value = _text(value, name)
    if _HUMAN_RE.fullmatch(value) is None and _AUTOMATION_RE.fullmatch(value) is None:
        raise CoverageMissionValidationError(f"{name} must use the human: or automation: namespace")
    return value


def _sha256(value: Any, name: str) -> str:
    value = _text(value, name)
    if _SHA256_RE.fullmatch(value) is None:
        raise CoverageMissionValidationError(f"{name} must be a lowercase SHA-256")
    return value


def _texts(value: Any, name: str, *, nonempty: bool = False) -> list[str]:
    if not isinstance(value, list):
        raise CoverageMissionValidationError(f"{name} must be an array")
    result = [_text(item, f"{name}[]") for item in value]
    if nonempty and not result:
        raise CoverageMissionValidationError(f"{name} must not be empty")
    if len(set(result)) != len(result):
        raise CoverageMissionValidationError(f"{name} must contain unique values")
    return result


def _closed(value: Any, fields: frozenset[str], name: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise CoverageMissionValidationError(f"{name} must be an object")
    result = dict(value)
    if set(result) != fields:
        raise CoverageMissionValidationError(
            f"{name} has an invalid closed shape; missing={sorted(fields - set(result))}, "
            f"unknown={sorted(set(result) - fields)}"
        )
    return result


def _binding(value: Any, name: str) -> dict[str, str]:
    obj = _closed(value, frozenset({"ref", "hash"}), name)
    return {"ref": _text(obj["ref"], f"{name}.ref"), "hash": _sha256(obj["hash"], f"{name}.hash")}


def _non_negative_int(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise CoverageMissionValidationError(f"{name} must be a non-negative integer")
    return value


def _non_negative_number(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        raise CoverageMissionValidationError(f"{name} must be a non-negative number")
    return float(value)


def _vocabulary(value: Any, allowed: tuple[str, ...], name: str) -> str:
    value = _text(value, name)
    if value not in allowed:
        raise CoverageMissionValidationError(f"{name} must be one of {list(allowed)}")
    return value


def validate_mission_body(value: Mapping[str, Any]) -> dict[str, Any]:
    """Validate the human-authored mission body without touching the store."""

    body = _closed(value, _BODY_FIELDS, "mission")
    body["title"] = _text(body["title"], "title")
    body["objective"] = _text(body["objective"], "objective")
    body["industry_ref"] = _text(body["industry_ref"], "industry_ref")

    if not isinstance(body["universe"], list) or not body["universe"]:
        raise CoverageMissionValidationError("universe must be a non-empty array")
    universe: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in body["universe"]:
        member = _closed(
            raw, frozenset({"company_ref", "ticker", "coverage_tier", "bootstrap_priority"}), "universe[]"
        )
        company_ref = _text(member["company_ref"], "universe[].company_ref")
        if company_ref in seen:
            raise CoverageMissionValidationError("universe company_ref must be unique")
        seen.add(company_ref)
        universe.append({
            "company_ref": company_ref,
            "ticker": _text(member["ticker"], "universe[].ticker"),
            "coverage_tier": _vocabulary(member["coverage_tier"], COVERAGE_TIERS, "universe[].coverage_tier"),
            "bootstrap_priority": _vocabulary(
                member["bootstrap_priority"], BOOTSTRAP_PRIORITIES, "universe[].bootstrap_priority"
            ),
        })
    body["universe"] = universe

    body["research_questions"] = _texts(body["research_questions"], "research_questions", nonempty=True)
    deliverables = _texts(body["deliverables"], "deliverables", nonempty=True)
    for item in deliverables:
        _vocabulary(item, DELIVERABLE_KINDS, "deliverables[]")
    body["deliverables"] = deliverables

    if not isinstance(body["source_plan"], list) or not body["source_plan"]:
        raise CoverageMissionValidationError("source_plan must be a non-empty array")
    sources: list[dict[str, str]] = []
    seen = set()
    for raw in body["source_plan"]:
        source = _closed(raw, frozenset({"source_ref", "role", "status"}), "source_plan[]")
        ref = _text(source["source_ref"], "source_plan[].source_ref")
        if ref in seen:
            raise CoverageMissionValidationError("source_plan source_ref must be unique")
        seen.add(ref)
        sources.append({
            "source_ref": ref,
            "role": _text(source["role"], "source_plan[].role"),
            "status": _vocabulary(source["status"], SOURCE_STATUSES, "source_plan[].status"),
        })
    body["source_plan"] = sources

    bindings = _closed(body["bindings"], _BINDING_FIELDS, "bindings")
    body["bindings"] = {field: _binding(bindings[field], f"bindings.{field}") for field in sorted(_BINDING_FIELDS)}

    autonomy = _closed(
        body["autonomy"], frozenset({"automation_principal", "may_write", "human_checkpoints"}), "autonomy"
    )
    principal = _text(autonomy["automation_principal"], "autonomy.automation_principal")
    if _AUTOMATION_RE.fullmatch(principal) is None:
        raise CoverageMissionValidationError("autonomy.automation_principal must use the automation: namespace")
    may_write = _texts(autonomy["may_write"], "autonomy.may_write")
    for item in may_write:
        _vocabulary(item, AUTOMATION_WRITE_SCOPES, "autonomy.may_write[]")
    checkpoints = _texts(autonomy["human_checkpoints"], "autonomy.human_checkpoints", nonempty=True)
    for item in checkpoints:
        _vocabulary(item, CHECKPOINT_KINDS, "autonomy.human_checkpoints[]")
    missing = REQUIRED_CHECKPOINTS - set(checkpoints)
    if missing:
        raise CoverageMissionValidationError(
            f"autonomy.human_checkpoints cannot drop {sorted(missing)}"
        )
    body["autonomy"] = {
        "automation_principal": principal,
        "may_write": may_write,
        "human_checkpoints": checkpoints,
    }

    budget = _closed(
        body["budget"],
        frozenset({"max_daily_paid_calls", "max_daily_cost_usd", "max_alphaengine_calls_24h"}),
        "budget",
    )
    body["budget"] = {
        "max_daily_paid_calls": _non_negative_int(budget["max_daily_paid_calls"], "budget.max_daily_paid_calls"),
        "max_daily_cost_usd": _non_negative_number(budget["max_daily_cost_usd"], "budget.max_daily_cost_usd"),
        "max_alphaengine_calls_24h": _non_negative_int(
            budget["max_alphaengine_calls_24h"], "budget.max_alphaengine_calls_24h"
        ),
    }
    return body


def validate_coverage_mission_version(value: Mapping[str, Any]) -> dict[str, Any]:
    wire = dict(value)
    if set(wire) != _VERSION_FIELDS or wire.get("schema_version") != SCHEMA_VERSION:
        raise CoverageMissionValidationError("coverage mission has an invalid closed shape")
    for field in ("id", "created_at", "mission_ref"):
        wire[field] = _text(wire[field], field)
    wire["actor_ref"] = _human(wire["actor_ref"])
    wire["content_hash"] = _sha256(wire["content_hash"], "content_hash")
    if type(wire["version"]) is not int or wire["version"] < 1:
        raise CoverageMissionValidationError("coverage mission version must be positive")
    if wire["prior_version_ref"] is not None:
        wire["prior_version_ref"] = _text(wire["prior_version_ref"], "prior_version_ref")
    wire.update(validate_mission_body({field: wire[field] for field in _BODY_FIELDS}))
    base = dict(wire)
    expected_hash = base.pop("content_hash")
    if content_hash(base) != expected_hash:
        raise CoverageMissionValidationError("coverage mission content_hash is invalid")
    return wire


def validate_mission_stage_record(value: Mapping[str, Any]) -> dict[str, Any]:
    wire = dict(value)
    if set(wire) != _STAGE_RECORD_FIELDS or wire.get("schema_version") != SCHEMA_VERSION:
        raise CoverageMissionValidationError("mission stage record has an invalid closed shape")
    for field in ("id", "created_at", "mission_version_ref", "company_ref", "rationale"):
        wire[field] = _text(wire[field], field)
    wire["mission_version_hash"] = _sha256(wire["mission_version_hash"], "mission_version_hash")
    wire["stage_ref"] = _vocabulary(wire["stage_ref"], STAGE_ORDER, "stage_ref")
    wire["status"] = _vocabulary(wire["status"], STAGE_STATUSES, "status")
    wire["evidence_refs"] = _texts(wire["evidence_refs"], "evidence_refs")
    wire["actor_ref"] = _actor(wire["actor_ref"])
    wire["content_hash"] = _sha256(wire["content_hash"], "content_hash")
    base = dict(wire)
    expected_hash = base.pop("content_hash")
    if content_hash(base) != expected_hash:
        raise CoverageMissionValidationError("mission stage record content_hash is invalid")
    return wire


def _canonical_record(raw: Any, name: str) -> dict[str, Any]:
    if not isinstance(raw, str):
        raise CoverageMissionConflict(f"{name} record is missing")
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise CoverageMissionConflict(f"{name} record is invalid") from exc
    if not isinstance(value, dict) or canonical_json(value) != raw:
        raise CoverageMissionConflict(f"{name} record is not canonical")
    return value


def _ref(prefix: str, identity: Mapping[str, Any]) -> str:
    return f"{prefix}:{content_hash(identity)[:32]}"


class CoverageMissionAuthority:
    """Publish missions, record stage progress and project mission state."""

    def __init__(self, store: DaltonStore):
        self.store = store
        self.connection = store.connection
        self._authorized = False
        self.connection.create_function(
            "dalton_coverage_mission_authorized", 0, lambda: int(self._authorized)
        )
        self.connection.executescript(_SCHEMA_PATH.read_text(encoding="utf-8"))

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Cursor]:
        if self._authorized:
            raise RuntimeError("CoverageMissionAuthority operation cannot be nested")
        self._authorized = True
        try:
            with self.store._transaction() as cur:
                yield cur
        finally:
            self._authorized = False

    @staticmethod
    def _request_hash(operation: str, request: Mapping[str, Any]) -> str:
        return content_hash({"operation": operation, "request": dict(request)})

    def _idem(
        self, cur: sqlite3.Cursor, key: str, operation: str, request_hash: str,
        *, marker: str = "status",
    ) -> dict[str, Any] | None:
        row = cur.execute(
            "SELECT * FROM coverage_mission_idempotency WHERE idempotency_key=?", (key,)
        ).fetchone()
        if row is None:
            return None
        if row["operation"] != operation or row["request_hash"] != request_hash:
            raise CoverageMissionConflict("idempotency key conflicts with prior request")
        return {**json.loads(row["result_json"]), marker: "duplicate"}

    def _save_idem(
        self, cur: sqlite3.Cursor, key: str, operation: str, request_hash: str,
        result: Mapping[str, Any], created_at: str,
    ) -> None:
        cur.execute(
            "INSERT INTO coverage_mission_idempotency"
            "(idempotency_key,operation,request_hash,result_json,created_at) VALUES(?,?,?,?,?)",
            (key, operation, request_hash, canonical_json(result), created_at),
        )

    # -- binding validation -------------------------------------------------

    def _validate_playbook_binding(self, cur: sqlite3.Cursor, binding: Mapping[str, str]) -> dict[str, Any]:
        try:
            playbook = read_exact_playbook_version(self.connection, binding["ref"])
        except ResearchPlaybookNotFound as exc:
            raise CoverageMissionNotFound(str(exc)) from exc
        except ResearchPlaybookError as exc:
            raise CoverageMissionConflict(str(exc)) from exc
        if playbook["content_hash"] != binding["hash"]:
            raise CoverageMissionConflict("playbook binding failed")
        pointer = cur.execute(
            "SELECT playbook_version_id FROM research_playbook_pointer WHERE playbook_ref=?",
            (playbook["playbook_ref"],),
        ).fetchone()
        if pointer is None or pointer["playbook_version_id"] != binding["ref"]:
            raise CoverageMissionConflict("playbook is not the active version")
        return playbook

    def _validate_constitution_binding(
        self, cur: sqlite3.Cursor, binding: Mapping[str, str], industry_ref: str
    ) -> dict[str, Any]:
        table = cur.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='research_constitution_versions'"
        ).fetchone()
        if table is None:
            raise CoverageMissionNotFound("research constitution authority is not open on this Core")
        row = cur.execute(
            "SELECT * FROM research_constitution_versions WHERE constitution_version_id=?",
            (binding["ref"],),
        ).fetchone()
        if row is None:
            raise CoverageMissionNotFound("research constitution version was not found")
        wire = _canonical_record(row["record_json"], "research constitution")
        base = dict(wire)
        asserted = base.pop("content_hash", None)
        if (
            wire.get("id") != binding["ref"]
            or asserted != row["content_hash"]
            or asserted != binding["hash"]
            or content_hash(base) != asserted
        ):
            raise CoverageMissionConflict("constitution binding failed")
        if row["industry_ref"] != industry_ref or wire.get("industry_ref") != industry_ref:
            raise CoverageMissionConflict("constitution does not govern the mission industry")
        pointer = cur.execute(
            "SELECT constitution_version_id FROM research_constitution_pointer WHERE constitution_ref=?",
            (row["constitution_ref"],),
        ).fetchone()
        if pointer is None or pointer["constitution_version_id"] != binding["ref"]:
            raise CoverageMissionConflict("constitution is not the active version")
        return wire

    def _validate_mandate_binding(
        self, cur: sqlite3.Cursor, binding: Mapping[str, str], industry_ref: str
    ) -> dict[str, Any]:
        row = cur.execute(
            "SELECT * FROM mandate_versions WHERE version_id=?", (binding["ref"],)
        ).fetchone()
        if row is None:
            raise CoverageMissionNotFound("mandate version was not found")
        wire = _canonical_record(row["record_json"], "mandate")
        base = dict(wire)
        asserted = base.pop("content_hash", None)
        if (
            wire.get("id") != binding["ref"]
            or asserted != row["content_hash"]
            or asserted != binding["hash"]
            or content_hash(base) != asserted
        ):
            raise CoverageMissionConflict("mandate binding failed")
        if industry_ref not in set(wire.get("scope_refs", [])):
            raise CoverageMissionConflict("mandate does not cover the mission industry")
        now = _now()
        if (
            wire.get("effective_from") > now
            or (wire.get("effective_until") is not None and wire["effective_until"] <= now)
        ):
            raise CoverageMissionConflict("mandate is outside its effective window")
        pointer = cur.execute(
            "SELECT version_id,active FROM mandate_pointer WHERE mandate_ref=?",
            (wire["mandate_ref"],),
        ).fetchone()
        if pointer is None or pointer["version_id"] != binding["ref"] or int(pointer["active"]) != 1:
            raise CoverageMissionConflict("mandate is not the active version")
        return wire

    # -- mission versions ----------------------------------------------------

    def create_mission(
        self,
        mission_ref: str,
        *,
        title: str,
        objective: str,
        industry_ref: str,
        universe: list[Mapping[str, Any]],
        research_questions: list[str],
        deliverables: list[str],
        source_plan: list[Mapping[str, Any]],
        bindings: Mapping[str, Any],
        autonomy: Mapping[str, Any],
        budget: Mapping[str, Any],
        actor_ref: str,
        version_id: str,
        prior_version_ref: str | None,
        idempotency_key: str,
    ) -> dict[str, Any]:
        mission_ref = _text(mission_ref, "mission_ref")
        actor_ref = _human(actor_ref)
        version_id = _text(version_id, "version_id")
        idempotency_key = _text(idempotency_key, "idempotency_key")
        if prior_version_ref is not None:
            prior_version_ref = _text(prior_version_ref, "prior_version_ref")
        body = validate_mission_body({
            "title": title,
            "objective": objective,
            "industry_ref": industry_ref,
            "universe": universe,
            "research_questions": research_questions,
            "deliverables": deliverables,
            "source_plan": source_plan,
            "bindings": bindings,
            "autonomy": autonomy,
            "budget": budget,
        })
        request = {
            "mission_ref": mission_ref,
            **body,
            "actor_ref": actor_ref,
            "version_id": version_id,
            "prior_version_ref": prior_version_ref,
        }
        request_hash = self._request_hash("create_mission", request)
        with self._transaction() as cur:
            duplicate = self._idem(cur, idempotency_key, "create_mission", request_hash)
            if duplicate is not None:
                return duplicate
            latest = cur.execute(
                "SELECT mission_version_id,version_number FROM coverage_mission_versions "
                "WHERE mission_ref=? ORDER BY version_number DESC LIMIT 1",
                (mission_ref,),
            ).fetchone()
            if latest is None:
                if prior_version_ref is not None:
                    raise CoverageMissionConflict("first mission cannot have a prior version")
                version = 1
            else:
                if prior_version_ref != latest["mission_version_id"]:
                    raise CoverageMissionConflict("mission must continue the latest version")
                version = int(latest["version_number"]) + 1
            if cur.execute(
                "SELECT 1 FROM coverage_mission_versions WHERE mission_version_id=?", (version_id,)
            ).fetchone():
                raise CoverageMissionConflict("mission version id already exists")
            playbook = self._validate_playbook_binding(cur, body["bindings"]["playbook_version"])
            self._validate_constitution_binding(
                cur, body["bindings"]["constitution_version"], body["industry_ref"]
            )
            self._validate_mandate_binding(cur, body["bindings"]["mandate_version"], body["industry_ref"])
            required_stage_checkpoints = {
                stage["stage_ref"] for stage in playbook["stages"] if stage["human_checkpoint"]
            }
            missing = required_stage_checkpoints - set(body["autonomy"]["human_checkpoints"])
            if missing:
                raise CoverageMissionConflict(
                    f"mission cannot drop playbook human checkpoints {sorted(missing)}"
                )
            created_at = _now()
            record = {
                "schema_version": SCHEMA_VERSION,
                "id": version_id,
                "created_at": created_at,
                "mission_ref": mission_ref,
                "version": version,
                "prior_version_ref": prior_version_ref,
                **body,
                "actor_ref": actor_ref,
            }
            wire = dict(record)
            wire["content_hash"] = content_hash(record)
            validate_coverage_mission_version(wire)
            cur.execute(
                "INSERT INTO coverage_mission_versions"
                "(mission_version_id,mission_ref,version_number,prior_version_id,industry_ref,"
                "playbook_version_ref,constitution_version_ref,mandate_version_ref,"
                "record_json,content_hash,actor_ref,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    version_id, mission_ref, version, prior_version_ref, body["industry_ref"],
                    body["bindings"]["playbook_version"]["ref"],
                    body["bindings"]["constitution_version"]["ref"],
                    body["bindings"]["mandate_version"]["ref"],
                    canonical_json(wire), wire["content_hash"], actor_ref, created_at,
                ),
            )
            cur.execute(
                "INSERT INTO coverage_mission_pointer"
                "(mission_ref,mission_version_id,version_number,content_hash,updated_at) "
                "VALUES(?,?,?,?,?) ON CONFLICT(mission_ref) DO UPDATE SET "
                "mission_version_id=excluded.mission_version_id,"
                "version_number=excluded.version_number,"
                "content_hash=excluded.content_hash,updated_at=excluded.updated_at",
                (mission_ref, version_id, version, wire["content_hash"], created_at),
            )
            result = {"status": "fresh", **wire}
            self._save_idem(cur, idempotency_key, "create_mission", request_hash, result, created_at)
            return result

    def mission(self, version_id: str) -> dict[str, Any]:
        version_id = _text(version_id, "version_id")
        row = self.connection.execute(
            "SELECT * FROM coverage_mission_versions WHERE mission_version_id=?", (version_id,)
        ).fetchone()
        if row is None:
            raise CoverageMissionNotFound("coverage mission version was not found")
        wire = validate_coverage_mission_version(_canonical_record(row["record_json"], "coverage mission"))
        if (
            wire["id"] != row["mission_version_id"]
            or wire["mission_ref"] != row["mission_ref"]
            or wire["version"] != row["version_number"]
            or wire["prior_version_ref"] != row["prior_version_id"]
            or wire["industry_ref"] != row["industry_ref"]
            or wire["bindings"]["playbook_version"]["ref"] != row["playbook_version_ref"]
            or wire["bindings"]["constitution_version"]["ref"] != row["constitution_version_ref"]
            or wire["bindings"]["mandate_version"]["ref"] != row["mandate_version_ref"]
            or wire["actor_ref"] != row["actor_ref"]
            or wire["created_at"] != row["created_at"]
            or wire["content_hash"] != row["content_hash"]
        ):
            raise CoverageMissionConflict("coverage mission authority drifted")
        return wire

    def active_mission(self, mission_ref: str) -> dict[str, Any]:
        mission_ref = _text(mission_ref, "mission_ref")
        pointer = self.connection.execute(
            "SELECT * FROM coverage_mission_pointer WHERE mission_ref=?", (mission_ref,)
        ).fetchone()
        if pointer is None:
            raise CoverageMissionNotFound("coverage mission pointer was not found")
        wire = self.mission(pointer["mission_version_id"])
        if wire["version"] != pointer["version_number"] or wire["content_hash"] != pointer["content_hash"]:
            raise CoverageMissionConflict("coverage mission pointer drifted")
        return wire

    # -- stage records -------------------------------------------------------

    def _stage_state(
        self, cur: sqlite3.Cursor, mission_version_ref: str, company_ref: str
    ) -> dict[str, list[str]]:
        """Ordered status history per stage for one company."""

        state: dict[str, list[str]] = {stage: [] for stage in STAGE_ORDER}
        for row in cur.execute(
            "SELECT stage_ref,status FROM coverage_mission_stage_records "
            "WHERE mission_version_ref=? AND company_ref=? ORDER BY created_at,record_id",
            (mission_version_ref, company_ref),
        ).fetchall():
            state[row["stage_ref"]].append(row["status"])
        return state

    def record_stage(
        self,
        *,
        mission_version_ref: str,
        mission_version_hash: str,
        company_ref: str,
        stage_ref: str,
        status: str,
        evidence_refs: list[str],
        rationale: str,
        actor_ref: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        mission_version_ref = _text(mission_version_ref, "mission_version_ref")
        mission_version_hash = _sha256(mission_version_hash, "mission_version_hash")
        company_ref = _text(company_ref, "company_ref")
        stage_ref = _vocabulary(stage_ref, STAGE_ORDER, "stage_ref")
        status = _vocabulary(status, STAGE_STATUSES, "status")
        evidence_refs = _texts(evidence_refs, "evidence_refs")
        rationale = _text(rationale, "rationale")
        actor_ref = _actor(actor_ref)
        idempotency_key = _text(idempotency_key, "idempotency_key")
        if status == "gate_passed" and not evidence_refs:
            raise CoverageMissionValidationError("gate_passed requires at least one evidence ref")
        request = {
            "mission_version_ref": mission_version_ref,
            "mission_version_hash": mission_version_hash,
            "company_ref": company_ref,
            "stage_ref": stage_ref,
            "status": status,
            "evidence_refs": evidence_refs,
            "rationale": rationale,
            "actor_ref": actor_ref,
        }
        request_hash = self._request_hash("record_stage", request)
        with self._transaction() as cur:
            duplicate = self._idem(
                cur, idempotency_key, "record_stage", request_hash, marker="status_marker"
            )
            if duplicate is not None:
                return duplicate
            mission = self.mission(mission_version_ref)
            if mission["content_hash"] != mission_version_hash:
                raise CoverageMissionConflict("mission version hash binding failed")
            pointer = cur.execute(
                "SELECT mission_version_id FROM coverage_mission_pointer WHERE mission_ref=?",
                (mission["mission_ref"],),
            ).fetchone()
            if pointer is None or pointer["mission_version_id"] != mission_version_ref:
                raise CoverageMissionConflict("stage records must bind the active mission version")
            if company_ref not in {member["company_ref"] for member in mission["universe"]}:
                raise CoverageMissionConflict("company is not in the mission universe")
            playbook = self._validate_playbook_binding(cur, mission["bindings"]["playbook_version"])
            stage = next(item for item in playbook["stages"] if item["stage_ref"] == stage_ref)
            if actor_ref.startswith("automation:"):
                if actor_ref != mission["autonomy"]["automation_principal"]:
                    raise CoverageMissionConflict("automation actor is not the mission principal")
                if "stage_record" not in mission["autonomy"]["may_write"]:
                    raise CoverageMissionConflict("mission does not grant stage_record writes to automation")
                if status == "gate_passed" and stage["human_checkpoint"]:
                    raise CoverageMissionConflict(
                        f"{stage_ref} is a human checkpoint; gate_passed requires a human: actor"
                    )
            state = self._stage_state(cur, mission_version_ref, company_ref)
            index = STAGE_ORDER.index(stage_ref)
            if status == "entered":
                if state[stage_ref]:
                    raise CoverageMissionConflict(f"{stage_ref} was already entered for this company")
                if index > 0 and "gate_passed" not in state[STAGE_ORDER[index - 1]]:
                    raise CoverageMissionConflict(
                        f"{stage_ref} cannot be entered before {STAGE_ORDER[index - 1]} gate_passed"
                    )
            else:
                if "entered" not in state[stage_ref]:
                    raise CoverageMissionConflict(f"{stage_ref} must be entered before its gate is decided")
                if "gate_passed" in state[stage_ref]:
                    raise CoverageMissionConflict(f"{stage_ref} gate was already passed for this company")
            identity = dict(request)
            record_id = _ref("mission-stage-record", identity)
            existing = cur.execute(
                "SELECT record_json FROM coverage_mission_stage_records WHERE record_id=?", (record_id,)
            ).fetchone()
            if existing is not None:
                return {**_canonical_record(existing["record_json"], "mission stage record"), "status_marker": "duplicate"}
            created_at = _now()
            record = {
                "schema_version": SCHEMA_VERSION,
                "id": record_id,
                "created_at": created_at,
                **identity,
            }
            wire = dict(record)
            wire["content_hash"] = content_hash(record)
            validate_mission_stage_record(wire)
            cur.execute(
                "INSERT INTO coverage_mission_stage_records"
                "(record_id,mission_version_ref,company_ref,stage_ref,status,actor_ref,"
                "record_json,content_hash,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    record_id, mission_version_ref, company_ref, stage_ref, status, actor_ref,
                    canonical_json(wire), wire["content_hash"], created_at,
                ),
            )
            result = {**wire, "status_marker": "fresh"}
            self._save_idem(cur, idempotency_key, "record_stage", request_hash, result, created_at)
            return result

    def stage_records(self, mission_version_ref: str, company_ref: str | None = None) -> list[dict[str, Any]]:
        mission_version_ref = _text(mission_version_ref, "mission_version_ref")
        query = "SELECT * FROM coverage_mission_stage_records WHERE mission_version_ref=?"
        params: list[Any] = [mission_version_ref]
        if company_ref is not None:
            query += " AND company_ref=?"
            params.append(_text(company_ref, "company_ref"))
        query += " ORDER BY created_at,record_id"
        records = []
        for row in self.connection.execute(query, params).fetchall():
            wire = validate_mission_stage_record(_canonical_record(row["record_json"], "mission stage record"))
            if (
                wire["id"] != row["record_id"]
                or wire["mission_version_ref"] != row["mission_version_ref"]
                or wire["company_ref"] != row["company_ref"]
                or wire["stage_ref"] != row["stage_ref"]
                or wire["status"] != row["status"]
                or wire["actor_ref"] != row["actor_ref"]
                or wire["created_at"] != row["created_at"]
                or wire["content_hash"] != row["content_hash"]
            ):
                raise CoverageMissionConflict("mission stage record authority drifted")
            records.append(wire)
        return records

    def mission_progress(self, mission_ref: str) -> dict[str, Any]:
        mission = self.active_mission(mission_ref)
        companies = []
        for member in mission["universe"]:
            history = self._stage_state(self.connection.cursor(), mission["id"], member["company_ref"])
            completed = [stage for stage in STAGE_ORDER if "gate_passed" in history[stage]]
            entered = [stage for stage in STAGE_ORDER if history[stage]]
            current = entered[-1] if entered else None
            if current is None:
                current_status = None
                next_stage = STAGE_ORDER[0]
            else:
                current_status = history[current][-1]
                if "gate_passed" in history[current]:
                    current_status = "gate_passed"
                    index = STAGE_ORDER.index(current)
                    next_stage = STAGE_ORDER[index + 1] if index + 1 < len(STAGE_ORDER) else None
                else:
                    next_stage = current
            companies.append({
                **member,
                "current_stage": current,
                "current_status": current_status,
                "completed_stages": completed,
                "next_stage": next_stage,
                "record_count": sum(len(items) for items in history.values()),
            })
        return {
            "projection_kind": "coverage_mission_progress",
            "mission_ref": mission["mission_ref"],
            "mission_version_ref": mission["id"],
            "mission_version_hash": mission["content_hash"],
            "stage_order": list(STAGE_ORDER),
            "companies": companies,
        }


__all__ = [
    "AUTOMATION_WRITE_SCOPES",
    "BOOTSTRAP_PRIORITIES",
    "CHECKPOINT_KINDS",
    "COVERAGE_TIERS",
    "DELIVERABLE_KINDS",
    "REQUIRED_CHECKPOINTS",
    "SOURCE_STATUSES",
    "STAGE_STATUSES",
    "CoverageMissionAuthority",
    "CoverageMissionConflict",
    "CoverageMissionError",
    "CoverageMissionNotFound",
    "CoverageMissionValidationError",
    "validate_coverage_mission_version",
    "validate_mission_body",
    "validate_mission_stage_record",
]
